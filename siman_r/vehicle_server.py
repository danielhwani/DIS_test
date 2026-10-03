"""차량 측 SIMAN-R 서버 (실차 게이트웨이 또는 시뮬레이터 수신부 역할).

- 고정 포트에 bind하여 대기하고, 수신 패킷의 발신 주소로 응답한다 (3.3절).
- 승인된 Exercise ID와 자기 앞(Receiving ID)으로 온 PDU만 처리한다 (8장 안전 격리).
- (Originating ID, Request ID)로 중복을 판단하여 재적용 없이 마지막 응답만 재송신한다 (10.9절 예시 1).
  콘솔이 진행 중 명령의 상태를 같은 Request ID로 다시 물을 때도 이 경로로 최신 상태를 돌려준다.
- 오래 걸리는 명령은 같은 Request ID로 Pending → Executing → Complete를 보낸다 (10.9절 예시 2).
- 링크 감시 (3.3절, 4.12절): 제어권을 받은 콘솔에게 1 Hz로 Report_BasicInformation(Data PDU)을
  보내 heartbeat를 겸하고, 그 콘솔에게서 comm_lost_timeout_s 동안 아무 PDU도 오지 않으면
  통신 두절로 판정해 단절 시 동작(Command_CommLostBehaviorSetting의 behavior)을 실행한다.
    STOP     : 진행 중 임무를 중단(FAILED / COMM_LOST)하고 COMM_LOST_STOP 모드로 정지, 제어권 해제
    CONTINUE : 임무를 계속하고 제어권도 유지 (링크 상태만 LOST로 보고)
  콘솔의 heartbeat가 다시 들어오면 링크 상태를 OK로 되돌린다. 해제된 제어권은 재접속해야 다시 얻는다.

실행: python -m siman_r.vehicle_server --port 3000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import envelope as env
from .envelope import ActionRequestR, ActionResponseR, DataPdu, EntityId, Payload

log = logging.getLogger("vehicle")

DEDUP_TTL_S = 60.0
LINK_CHECK_S = 0.1            # 링크 감시 주기
COMM_LOST_BEHAVIORS = ("STOP", "CONTINUE")


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
                 drop_if: Callable[[ActionResponseR], bool] | None = None,
                 comm_lost_timeout_s: float = 5.0, comm_lost_behavior: str = "STOP",
                 report_period_s: float = 1.0):
        if comm_lost_behavior not in COMM_LOST_BEHAVIORS:
            raise ValueError(f"comm_lost_behavior must be one of {COMM_LOST_BEHAVIORS}")
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
        # 링크 감시 (값은 규약에서 확정)
        self.comm_lost_timeout_s = comm_lost_timeout_s
        self.comm_lost_behavior = comm_lost_behavior
        self.report_period_s = report_period_s
        self.link_peer: EntityId | None = None    # 감시 대상 콘솔 (제어권을 받은 콘솔)
        self.link_addr: tuple | None = None       # 그 콘솔의 주소 (주기 보고 목적지)
        self.last_heard = 0.0                     # 그 콘솔에게서 마지막으로 PDU를 받은 시각
        self.comm_state = "NONE"                  # NONE(접속 전) / OK / LOST
        self.comm_lost_count = 0
        self.active: tuple[ActionRequestR, tuple, asyncio.Task] | None = None   # 진행 중 임무

    # ------------------------------------------------------------ 전송
    def connection_made(self, transport):
        self.transport = transport
        asyncio.get_running_loop().create_task(self._link_loop())

    def _send(self, data: bytes, addr) -> None:
        if self.drop_if is not None:
            pdu = env.decode(data)
            dropped = isinstance(pdu, ActionResponseR) and self.drop_if(pdu)
        else:
            dropped = False
        if dropped:
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
        if not isinstance(pdu, (ActionRequestR, DataPdu)):
            return                                 # 처리기가 없는 PDU는 무시
        if not pdu.receiving.matches(self.me):
            return                                 # 다른 차량 앞 명령
        if pdu.originating == self.link_peer:
            self._heard_from_peer(addr)            # heartbeat뿐 아니라 어떤 PDU든 생존 신호
        if isinstance(pdu, DataPdu):
            return                                 # Report_ConsoleHeartbeat: 링크 갱신만

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
                self.link_peer, self.link_addr = req.originating, addr
                self._heard_from_peer(addr)
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
            "comm_lost": {"timeout_s": self.comm_lost_timeout_s,
                          "behavior": self.comm_lost_behavior},
            "report_period_s": self.report_period_s,
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
            self._abort_active("STOPPED_BY_OPERATOR")
            self.mode = "STANDBY"
            return self._respond(req, addr, "COMPLETED", {}, "Response_CommandResult")
        if self.active is not None:
            return self._respond(req, addr, "TEMPORARILY_REJECTED",
                                 {"reason_code": "BUSY",
                                  "active_request_id": self.active[0].request_id},
                                 "Response_CommandResult")
        self._respond(req, addr, "ACCEPTED", {}, "Response_CommandResult")
        task = asyncio.get_running_loop().create_task(self._run_task(req, addr))
        self.active = (req, addr, task)

    async def _run_task(self, req: ActionRequestR, addr) -> None:
        plan_s, done_s = self.op_durations
        try:
            await asyncio.sleep(plan_s)
            self.mode = "AUTONOMOUS"
            self._respond(req, addr, "IN_PROGRESS", {"detail": "주행 시작 (경로 계획 완료)"},
                          "Response_CommandResult")
            await asyncio.sleep(done_s)
            self.mode = "STANDBY"
            self._respond(req, addr, "COMPLETED",
                          {"task_id": req.payload.body.get("task_id")}, "Response_CommandResult")
        finally:
            if self.active is not None and self.active[0] is req:
                self.active = None

    def _abort_active(self, reason: str) -> None:
        """진행 중 임무를 중단하고 그 Request ID에 최종 응답(FAILED)을 남긴다.
        응답이 손실되거나 링크가 끊겨 있어도, 콘솔이 같은 Request ID로 재질의하면 이 응답을 받는다."""
        if self.active is None:
            return
        req, addr, task = self.active
        self.active = None
        task.cancel()
        log.warning("  임무 중단 req=%d (%s)", req.request_id, reason)
        self._respond(req, self.link_addr or addr, "FAILED",
                      {"reason_code": reason, "task_id": req.payload.body.get("task_id")},
                      "Response_CommandResult")

    # ------------------------------------------------------------ 링크 감시
    def _heard_from_peer(self, addr) -> None:
        self.last_heard = time.monotonic()
        self.link_addr = addr
        if self.comm_state == "LOST":
            note = "" if self.controller == self.link_peer else " (제어권 해제 상태: 재접속 필요)"
            log.warning("◆ 콘솔 %s 링크 복구%s", self.link_peer, note)
        self.comm_state = "OK"

    def _on_comm_lost(self) -> None:
        self.comm_state = "LOST"
        self.comm_lost_count += 1
        age = time.monotonic() - self.last_heard
        log.error("◆ 통신 두절: 콘솔 %s에게서 %.1f s 동안 수신 없음 → 단절 시 동작 %s",
                  self.link_peer, age, self.comm_lost_behavior)
        if self.comm_lost_behavior == "STOP":
            self._abort_active("COMM_LOST")
            self.mode = "COMM_LOST_STOP"
            self.controller = None
            log.error("  정지(COMM_LOST_STOP), 제어권 해제")

    def report_body(self) -> dict:
        return {
            "time_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "mode": self.mode,
            "control_owner": str(self.controller) if self.controller else None,
            "active_task_id": self.active[0].payload.body.get("task_id") if self.active else None,
            "comm_state": self.comm_state,
            "settings_version": self.settings_version,
        }

    async def _link_loop(self) -> None:
        next_report = time.monotonic() + self.report_period_s
        while self.transport is not None and not self.transport.is_closing():
            await asyncio.sleep(LINK_CHECK_S)
            if self.transport.is_closing():
                break
            now = time.monotonic()
            if (self.comm_state == "OK" and self.controller is not None
                    and self.controller == self.link_peer
                    and now - self.last_heard > self.comm_lost_timeout_s):
                self._on_comm_lost()
            if now >= next_report:
                next_report = now + self.report_period_s
                if self.link_peer is not None and self.link_addr is not None:
                    pdu = DataPdu(self.exercise_id, self.me, self.link_peer,
                                  Payload("Report_BasicInformation", self.report_body()))
                    self._send(env.encode(pdu), self.link_addr)


async def serve(host: str, port: int, server: VehicleServer):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: server, local_addr=(host, port))
    log.info("차량 %s 대기 중 udp://%s:%d (Exercise %d, 두절 판정 %.1f s → %s)", server.me, host,
             port, server.exercise_id, server.comm_lost_timeout_s, server.comm_lost_behavior)
    return transport


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--entity", default="1/3/1")
    ap.add_argument("--exercise", type=int, default=1)
    ap.add_argument("--drop", type=float, default=0.0, help="송신 손실 확률 (0~1)")
    ap.add_argument("--mission-s", type=float, default=5.0,
                    help="모의 임무의 주행 시간 s (경로 계획 2.5 s 뒤부터)")
    ap.add_argument("--comm-lost-timeout", type=float, default=5.0, help="통신 두절 판정 시간 s")
    ap.add_argument("--comm-lost-behavior", choices=COMM_LOST_BEHAVIORS, default="STOP",
                    help="단절 시 동작")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(name)-7s %(message)s",
                        datefmt="%H:%M:%S")
    srv = VehicleServer(EntityId.parse(a.entity), a.exercise, op_durations=(2.5, a.mission_s),
                        drop_tx=a.drop, comm_lost_timeout_s=a.comm_lost_timeout,
                        comm_lost_behavior=a.comm_lost_behavior)

    async def run():
        await serve(a.host, a.port, srv)
        await asyncio.Event().wait()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
