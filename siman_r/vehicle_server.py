"""차량 측 SIMAN-R 서버 (실차 게이트웨이 또는 시뮬레이터 수신부 역할).

- 고정 포트에 bind하여 대기하고, 수신 패킷의 발신 주소로 응답한다 (3.3절).
- 승인된 Exercise ID와 자기 앞(Receiving ID)으로 온 PDU만 처리한다 (8장 안전 격리).
- (Originating ID, Request ID)로 중복을 판단하여 재적용 없이 마지막 응답만 재송신한다 (10.9절 예시 1).
  콘솔이 진행 중 명령의 상태를 같은 Request ID로 다시 물을 때도 이 경로로 최신 상태를 돌려준다.
- 오래 걸리는 명령은 같은 Request ID로 Pending → Executing → Complete를 보낸다 (10.9절 예시 2).

실행: python -m siman_r.vehicle_server --port 3000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Callable

from . import envelope as env
from .envelope import ActionRequestR, ActionResponseR, EntityId, Payload

log = logging.getLogger("vehicle")

DEDUP_TTL_S = 60.0


@dataclass
class _Handled:
    last_response: bytes | None
    addr: tuple
    t: float                  # 마지막 응답 시각
    final: bool = False       # 최종 상태(완료·거부)를 보냈는지. 진행 중인 항목은 만료하지 않는다


class VehicleServer(asyncio.DatagramProtocol):
    def __init__(self, entity_id: EntityId, exercise_id: int,
                 op_durations: tuple[float, float] = (2.5, 5.0),
                 drop_tx: float = 0.0, drop_first_n_tx: int = 0,
                 drop_if: Callable[[ActionResponseR], bool] | None = None):
        self.me = entity_id
        self.exercise_id = exercise_id
        self.op_durations = op_durations          # (계획 완료까지, 임무 완료까지) s
        self.drop_tx = drop_tx                    # 무선 손실 모의 (확률)
        self.drop_first_n_tx = drop_first_n_tx    # 무선 손실 모의 (처음 n개 송신 손실)
        self.drop_if = drop_if                    # 무선 손실 모의 (조건에 맞는 응답 손실, 시험용)
        self.transport: asyncio.DatagramTransport | None = None
        self.handled: dict[tuple[EntityId, int], _Handled] = {}
        self.controller: EntityId | None = None   # 제어권 보유 콘솔
        self.settings_version = 1
        self.mode = "STANDBY"
        self.executions = 0                       # 실제로 실행한 명령 수 (멱등성 확인용)

    # ------------------------------------------------------------ 전송
    def connection_made(self, transport):
        self.transport = transport

    def _send(self, data: bytes, addr) -> None:
        if self.drop_if is not None and self.drop_if(env.decode(data)):
            log.warning("  [loss] 응답 %d B 손실 모의", len(data))
            return
        if self.drop_first_n_tx > 0:
            self.drop_first_n_tx -= 1
            log.warning("  [loss] 응답 %d B 손실 모의", len(data))
            return
        if random.random() < self.drop_tx:
            log.warning("  [loss] 응답 %d B 손실 모의", len(data))
            return
        self.transport.sendto(data, addr)

    def _respond(self, req: ActionRequestR, addr, result: str, body: dict,
                 type_: str) -> None:
        status = env.RESULT_TO_STATUS[result]
        resp = ActionResponseR(
            exercise_id=self.exercise_id, originating=self.me, receiving=req.originating,
            request_id=req.request_id, request_status=status,
            payload=Payload(type_, {"result": result, **body}))
        data = env.encode(resp)
        h = self.handled.get((req.originating, req.request_id))
        if h is not None:
            h.last_response = data
            h.addr = addr
            h.t = time.monotonic()
            h.final = status in env.FINAL_STATUSES
        log.info("→ %s req=%d status=%d result=%s", type_, req.request_id, status, result)
        self._send(data, addr)

    # ------------------------------------------------------------ 수신
    def datagram_received(self, data: bytes, addr) -> None:
        try:
            hdr = env.peek_header(data)
            if hdr.exercise_id != self.exercise_id:
                log.warning("✗ Exercise ID %d 거부 (승인값 %d) from %s",
                            hdr.exercise_id, self.exercise_id, addr)
                return
            pdu = env.decode(data)
        except env.DecodeError as e:
            log.warning("✗ 디코딩 실패 from %s: %s", addr, e)
            return
        if not isinstance(pdu, ActionRequestR):
            return                                 # 처리기가 없는 PDU는 무시
        if not pdu.receiving.matches(self.me):
            return                                 # 다른 차량 앞 명령

        self._expire_dedup()
        key = (pdu.originating, pdu.request_id)
        prev = self.handled.get(key)
        if prev is not None:
            log.info("← %s req=%d (중복: 재적용 없이 마지막 응답 재송신)",
                     pdu.payload.type, pdu.request_id)
            if prev.last_response is not None:
                self._send(prev.last_response, addr)
            return
        self.handled[key] = _Handled(None, addr, time.monotonic())
        log.info("← %s req=%d from %s %s", pdu.payload.type, pdu.request_id,
                 pdu.originating, pdu.payload.body)
        self._dispatch(pdu, addr)

    def _expire_dedup(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self.handled.items() if v.final and now - v.t > DEDUP_TTL_S]:
            del self.handled[k]

    # ------------------------------------------------------------ VML 처리
    def _dispatch(self, req: ActionRequestR, addr) -> None:
        p = req.payload
        if p.lang != env.LANG_VML:
            # 실차 게이트웨이는 VML 외 언어(예: 날씨 = 3)를 거른다 (6.5절)
            return self._respond(req, addr, "UNSUPPORTED",
                                 {"reason_code": "LANG_NOT_SUPPORTED"}, "Response_CommandResult")
        if p.version >> 8 != 1:
            return self._respond(req, addr, "DENIED",
                                 {"reason_code": "VERSION_MAJOR_MISMATCH"}, "Response_CommandResult")
        handler = {
            "Request_Connection": self._on_connection,
            "Command_AutonomousOperation": self._on_autonomous_operation,
        }.get(p.type)
        if handler is None:
            return self._respond(req, addr, "UNSUPPORTED",
                                 {"reason_code": "UNKNOWN_MESSAGE"}, "Response_CommandResult")
        handler(req, addr)

    def _on_connection(self, req: ActionRequestR, addr) -> None:
        b = req.payload.body
        granted = False
        if b.get("request_control"):
            if self.controller in (None, req.originating):
                self.controller = req.originating
                granted = True
            else:
                return self._respond(req, addr, "DENIED",
                                     {"reason_code": "CONTROL_HELD_BY_OTHER",
                                      "controller": str(self.controller)},
                                     "Response_Connection")
        self.executions += 1
        self._respond(req, addr, "COMPLETED", {
            "control_granted": granted,
            "vml_version": "1.0",
            "mode": self.mode,
            "settings_version": self.settings_version,
            "capabilities": {
                "messages": ["Request_Connection", "Command_AutonomousOperation"],
                "payload_langs": [env.LANG_VML],
            },
        }, "Response_Connection")

    def _on_autonomous_operation(self, req: ActionRequestR, addr) -> None:
        if req.originating != self.controller:
            return self._respond(req, addr, "DENIED",
                                 {"reason_code": "NO_CONTROL"}, "Response_CommandResult")
        op = req.payload.body.get("operation")
        if op not in ("START", "STOP"):
            return self._respond(req, addr, "DENIED",
                                 {"reason_code": "BAD_OPERATION"}, "Response_CommandResult")
        self.executions += 1
        if op == "STOP":
            self.mode = "STANDBY"
            return self._respond(req, addr, "COMPLETED", {}, "Response_CommandResult")
        self._respond(req, addr, "ACCEPTED", {}, "Response_CommandResult")
        asyncio.get_running_loop().create_task(self._run_task(req, addr))

    async def _run_task(self, req: ActionRequestR, addr) -> None:
        plan_s, done_s = self.op_durations
        await asyncio.sleep(plan_s)
        self.mode = "AUTONOMOUS"
        self._respond(req, addr, "IN_PROGRESS", {"detail": "주행 시작 (경로 계획 완료)"},
                      "Response_CommandResult")
        await asyncio.sleep(done_s)
        self.mode = "STANDBY"
        self._respond(req, addr, "COMPLETED",
                      {"task_id": req.payload.body.get("task_id")}, "Response_CommandResult")


async def serve(host: str, port: int, server: VehicleServer):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: server, local_addr=(host, port))
    log.info("차량 %s 대기 중 udp://%s:%d (Exercise %d)", server.me, host, port, server.exercise_id)
    return transport


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--entity", default="1/3/1")
    ap.add_argument("--exercise", type=int, default=1)
    ap.add_argument("--drop", type=float, default=0.0, help="응답 손실 확률 (0~1)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(name)-7s %(message)s",
                        datefmt="%H:%M:%S")
    srv = VehicleServer(EntityId.parse(a.entity), a.exercise, drop_tx=a.drop)

    async def run():
        await serve(a.host, a.port, srv)
        await asyncio.Event().wait()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
