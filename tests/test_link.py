"""양방향 heartbeat, 통신 두절 판정, 단절 시 차량 동작.

기록 중계기의 링크 차단(relay.blocked)으로 콘솔↔차량 통신 두절을 만든다. 시간 값은 시험용으로 짧게 줄였다.
"""
import asyncio
import os
import struct
import tempfile
import unittest

from siman_r import envelope as env, pdulog
from siman_r.console_client import ConsoleClient, ReliabilityParams, open_client
from siman_r.envelope import DataPdu, Payload
from siman_r.recorder import Recorder, start_relay
from siman_r.vehicle_server import VehicleServer, serve
from tests.test_siman_r import OCU, VEH

PARAMS = ReliabilityParams(response_timeout_s=0.2, max_retries=3, status_poll_interval_s=0.3,
                           completion_timeout_s=6.0, heartbeat_period_s=0.1,
                           link_stale_s=0.35, link_lost_s=0.6)


class DataPduLayout(unittest.TestCase):
    def test_header_and_fields(self):
        p = Payload("Report_BasicInformation", {"mode": "STANDBY"})
        data = env.encode(DataPdu(1, VEH, OCU, p))
        self.assertEqual(data[:4], bytes([7, 1, 20, 5]))       # DIS 7, Exercise 1, Data, SIMAN
        self.assertEqual(struct.unpack_from(">H", data, 8)[0], len(data))
        self.assertEqual(data[24:32], b"\0" * 8)                 # Request ID 0(자발 송신) + 패딩
        self.assertEqual(struct.unpack_from(">II", data, 32), (3, 1))
        self.assertEqual(data[40:64], bytes.fromhex(              # 설계 문서 4.12.2절 Hex
            "0007A12100000002" "0007A12200000100" "0007A12300000001"))
        out = env.decode(data)
        self.assertIsInstance(out, DataPdu)
        self.assertEqual((out.originating, out.receiving, out.request_id), (VEH, OCU, 0))
        self.assertEqual(out.payload.body, {"mode": "STANDBY"})
        self.assertEqual(pdulog.summarize(data)["pdu_name"], "Data")


