import asyncio
import struct
import unittest

from siman_r import envelope as env
from siman_r.envelope import ActionRequestR, ActionResponseR, EntityId, Payload
from siman_r.console_client import ConsoleClient, ReliabilityParams, open_client
from siman_r.vehicle_server import VehicleServer, serve

VEH = EntityId(1, 3, 1)
OCU = EntityId(2, 1, 1)
OCU2 = EntityId(2, 1, 2)


class EnvelopeLayout(unittest.TestCase):
    def test_request_bytes(self):
        p = Payload("Request_Connection", {"a": 1})          # {"type":"Request_Connection","body":{"a":1}}
        body = p.to_json_bytes()
        data = env.encode(ActionRequestR(1, OCU, VEH, 1024, p, timestamp=0x12345679))
        # 헤더: v7, Exercise 1, Type 56, Family 10
        self.assertEqual(data[:4], bytes([7, 1, 56, 10]))
        self.assertEqual(struct.unpack_from(">H", data, 8)[0], len(data))
        self.assertEqual(data[12:24], bytes.fromhex("000200010001 000100030001"))
        # Reliability 0 + 패딩 3, Request ID, Action ID
        self.assertEqual(data[24:28], b"\x00\x00\x00\x00")
        self.assertEqual(struct.unpack_from(">II", data, 28), (1024, env.ACTION_ID_VML_MESSAGE))
        self.assertEqual(struct.unpack_from(">II", data, 36), (3, 1))
        # Fixed Datum: 문서 4.12.2절 예시 Hex와 동일
        self.assertEqual(data[44:68], bytes.fromhex(
            "0007A12100000002" "0007A12200000100" "0007A12300000001"))
        # Variable Datum: 길이는 비트 단위, 64비트 경계 패딩
        did, nbits = struct.unpack_from(">II", data, 68)
        self.assertEqual((did, nbits), (500004, len(body) * 8))
        self.assertEqual(data[76:76 + len(body)], body)
        self.assertEqual(len(data), 76 + len(body) + (-len(body)) % 8)

    def test_roundtrip(self):
        req = ActionRequestR(5, OCU, VEH, 7, Payload("X", {"한글": "값", "n": 1.5}))
        out = env.decode(env.encode(req))
        self.assertEqual((out.originating, out.receiving, out.request_id), (OCU, VEH, 7))
        self.assertEqual(out.payload.body, {"한글": "값", "n": 1.5})
        resp = ActionResponseR(5, VEH, OCU, 7, env.STATUS_EXECUTING, Payload("Y", {}))
        out = env.decode(env.encode(resp))
        self.assertEqual(out.request_status, env.STATUS_EXECUTING)
        self.assertEqual(env.encode(resp)[2:4], bytes([57, 10]))

    def test_unknown_pdu_ignored_and_bad_length_rejected(self):
        es = bytes([7, 1, 1, 1]) + b"\0" * 4 + struct.pack(">H", 144) + b"\0" * 134
        self.assertIsNone(env.decode(es))                     # Entity State: 처리기 없음
        data = env.encode(ActionRequestR(1, OCU, VEH, 1, Payload("X")))
        with self.assertRaises(env.DecodeError):
            env.decode(data[:-8])

    def test_broadcast_match(self):
        self.assertTrue(EntityId(0xFFFF, 0xFFFF, 0xFFFF).matches(VEH))
        self.assertFalse(EntityId(1, 3, 2).matches(VEH))


class Handshake(unittest.IsolatedAsyncioTestCase):
    FAST = ReliabilityParams(response_timeout_s=0.2, max_retries=3, completion_timeout_s=3.0)

    async def asyncSetUp(self):
        self.srv = VehicleServer(VEH, exercise_id=1, op_durations=(0.2, 0.3))
        self.st = await serve("127.0.0.1", 0, self.srv)
        self.addr = self.st.get_extra_info("sockname")
        self.transports = [self.st]

    async def asyncTearDown(self):
        for t in self.transports:
            t.close()

    async def client(self, me=OCU, exercise=1, **kw):
        c = ConsoleClient(me, exercise, self.addr, self.FAST, **kw)
        self.transports.append(await open_client(c, ("127.0.0.1", 0)))
        return c

    async def connect(self, c):
        return await c.request(VEH, "Request_Connection", {"request_control": True})

    async def test_connection(self):
        r = await self.connect(await self.client())
        self.assertEqual(r.final.request_status, env.STATUS_COMPLETE)
        self.assertTrue(r.final.payload.body["control_granted"])
        self.assertEqual(r.attempts, 1)

    async def test_response_lost_retransmit_not_reapplied(self):
        """10.9절 예시 1: 첫 응답 손실 → 같은 Request ID 재전송 → 재적용 없이 이전 응답 재송신."""
        c = await self.client()
        self.srv.drop_first_n_tx = 1
        r = await self.connect(c)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(r.final.request_status, env.STATUS_COMPLETE)
        self.assertEqual(self.srv.executions, 1)

    async def test_request_lost_retransmit(self):
        c = await self.client(drop_first_n_tx=2)
        r = await self.connect(c)
        self.assertEqual(r.attempts, 3)
        self.assertEqual(self.srv.executions, 1)

    async def test_progress_pending_executing_complete(self):
        """10.9절 예시 2: 같은 Request ID로 Pending → Executing → Complete."""
        c = await self.client()
        await self.connect(c)
        r = await c.request(VEH, "Command_AutonomousOperation",
                            {"operation": "START", "task_id": "T-001"})
        self.assertEqual([x.request_status for x in r.responses],
                         [env.STATUS_PENDING, env.STATUS_EXECUTING, env.STATUS_COMPLETE])
        self.assertTrue(all(x.request_id == r.request_id for x in r.responses))

    async def test_command_without_control_denied(self):
        c = await self.client()
        r = await c.request(VEH, "Command_AutonomousOperation", {"operation": "START"})
        self.assertEqual(r.final.request_status, env.STATUS_REJECTED)
        self.assertEqual(r.final.payload.body["reason_code"], "NO_CONTROL")

    async def test_second_console_denied_control(self):
        await self.connect(await self.client())
        r = await self.connect(await self.client(me=OCU2))
        self.assertEqual(r.final.request_status, env.STATUS_REJECTED)
        self.assertEqual(r.final.payload.body["reason_code"], "CONTROL_HELD_BY_OTHER")

    async def test_unknown_message_unsupported(self):
        c = await self.client()
        r = await c.request(VEH, "Command_Teleport", {})
        self.assertEqual(r.final.payload.body["result"], "UNSUPPORTED")

    async def test_wrong_exercise_ignored(self):
        """8장 안전 격리: 승인되지 않은 Exercise ID는 응답 없이 버림 → 콘솔은 재시도 후 포기."""
        r = await self.connect(await self.client(exercise=99))
        self.assertTrue(r.timed_out)
        self.assertEqual(r.attempts, 4)
        self.assertEqual(self.srv.executions, 0)


if __name__ == "__main__":
    unittest.main()
