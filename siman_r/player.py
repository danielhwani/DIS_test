"""기록 재생기 (설계 문서 11.3·11.4절).

재생(playback): 기록된 PDU 원본 바이트를 원래 시간 간격대로 다시 송출한다.
재생한 명령이 실차에 닿지 않도록 Exercise ID를 바꿔 보낸다. 기록과 같은 Exercise ID로
보내려면 --keep-exercise를 명시해야 한다.

    python -m siman_r.player recordings/session_X.jsonl --print            # 화면에 타임라인만
    python -m siman_r.player recordings/session_X.jsonl --target 127.0.0.1:4000   # 모니터로 송출
    python -m siman_r.player capture.pcapng --print        # Wireshark에서 저장한 캡처도 가능

pcap/pcapng는 차량 포트(--vehicle-port, 기본 3000)로 방향을 판단하고, 그 포트를 지나지 않는
패킷은 버린다.
"""
from __future__ import annotations

import argparse
import socket
import sys
import time

from . import pdulog

REPLAY_EXERCISE_ID = 99


def rewrite_exercise(data: bytes, exercise_id: int) -> bytes:
    return data[:1] + bytes([exercise_id]) + data[2:]


def play(records: list[dict], target: tuple | None, exercise_id: int | None,
         speed: float, sender: str | None = None, out=print, color: bool = False) -> int:
    """records를 재생하고 보낸 패킷 수를 돌려준다. target이 None이면 출력만 한다.
    sender가 "console" 또는 "vehicle"이면 그쪽이 보낸 패킷만 재생한다."""
    sel = [r for r in records if sender is None or r["dir"].startswith(sender + "->")]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if target else None
    start = time.monotonic()
    t_first = sel[0]["t_rel"] if sel else 0.0
    try:
        for r in sel:
            if speed > 0:
                delay = (r["t_rel"] - t_first) / speed - (time.monotonic() - start)
                if delay > 0:
                    time.sleep(delay)
            data = bytes.fromhex(r["raw"])
            if exercise_id is not None:
                data = rewrite_exercise(data, exercise_id)
            if sock:
                sock.sendto(data, target)
            out(pdulog.format_record({**r, **pdulog.summarize(data)}, color))
    finally:
        if sock:
            sock.close()
    return len(sel)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("recording", help="recorder가 만든 .jsonl, 또는 .pcap/.pcapng 캡처")
    ap.add_argument("--vehicle-port", type=int, default=3000,
                    help="pcap/pcapng에서 방향을 판단할 차량 포트 (기본 3000)")
    ap.add_argument("--target", help="송출 주소 host:port. 생략하면 --print와 같음")
    ap.add_argument("--print", action="store_true", help="송출 없이 타임라인만 출력")
    ap.add_argument("--speed", type=float, default=1.0, help="배속. 0이면 기다리지 않음")
    ap.add_argument("--from", dest="sender", choices=["console", "vehicle"],
                    help="한쪽이 보낸 패킷만 재생 (console = 요청, vehicle = 응답)")
    ap.add_argument("--exercise", type=int, default=REPLAY_EXERCISE_ID,
                    help=f"재생 시 Exercise ID (기본 {REPLAY_EXERCISE_ID})")
    ap.add_argument("--keep-exercise", action="store_true",
                    help="기록의 Exercise ID 그대로 송출 (실차 연결 환경에서는 금지)")
    ap.add_argument("--no-color", action="store_true", help="색상 끄기")
    a = ap.parse_args()

    records = pdulog.load_records(a.recording, a.vehicle_port)
    if not records:
        ap.error(f"재생할 패킷이 없다 (pcap이면 차량 포트 {a.vehicle_port}을 지나는 UDP 패킷이 있는지 확인)")
    recorded = {r.get("exercise_id") for r in records}
    target = None
    if a.target and not a.print:
        host, port = a.target.rsplit(":", 1)
        target = (host, int(port))
    exercise = None if (a.keep_exercise or target is None) else a.exercise
    if exercise is not None and exercise in recorded:
        ap.error(f"재생 Exercise ID {exercise}가 기록의 Exercise ID와 같다. 다른 값을 지정할 것")

    where = f"→ {target[0]}:{target[1]}" if target else "(출력만)"
    ex = "기록 그대로" if exercise is None else str(exercise)
    color = not a.no_color and pdulog.use_color(sys.stdout)
    print(f"재생 {a.recording}: {len(records)}개, {a.speed}배속, Exercise ID {ex} {where}")
    if color:
        print(pdulog.LEGEND)
    n = play(records, target, exercise, a.speed, a.sender, color=color)
    print(f"{n}개 재생 완료")


if __name__ == "__main__":
    main()
