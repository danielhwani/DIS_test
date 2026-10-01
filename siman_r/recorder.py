"""기록 중계기: 콘솔과 차량 사이에서 패킷을 그대로 전달하며 양방향을 기록한다 (설계 문서 11장).

    콘솔 ──▶ 중계기(:3001) ──▶ 차량(:3000)
    콘솔 ◀── 중계기        ◀── 차량

- 콘솔마다 차량 쪽 소켓을 따로 열어, 차량이 보낸 응답을 원래 콘솔에게 돌려준다.
- 패킷은 바꾸지 않고 전달한다. 기록에는 중계기 주소가 아니라 실제 콘솔·차량 주소를 남긴다.
- 출력: <out>.jsonl (원본 + 해석 층), <out>.pcap (Wireshark용)

실행: python -m siman_r.recorder --listen 3001 --vehicle 127.0.0.1:3000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from . import pdulog

log = logging.getLogger("recorder")


class Recorder:
    def __init__(self, jsonl_path: str, pcap_path: str | None, color: bool = False):
        self.jsonl = open(jsonl_path, "w", encoding="utf-8")
        self.pcap = pdulog.PcapWriter(open(pcap_path, "wb")) if pcap_path else None
        self.t0 = time.monotonic()
        self.count = 0
        self.color = color

    def record(self, direction: str, src: tuple, dst: tuple, data: bytes) -> None:
        now = time.time()
        t_utc = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="microseconds")
        r = pdulog.make_record(time.monotonic() - self.t0, t_utc, direction, src, dst, data)
        self.jsonl.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.jsonl.flush()
        if self.pcap:
            self.pcap.write(now, src, dst, data)
        self.count += 1
        log.info("%s", pdulog.format_record(r, self.color))

    def close(self) -> None:
        self.jsonl.close()
        if self.pcap:
            self.pcap.f.close()


class _Upstream(asyncio.DatagramProtocol):
    """콘솔 하나를 대신해 차량과 통신하는 소켓."""

    def __init__(self, relay: "Relay", console_addr: tuple):
        self.relay = relay
        self.console_addr = console_addr
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.relay.recorder.record("vehicle->console", addr, self.console_addr, data)
        self.relay.transport.sendto(data, self.console_addr)


class Relay(asyncio.DatagramProtocol):
    def __init__(self, vehicle_addr: tuple, recorder: Recorder):
        self.vehicle_addr = vehicle_addr
        self.recorder = recorder
        self.upstreams: dict[tuple, _Upstream] = {}
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.recorder.record("console->vehicle", addr, self.vehicle_addr, data)
        up = self.upstreams.get(addr)
        if up is not None and up.transport is not None:
            up.transport.sendto(data)
        else:
            asyncio.get_running_loop().create_task(self._open_and_send(addr, data))

    async def _open_and_send(self, addr: tuple, data: bytes) -> None:
        up = self.upstreams.get(addr)
        if up is None:
            up = self.upstreams[addr] = _Upstream(self, addr)
            await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: up, remote_addr=self.vehicle_addr)
            log.info("새 콘솔 %s:%d → 차량 쪽 소켓 개설", *addr)
        while up.transport is None:
            await asyncio.sleep(0)
        up.transport.sendto(data)

    def close(self) -> None:
        for up in self.upstreams.values():
            if up.transport:
                up.transport.close()
        if self.transport:
            self.transport.close()


async def start_relay(listen: tuple, vehicle_addr: tuple, recorder: Recorder) -> Relay:
    relay = Relay(vehicle_addr, recorder)
    await asyncio.get_running_loop().create_datagram_endpoint(lambda: relay, local_addr=listen)
    return relay


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:3001", help="콘솔이 보낼 주소 (host:port 또는 port)")
    ap.add_argument("--vehicle", default="127.0.0.1:3000")
    ap.add_argument("--out", default=None,
                    help="출력 경로(확장자 제외). 기본 recordings/session_<시각>")
    ap.add_argument("--no-pcap", action="store_true")
    ap.add_argument("--no-color", action="store_true", help="색상 끄기")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(name)-8s %(message)s",
                        datefmt="%H:%M:%S")
    host, _, port = a.listen.rpartition(":")
    listen = (host or "127.0.0.1", int(port))
    vhost, vport = a.vehicle.rsplit(":", 1)
    out = a.out or os.path.join("recordings", datetime.now().strftime("session_%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    color = not a.no_color and pdulog.use_color(sys.stderr)       # logging은 stderr로 출력
    rec = Recorder(out + ".jsonl", None if a.no_pcap else out + ".pcap", color)

    async def run():
        await start_relay(listen, (vhost, int(vport)), rec)
        log.info("기록 중계 udp://%s:%d → %s:%s, 저장 %s.jsonl%s",
                 *listen, vhost, vport, out, "" if a.no_pcap else " / .pcap")
        if color:
            log.info("%s", pdulog.LEGEND)
        await asyncio.Event().wait()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        rec.close()
        print(f"\n{rec.count}개 패킷 저장: {out}.jsonl" + ("" if a.no_pcap else f", {out}.pcap"))


if __name__ == "__main__":
    main()
