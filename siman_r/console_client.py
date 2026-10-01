"""운용콘솔 측 SIMAN-R 클라이언트.

- 응답을 기대하는 요청마다 Request ID를 부여한다. 카운터 시작값은 부팅 시각 기반 (5.3절).
- 응답 대기 시간 초과 시 같은 Request ID로 재전송한다 (10.9절 예시 1).
- 같은 Request ID의 첫 응답(Pending 포함)을 받으면 빠른 재전송을 멈추고 최종 상태까지 기다린다.
  기다리는 동안 차량에서 status_poll_interval_s 동안 아무 응답이 없으면 같은 Request ID로 다시 보내
  최신 상태를 묻는다. 응답이 오고 있으면 묻지 않는다.
  차량은 이를 중복 요청으로 보고 재실행 없이 최신 응답만 돌려주므로, Executing·Complete 응답이
  손실되어도 다음 재질의에서 복구된다.
- 자기 앞(Receiving ID)이 아니거나 이미 포기한 Request ID의 응답은 무시한다.

실행: python -m siman_r.console_client --vehicle 127.0.0.1:3000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass, field

from . import envelope as env
from .envelope import ActionRequestR, ActionResponseR, EntityId, Payload

log = logging.getLogger("console")


@dataclass
class ReliabilityParams:
    """연습 수준 운영 파라미터 (PDU 필드가 아님, 4.1절). 값은 실측 후 확정."""
    response_timeout_s: float = 1.0
    max_retries: int = 3
    status_poll_interval_s: float = 3.0
    completion_timeout_s: float = 30.0


@dataclass
class RequestResult:
    request_id: int
    responses: list[ActionResponseR] = field(default_factory=list)
    attempts: int = 0
    polls: int = 0
    timed_out: bool = False

    @property
    def final(self) -> ActionResponseR | None:
        if self.responses and self.responses[-1].request_status in env.FINAL_STATUSES:
            return self.responses[-1]
        return None


class _Pending:
    def __init__(self, result: RequestResult):
        self.result = result
        self.first = asyncio.Event()
        self.done = asyncio.Event()
        self.last_activity = time.monotonic()   # 마지막 응답 수신 또는 상태 재질의 시각


class ConsoleClient(asyncio.DatagramProtocol):
    def __init__(self, entity_id: EntityId, exercise_id: int, vehicle_addr: tuple,
                 params: ReliabilityParams | None = None, drop_first_n_tx: int = 0):
        self.me = entity_id
        self.exercise_id = exercise_id
        self.vehicle_addr = vehicle_addr
        self.params = params or ReliabilityParams()
        self.drop_first_n_tx = drop_first_n_tx
        self._next_id = int(time.time()) & 0x00FFFFFF   # 재부팅 시 과거 번호와 겹치지 않게
        self._pending: dict[int, _Pending] = {}
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def _send(self, data: bytes) -> None:
        if self.drop_first_n_tx > 0:
            self.drop_first_n_tx -= 1
            log.warning("  [loss] 요청 %d B 손실 모의", len(data))
            return
        self.transport.sendto(data, self.vehicle_addr)

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            pdu = env.decode(data)
        except env.DecodeError as e:
            log.warning("✗ 디코딩 실패: %s", e)
            return
        if not isinstance(pdu, ActionResponseR) or not pdu.receiving.matches(self.me):
            return
        p = self._pending.get(pdu.request_id)
        if p is None:
            log.info("  (무시) 대기하지 않는 req=%d 응답", pdu.request_id)
            return
        p.last_activity = time.monotonic()
        statuses = [r.request_status for r in p.result.responses]
        if statuses and statuses[-1] == pdu.request_status:
            return                                  # 재송신된 같은 응답
        p.result.responses.append(pdu)
        log.info("← %s req=%d status=%d %s", pdu.payload.type, pdu.request_id,
                 pdu.request_status, pdu.payload.body)
        p.first.set()
        if pdu.request_status in env.FINAL_STATUSES:
            p.done.set()

    async def request(self, receiving: EntityId, msg_type: str, body: dict,
                      wait_final: bool = True) -> RequestResult:
        self._next_id = (self._next_id + 1) & 0xFFFFFFFF
        rid = self._next_id
        pdu = ActionRequestR(self.exercise_id, self.me, receiving, rid, Payload(msg_type, body))
        data = env.encode(pdu)
        res = RequestResult(rid)
        p = self._pending[rid] = _Pending(res)
        try:
            for attempt in range(1 + self.params.max_retries):
                res.attempts = attempt + 1
                log.info("→ %s req=%d (시도 %d, %d B)", msg_type, rid, res.attempts, len(data))
                self._send(data)                    # 재전송도 같은 Request ID, 같은 바이트
                try:
                    await asyncio.wait_for(p.first.wait(), self.params.response_timeout_s)
                    break
                except asyncio.TimeoutError:
                    log.warning("  req=%d 응답 대기 시간 초과", rid)
            else:
                res.timed_out = True
                log.error("  req=%d 재시도 %d회 소진, 포기", rid, self.params.max_retries)
                return res
            if wait_final:
                await self._wait_final(p, data, rid)
            return res
        finally:
            del self._pending[rid]                  # 이후 도착한 옛 응답은 무시


    async def _wait_final(self, p: _Pending, data: bytes, rid: int) -> None:
        deadline = time.monotonic() + self.params.completion_timeout_s
        while not p.done.is_set():
            now = time.monotonic()
            if now >= deadline:
                p.result.timed_out = True
                log.error("  req=%d 완료 대기 시간 초과", rid)
                return
            next_poll = p.last_activity + self.params.status_poll_interval_s
            if now >= next_poll:
                p.result.polls += 1
                p.last_activity = now
                log.info("→ req=%d 상태 재질의 (%d회)", rid, p.result.polls)
                self._send(data)
                continue
            try:
                await asyncio.wait_for(p.done.wait(), min(next_poll, deadline) - now)
            except asyncio.TimeoutError:
                pass


async def open_client(client: ConsoleClient, local=("0.0.0.0", 0)):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: client, local_addr=local)
    return transport


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicle", default="127.0.0.1:3000")
    ap.add_argument("--vehicle-entity", default="1/3/1")
    ap.add_argument("--entity", default="2/1/1", help="콘솔 Entity ID")
    ap.add_argument("--exercise", type=int, default=1)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(name)-7s %(message)s",
                        datefmt="%H:%M:%S")
    host, port = a.vehicle.rsplit(":", 1)
    veh = EntityId.parse(a.vehicle_entity)

    async def run():
        c = ConsoleClient(EntityId.parse(a.entity), a.exercise, (host, int(port)))
        t = await open_client(c)
        r = await c.request(veh, "Request_Connection", {
            "console_id": "OCU-01", "operator_id": "OP-0123", "vml_version": "1.0",
            "request_control": True, "heartbeat_period_ms": 1000})
        if r.final is None or r.final.request_status != env.STATUS_COMPLETE:
            log.error("접속 실패")
            return
        await c.request(veh, "Command_AutonomousOperation", {"operation": "START", "task_id": "T-001"})
        t.close()
    asyncio.run(run())


if __name__ == "__main__":
    main()
