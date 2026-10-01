import asyncio
import os
import socket
import struct
import tempfile
import unittest

from siman_r import envelope as env, pdulog, player
from siman_r.console_client import ConsoleClient, ReliabilityParams, open_client
from siman_r.recorder import Recorder, start_relay
from siman_r.vehicle_server import VehicleServer, serve
from tests.test_siman_r import OCU, VEH


class PcapFormat(unittest.TestCase):
    def test_header_and_packet(self):
        path = os.path.join(tempfile.mkdtemp(), "x.pcap")
        payload = env.encode(env.ActionRequestR(1, OCU, VEH, 7, env.Payload("X")))
        with open(path, "wb") as f:
            pdulog.PcapWriter(f).write(1.5, ("127.0.0.1", 40000), ("127.0.0.1", 3000), payload)
        data = open(path, "rb").read()
        magic, _, _, _, _, snap, link = struct.unpack_from("<IHHiIII", data)
        self.assertEqual((magic, link), (0xA1B2C3D4, pdulog.LINKTYPE_IPV4))
        sec, usec, incl, orig = struct.unpack_from("<IIII", data, 24)
        self.assertEqual((sec, usec, incl), (1, 500000, 28 + len(payload)))
        ip = data[40:60]
        self.assertEqual(pdulog._ip_checksum(ip), 0)          # 체크섬 포함 합 = 0이면 정상
        self.assertEqual(struct.unpack_from(">HH", data, 60), (40000, 3000))
        self.assertEqual(data[68:], payload)


class Colors(unittest.TestCase):
    def rec(self, pdu):
        return {"t_rel": 0.0, "dir": "x", **pdulog.summarize(env.encode(pdu))}

    def test_color_by_message_kind(self):
        req = self.rec(env.ActionRequestR(1, OCU, VEH, 1, env.Payload("X")))
        self.assertTrue(pdulog.format_record(req, True).startswith(pdulog._C_REQUEST))
        for status in (1, 2, 4, 5, 7):
            resp = self.rec(env.ActionResponseR(1, VEH, OCU, 1, status, env.Payload("Y")))
            line = pdulog.format_record(resp, True)
            self.assertTrue(line.startswith(pdulog._C_STATUS[status]))
            self.assertTrue(line.endswith(pdulog.RESET))
        bad = {"t_rel": 0.0, "dir": "x", **pdulog.summarize(b"\x07\x01")}
        self.assertTrue(pdulog.format_record(bad, True).startswith(pdulog._C_ERROR))

    def test_no_color_by_default(self):
        req = self.rec(env.ActionRequestR(1, OCU, VEH, 1, env.Payload("X")))
        self.assertNotIn("\033", pdulog.format_record(req))


class RecordAndReplay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        self.srv = VehicleServer(VEH, exercise_id=1, op_durations=(0.1, 0.1))
        self.vt = await serve("127.0.0.1", 0, self.srv)
        vaddr = self.vt.get_extra_info("sockname")
        self.out = os.path.join(self.dir, "s")
        self.rec = Recorder(self.out + ".jsonl", self.out + ".pcap")
        self.relay = await start_relay(("127.0.0.1", 0), vaddr, self.rec)
        raddr = self.relay.transport.get_extra_info("sockname")
        params = ReliabilityParams(response_timeout_s=0.3, status_poll_interval_s=1.0,
                                   completion_timeout_s=3.0)
        self.c = ConsoleClient(OCU, 1, raddr, params)
        self.ct = await open_client(self.c, ("127.0.0.1", 0))

    async def asyncTearDown(self):
        self.ct.close()
        self.relay.close()
        self.vt.close()
        self.rec.close()

    async def run_session(self):
        await self.c.request(VEH, "Request_Connection", {"request_control": True})
        return await self.c.request(VEH, "Command_AutonomousOperation", {"operation": "START"})

    async def test_relay_records_both_directions(self):
        r = await self.run_session()
        self.assertEqual(r.final.request_status, env.STATUS_COMPLETE)   # 중계를 거쳐도 핸드셰이크 정상
        self.rec.close()
        recs = list(pdulog.read_records(self.out + ".jsonl"))
        self.assertEqual([x["dir"] for x in recs],
                         ["console->vehicle", "vehicle->console", "console->vehicle"]
                         + ["vehicle->console"] * 3)
        self.assertEqual([x.get("request_status") for x in recs[3:]], [1, 2, 4])
        self.assertTrue(all(x["request_id"] == r.request_id for x in recs[2:]))
        self.assertEqual(recs, sorted(recs, key=lambda x: x["t_rel"]))
        # 해석 층은 원본 바이트에서 다시 만들 수 있다
        for x in recs:
            self.assertEqual(pdulog.summarize(bytes.fromhex(x["raw"]))["payload_type"],
                             x["payload_type"])

    async def test_replay_rewrites_exercise_and_vehicle_rejects(self):
        await self.run_session()
        self.rec.close()
        recs = list(pdulog.read_records(self.out + ".jsonl"))
        loop = asyncio.get_running_loop()

        # 수신기로 재생: 원본과 Exercise ID만 다르다
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        sink.settimeout(1.0)
        self.addCleanup(sink.close)
        n = await loop.run_in_executor(None, lambda: player.play(
            recs, sink.getsockname(), 99, 0, None, out=lambda s: None))
        self.assertEqual(n, len(recs))
        for x in recs:
            got = sink.recv(65535)
            orig = bytes.fromhex(x["raw"])
            self.assertEqual(got[1], 99)
            self.assertEqual(got[:1] + got[2:], orig[:1] + orig[2:])

        # 차량으로 재생: 요청만 골라 보내도 Exercise ID가 달라 실행되지 않는다
        before = self.srv.executions
        vaddr = self.vt.get_extra_info("sockname")
        n = await loop.run_in_executor(None, lambda: player.play(
            recs, vaddr, 99, 0, "console", out=lambda s: None))
        self.assertEqual(n, 2)                              # 접속 + 명령
        await asyncio.sleep(0.2)
        self.assertEqual(self.srv.executions, before)


if __name__ == "__main__":
    unittest.main()