class Link(unittest.IsolatedAsyncioTestCase):
    async def start(self, behavior="STOP", mission=(0.1, 1.5)):
        self.dir = tempfile.mkdtemp()
        self.srv = VehicleServer(VEH, 1, op_durations=mission, comm_lost_timeout_s=0.6,
                                 comm_lost_behavior=behavior, report_period_s=0.1)
        self.vt = await serve("127.0.0.1", 0, self.srv)
        out = os.path.join(self.dir, "s")
        self.rec = Recorder(out + ".jsonl", None)
        self.jsonl = out + ".jsonl"
        self.relay = await start_relay(("127.0.0.1", 0), self.vt.get_extra_info("sockname"),
                                       self.rec)
        self.c = ConsoleClient(OCU, 1, self.relay.transport.get_extra_info("sockname"), PARAMS)
        self.ct = await open_client(self.c, ("127.0.0.1", 0))
        r = await self.c.request(VEH, "Request_Connection", {"request_control": True})
        self.assertEqual(r.final.request_status, env.STATUS_COMPLETE)

    async def asyncTearDown(self):
        self.ct.close()
        self.relay.close()
        self.vt.close()
        self.rec.close()
        await asyncio.sleep(0.15)                     # 링크 감시 루프가 끝나도록

    async def cut(self, after: float, duration: float):
        await asyncio.sleep(after)
        self.relay.blocked = True
        await asyncio.sleep(duration)
        self.relay.blocked = False

    async def test_heartbeat_keeps_link_alive(self):
        """명령 응답이 없는 구간도 heartbeat·주기 보고로 링크가 유지된다."""
        await self.start(mission=(0.1, 1.5))           # 두절 판정 시간(0.6 s)보다 긴 임무
        r = await self.c.request(VEH, "Command_AutonomousOperation", {"operation": "START"})
        self.assertEqual(r.final.request_status, env.STATUS_COMPLETE)
        self.assertEqual(self.srv.comm_lost_count, 0)
        self.assertEqual(self.c.link_events, ["OK"])
        self.assertEqual(self.c.latest_report["control_owner"], str(OCU))
        self.assertEqual(self.c.latest_report["comm_state"], "OK")
        self.rec.close()
        kinds = {r["payload_type"] for r in pdulog.read_records(self.jsonl)}
        self.assertTrue({"Report_ConsoleHeartbeat", "Report_BasicInformation"} <= kinds)

    async def test_comm_lost_stop_then_recover(self):
        """두절 → 차량 정지·제어권 해제, 복구 → 콘솔이 중단 결과를 받고 재접속."""
        await self.start("STOP", mission=(0.1, 3.0))
        cut = asyncio.ensure_future(self.cut(0.3, 1.2))
        r = await self.c.request(VEH, "Command_AutonomousOperation",
                                 {"operation": "START", "task_id": "T-9"})
        await cut
        self.assertEqual(r.final.request_status, env.STATUS_REJECTED)
        self.assertEqual(r.final.payload.body["result"], "FAILED")
        self.assertEqual(r.final.payload.body["reason_code"], "COMM_LOST")
        self.assertEqual(self.srv.comm_lost_count, 1)
        self.assertIsNone(self.srv.active)
        self.assertEqual(self.c.link_events[:4], ["OK", "STALE", "LOST", "OK"])
        await asyncio.sleep(0.4)                       # 재접속 완료 대기
        self.assertEqual(self.c.reconnects, 1)
        self.assertEqual(self.srv.controller, OCU)
        self.assertEqual(self.srv.comm_state, "OK")
        self.assertEqual(self.srv.mode, "COMM_LOST_STOP")   # 재접속만으로 임무가 재개되지 않음
        self.rec.close()
        dropped = [x for x in pdulog.read_records(self.jsonl) if x.get("dropped")]
        self.assertTrue(dropped)
        self.assertTrue(all(x["dropped"] == "link_cut" for x in dropped))
        replay = pdulog.load_records(self.jsonl)
        self.assertFalse(any(x.get("dropped") for x in replay))

    async def test_comm_lost_continue(self):
        """CONTINUE: 두절 중에도 임무를 계속하고 제어권을 유지한다."""
        await self.start("CONTINUE", mission=(0.1, 1.8))
        cut = asyncio.ensure_future(self.cut(0.3, 1.0))
        r = await self.c.request(VEH, "Command_AutonomousOperation", {"operation": "START"})
        await cut
        self.assertEqual(r.final.request_status, env.STATUS_COMPLETE)
        self.assertEqual(self.srv.comm_lost_count, 1)
        self.assertEqual(self.srv.controller, OCU)
        self.assertEqual(self.c.reconnects, 0)

    async def test_short_glitch_below_timeout(self):
        """판정 시간보다 짧은 끊김은 두절로 보지 않는다."""
        await self.start("STOP", mission=(0.1, 1.2))
        cut = asyncio.ensure_future(self.cut(0.3, 0.25))
        r = await self.c.request(VEH, "Command_AutonomousOperation", {"operation": "START"})
        await cut
        self.assertEqual(r.final.request_status, env.STATUS_COMPLETE)
        self.assertEqual(self.srv.comm_lost_count, 0)
        self.assertNotIn("LOST", self.c.link_events)

    async def test_stop_aborts_and_busy_rejected(self):
        await self.start(mission=(0.1, 3.0))
        start = asyncio.ensure_future(self.c.request(
            VEH, "Command_AutonomousOperation", {"operation": "START"}))
        await asyncio.sleep(0.2)
        busy = await self.c.request(VEH, "Command_AutonomousOperation", {"operation": "START"})
        self.assertEqual(busy.final.request_status, env.STATUS_RETRANSMIT_LATER)
        self.assertEqual(busy.final.payload.body["reason_code"], "BUSY")
        stop = await self.c.request(VEH, "Command_AutonomousOperation", {"operation": "STOP"})
        self.assertEqual(stop.final.request_status, env.STATUS_COMPLETE)
        r = await start
        self.assertEqual(r.final.payload.body["reason_code"], "STOPPED_BY_OPERATOR")


if __name__ == "__main__":
    unittest.main()
