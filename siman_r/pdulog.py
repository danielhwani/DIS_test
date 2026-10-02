"""PDU 기록 형식 (설계 문서 11장).

한 패킷 = JSONL 한 줄. 원본 층(수신 시각, 송수신 주소, PDU 바이트 hex)과
해석 층(헤더 필드, SIMAN-R 필드, JSON 페이로드)을 같은 줄에 담는다.
해석 층은 원본 바이트에서 언제든 다시 만들 수 있으므로, 재생은 원본 바이트만 사용한다.

같은 내용을 pcap으로도 쓴다. 이더넷 없이 IPv4/UDP 헤더만 붙이므로(LINKTYPE_IPV4)
Wireshark가 UDP 3000번 포트를 DIS로 해석한다.
"""
from __future__ import annotations

import json
import os
import socket
import struct
from typing import IO, Any, Iterator

from . import envelope as env

PDU_TYPE_NAMES = {
    1: "Entity State", 20: "Data",
    env.PDU_ACTION_REQUEST_R: "Action Request-R",
    env.PDU_ACTION_RESPONSE_R: "Action Response-R",
}


def summarize(data: bytes) -> dict[str, Any]:
    """해석 층 필드. 디코딩에 실패해도 원본은 남기므로 예외를 던지지 않는다."""
    out: dict[str, Any] = {}
    try:
        h = env.peek_header(data)
    except env.DecodeError as e:
        return {"decode_error": str(e)}
    out.update(exercise_id=h.exercise_id, pdu_type=h.pdu_type,
               pdu_name=PDU_TYPE_NAMES.get(h.pdu_type, f"Type {h.pdu_type}"),
               family=h.family, timestamp=h.timestamp)
    try:
        pdu = env.decode(data)
    except env.DecodeError as e:
        out["decode_error"] = str(e)
        return out
    if pdu is None:
        return out
    out.update(originating=str(pdu.originating), receiving=str(pdu.receiving),
               request_id=pdu.request_id, payload_type=pdu.payload.type,
               payload_body=pdu.payload.body)
    if isinstance(pdu, env.ActionResponseR):
        out["request_status"] = pdu.request_status
    return out


def make_record(t_rel: float, t_utc: str, direction: str, src: tuple, dst: tuple,
                data: bytes) -> dict[str, Any]:
    return {"t_rel": round(t_rel, 6), "t_utc": t_utc, "dir": direction,
            "src": f"{src[0]}:{src[1]}", "dst": f"{dst[0]}:{dst[1]}",
            "len": len(data), **summarize(data), "raw": data.hex()}


def read_records(path: str) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def load_records(path: str, vehicle_port: int = 3000) -> list[dict[str, Any]]:
    """재생용 기록 읽기. 기록 중계기의 .jsonl 또는 Wireshark·중계기의 pcap/pcapng.

    캡처 파일에는 방향 정보가 없으므로 차량 포트로 판단한다. 목적지가 차량 포트면
    console->vehicle, 출발지가 차량 포트면 vehicle->console. 둘 다 아닌 UDP 패킷은 버린다
    (중계기를 끼운 캡처에서 콘솔↔중계기 구간이 함께 잡혀도 중복되지 않는다).
    """
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:1] == b"{":
        return list(read_records(path))
    from . import pcapread
    from datetime import datetime, timezone
    out: list[dict[str, Any]] = []
    t0 = None
    for p in pcapread.read_udp(path):
        if p.dst[1] == vehicle_port:
            direction = "console->vehicle"
        elif p.src[1] == vehicle_port:
            direction = "vehicle->console"
        else:
            continue
        t0 = p.t_epoch if t0 is None else t0
        t_utc = datetime.fromtimestamp(p.t_epoch, timezone.utc).isoformat(timespec="microseconds")
        out.append(make_record(p.t_epoch - t0, t_utc, direction, p.src, p.dst, p.payload))
    return out


