"""운용콘솔 측 SIMAN-R 클라이언트.

- 응답을 기대하는 요청마다 Request ID를 부여한다. 카운터 시작값은 부팅 시각 기반 (5.3절).
- 응답 대기 시간 초과 시 같은 Request ID로 재전송한다 (10.9절 예시 1).
- 같은 Request ID의 첫 응답(Pending 포함)을 받으면 빠른 재전송을 멈추고 최종 상태까지 기다린다.
  기다리는 동안 차량에서 status_poll_interval_s 동안 아무 응답이 없으면 같은 Request ID로 다시 보내
  최신 상태를 묻는다. 응답이 오고 있으면 묻지 않는다.
  차량은 이를 중복 요청으로 보고 재실행 없이 최신 응답만 돌려주므로, Executing·Complete 응답이
  손실되어도 다음 재질의에서 복구된다.
- 자기 앞(Receiving ID)이 아니거나 이미 포기한 Request ID의 응답은 무시한다.
- 링크 감시 (3.3절, 4.12절): 제어권을 받으면 heartbeat_period_s마다 Report_ConsoleHeartbeat
  (Data PDU)를 보낸다. 차량에서 오는 PDU(주기 보고·응답)가 link_stale_s 동안 없으면 STALE
  (정보 갱신 안 됨), link_lost_s 동안 없으면 LOST(통신 두절)로 판정한다. 다시 수신되면 OK로
  되돌리고, 차량이 제어권을 해제했다면 자동으로 다시 접속한다(auto_reconnect).

실행: python -m siman_r.console_client --vehicle 127.0.0.1:3000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass, field

from . import envelope as env
from .envelope import ActionRequestR, ActionResponseR, DataPdu, EntityId, Payload

log = logging.getLogger("console")


@dataclass
class ReliabilityParams:
    """연습 수준 운영 파라미터 (PDU 필드가 아님, 4.1절). 값은 실측 후 확정."""
    response_timeout_s: float = 1.0
    max_retries: int = 3
    status_poll_interval_s: float = 3.0
    completion_timeout_s: float = 30.0
    heartbeat_period_s: float = 1.0
    link_stale_s: float = 3.0
    link_lost_s: float = 5.0


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
                 params: ReliabilityParams | None = None, drop_first_n_tx: int = 0,
                 auto_reconnect: bool = True):
        self.me = entity_id
        self.exercise_id = exercise_id
        self.vehicle_addr = vehicle_addr
        self.params = params or ReliabilityParams()
        self.drop_first_n_tx = drop_first_n_tx
        self._next_id = int(time.time()) & 0x00FFFFFF   # 재부팅 시 과거 번호와 겹치지 않게
        self._pending: dict[int, _Pending] = {}
        self.transport: asyncio.DatagramTransport | None = None
        # 링크 감시
        self.auto_reconnect = auto_reconnect
        self.link_state = "NONE"                # NONE(접속 전) / OK / STALE / LOST
        self.link_events: list[str] = []        # 상태 전이 기록 (시험·로그용)
        self.last_vehicle_rx = 0.0
        self.latest_report: dict | None = None
        self.reconnects = 0
        self._connection: tuple[EntityId, dict] | None = None   # 재접속에 쓸 접속 요청
        self._link_task: asyncio.Task | None = None
        self._hb_seq = 0

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
        if pdu is None or not pdu.receiving.matches(self.me):
            return
        if isinstance(pdu, DataPdu):
            self._heard_from_vehicle()
            if pdu.payload.type == "Report_BasicInformation":
                self._on_report(pdu.payload.body)
            return
        self._heard_from_vehicle()
        if not isinstance(pdu, ActionResponseR):
            return
        if (pdu.payload.type == "Response_Connection"
                and pdu.request_status == env.STATUS_COMPLETE
                and pdu.payload.body.get("control_granted")):
            self._start_link()
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
        if msg_type == "Request_Connection" and body.get("request_control"):
            self._connection = (receiving, body)
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


    # ------------------------------------------------------------ 링크 감시
    def _set_link(self, state: str, detail: str = "") -> None:
        if state == self.link_state:
            return
        prev, self.link_state = self.link_state, state
        self.link_events.append(state)
        msg = {"OK": "링크 정상", "STALE": "정보 갱신 안 됨", "LOST": "통신 두절"}[state]
        level = logging.INFO if state == "OK" else (
            logging.WARNING if state == "STALE" else logging.ERROR)
        log.log(level, "◆ %s (%s → %s)%s", msg, prev, state, detail)

    def _heard_from_vehicle(self) -> None:
        self.last_vehicle_rx = time.monotonic()
        if self.link_state in ("STALE", "LOST"):
            self._set_link("OK")

    def _on_report(self, body: dict) -> None:
        prev = self.latest_report or {}
        self.latest_report = body
        keys = ("mode", "control_owner", "comm_state", "active_task_id")
        if any(prev.get(k) != body.get(k) for k in keys):
            log.info("  차량 상태: %s", {k: body.get(k) for k in keys})
        me = str(self.me)
        if (self.auto_reconnect and self._connection
                and prev.get("control_owner") == me and body.get("control_owner") != me):
            # 차량이 제어권을 해제했다(예: 통신 두절 STOP). 한 번만 재접속해 제어권을 다시 받고
            # 상태를 재동기화한다 (7장). 다른 콘솔이 가져간 경우 재접속은 거부되고 더 시도하지 않는다.
            self.reconnects += 1
            receiving, conn_body = self._connection
            log.warning("  제어권 해제 확인 → 재접속 (%d회)", self.reconnects)
            asyncio.get_running_loop().create_task(
                self.request(receiving, "Request_Connection", conn_body))

    def _start_link(self) -> None:
        self.last_vehicle_rx = time.monotonic()
        if self.link_state == "NONE":
            self._set_link("OK")
        if self._link_task is None or self._link_task.done():
            self._link_task = asyncio.get_running_loop().create_task(self._link_loop())

    async def _link_loop(self) -> None:
        p = self.params
        next_hb = time.monotonic() + p.heartbeat_period_s
        while self.transport is not None and not self.transport.is_closing():
            await asyncio.sleep(0.1)
            if self.transport.is_closing():
                break
            now = time.monotonic()
            if now >= next_hb and self._connection is not None:
                next_hb = now + p.heartbeat_period_s
                self._hb_seq += 1
                hb = DataPdu(self.exercise_id, self.me, self._connection[0],
                             Payload("Report_ConsoleHeartbeat",
                                     {"seq": self._hb_seq, "link_state": self.link_state}))
                self._send(env.encode(hb))
            age = now - self.last_vehicle_rx
            if age > p.link_lost_s:
                self._set_link("LOST", f" — 차량 수신 없음 {age:.1f} s")
            elif age > p.link_stale_s and self.link_state == "OK":
                self._set_link("STALE", f" — 차량 수신 없음 {age:.1f} s")


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
    ap.add_argument("--completion-timeout", type=float, default=30.0, help="완료 대기 시간 s")
    ap.add_argument("--hold", action="store_true",
                    help="명령이 끝나도 종료하지 않고 heartbeat·링크 감시를 계속 (Ctrl+C로 종료)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(name)-7s %(message)s",
                        datefmt="%H:%M:%S")
    host, port = a.vehicle.rsplit(":", 1)
    veh = EntityId.parse(a.vehicle_entity)

    async def run():
        params = ReliabilityParams(completion_timeout_s=a.completion_timeout)
        c = ConsoleClient(EntityId.parse(a.entity), a.exercise, (host, int(port)), params)
        t = await open_client(c)
        r = await c.request(veh, "Request_Connection", {
            "console_id": "OCU-01", "operator_id": "OP-0123", "vml_version": "1.0",
            "request_control": True, "heartbeat_period_ms": 1000})
        if r.final is None or r.final.request_status != env.STATUS_COMPLETE:
            log.error("접속 실패")
            return
        await c.request(veh, "Command_AutonomousOperation", {"operation": "START", "task_id": "T-001"})
        if a.hold:
            log.info("명령 처리 끝. heartbeat·링크 감시 계속 (Ctrl+C로 종료)")
            await asyncio.Event().wait()
        t.close()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
