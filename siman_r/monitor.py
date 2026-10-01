"""DIS 모니터: 받은 PDU를 해석해 한 줄씩 출력한다. 재생 확인용 수신기.

실행: python -m siman_r.monitor --port 4000
"""
from __future__ import annotations

import argparse
import socket
import sys
import time

from . import pdulog


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--no-color", action="store_true", help="색상 끄기")
    a = ap.parse_args()
    color = not a.no_color and pdulog.use_color(sys.stdout)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((a.host, a.port))
    print(f"모니터 대기 udp://{a.host}:{a.port} (Ctrl+C로 종료)")
    if color:
        print(pdulog.LEGEND)
    t0 = None
    try:
        while True:
            data, addr = sock.recvfrom(65535)
            now = time.monotonic()
            t0 = now if t0 is None else t0
            r = {"t_rel": now - t0, "dir": f"from {addr[0]}:{addr[1]}", **pdulog.summarize(data)}
            print(pdulog.format_record(r, color), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