# ------------------------------------------------------------------ 터미널 색상
# 방향이 아니라 메시지 종류로 칠한다. 모니터는 방향을 모르지만 종류는 패킷에 들어 있다.

RESET = "\033[0m"
_C_REQUEST = "\033[36m"           # 청록: 요청 (Action Request-R)
_C_STATUS = {
    env.STATUS_PENDING: "\033[33m",             # 노랑: 접수
    env.STATUS_EXECUTING: "\033[94m",           # 파랑: 진행
    env.STATUS_PARTIALLY_COMPLETE: "\033[94m",
    env.STATUS_COMPLETE: "\033[32m",            # 초록: 완료
    env.STATUS_REJECTED: "\033[31m",            # 빨강: 거부·실패
    env.STATUS_RETRANSMIT_LATER: "\033[35m",    # 자주: 일시 거부
}
_C_ERROR = "\033[1;31m"           # 굵은 빨강: 해석 실패
_C_OTHER = "\033[2m"              # 흐리게: 처리기 없는 PDU

LEGEND = ("색상: " + _C_REQUEST + "요청" + RESET + "  응답 " + _C_STATUS[1] + "접수" + RESET
          + " " + _C_STATUS[2] + "진행" + RESET + " " + _C_STATUS[4] + "완료" + RESET
          + " " + _C_STATUS[5] + "거부" + RESET + " " + _C_STATUS[7] + "일시거부" + RESET)


def use_color(stream) -> bool:
    """터미널일 때만 색을 쓴다. 파일로 저장하거나 NO_COLOR가 설정되면 끈다."""
    return (hasattr(stream, "isatty") and stream.isatty()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")


def record_color(r: dict[str, Any]) -> str:
    if "decode_error" in r:
        return _C_ERROR
    if "payload_type" not in r:
        return _C_OTHER
    if "request_status" in r:
        return _C_STATUS.get(r["request_status"], "")
    return _C_REQUEST


def format_record(r: dict[str, Any], color: bool = False) -> str:
    """사람이 읽는 한 줄 요약 (모니터·재생 출력용). color=True면 메시지 종류별로 색을 칠한다."""
    line = _format_plain(r)
    c = record_color(r) if color else ""
    return f"{c}{line}{RESET}" if c else line


def _format_plain(r: dict[str, Any]) -> str:
    head = f"{r.get('t_rel', 0):8.3f}s {r.get('dir', ''):<18} {r.get('pdu_name', '?'):<18}"
    if "payload_type" not in r:
        return f"{head} ex={r.get('exercise_id')} {r.get('decode_error', '')}"
    status = f" status={r['request_status']}" if "request_status" in r else ""
    return (f"{head} ex={r['exercise_id']} {r['originating']}→{r['receiving']} "
            f"req={r['request_id']}{status} {r['payload_type']} {r['payload_body']}")


# ------------------------------------------------------------------ pcap

LINKTYPE_IPV4 = 228


def _ip_checksum(hdr: bytes) -> int:
    s = sum(struct.unpack(f">{len(hdr) // 2}H", hdr))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


class PcapWriter:
    def __init__(self, f: IO[bytes]):
        self.f = f
        self.ip_id = 0
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, LINKTYPE_IPV4))

    def write(self, t_epoch: float, src: tuple, dst: tuple, payload: bytes) -> None:
        udp = struct.pack(">HHHH", src[1], dst[1], 8 + len(payload), 0) + payload  # IPv4 UDP 체크섬 0 = 미사용
        self.ip_id = (self.ip_id + 1) & 0xFFFF
        ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), self.ip_id, 0, 64,
                         socket.IPPROTO_UDP, 0,
                         socket.inet_aton(src[0]), socket.inet_aton(dst[0]))
        ip = ip[:10] + struct.pack(">H", _ip_checksum(ip)) + ip[12:]
        pkt = ip + udp
        sec = int(t_epoch)
        self.f.write(struct.pack("<IIII", sec, int((t_epoch - sec) * 1e6), len(pkt), len(pkt)))
        self.f.write(pkt)
        self.f.flush()
