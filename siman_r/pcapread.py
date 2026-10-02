"""pcap / pcapng 읽기 (표준 라이브러리만 사용).

Wireshark·tshark로 저장한 캡처(pcapng)와 기록 중계기가 만든 pcap에서 UDP 패킷을 꺼낸다.
IPv4 위 UDP만 다루며, 조각난(fragmented) IP 패킷은 건너뛴다.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import Iterator

# 링크 계층 종류 (LINKTYPE_*)
LT_NULL, LT_ETHERNET, LT_RAW, LT_LOOP = 0, 1, 101, 108
LT_LINUX_SLL, LT_IPV4, LT_LINUX_SLL2 = 113, 228, 276

PCAPNG_SHB = 0x0A0D0D0A
PCAPNG_IDB = 0x00000001
PCAPNG_EPB = 0x00000006


@dataclass
class UdpPacket:
    t_epoch: float
    src: tuple[str, int]
    dst: tuple[str, int]
    payload: bytes


def read_udp(path: str) -> Iterator[UdpPacket]:
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 4:
        return
    magic = data[:4]
    if struct.unpack("<I", magic)[0] == PCAPNG_SHB:
        frames = _pcapng_frames(data)
    elif magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        frames = _pcap_frames(data)
    else:
        raise ValueError(f"{path}: pcap/pcapng 파일이 아님")
    for t, linktype, frame in frames:
        pkt = _udp_from_frame(linktype, frame, t)
        if pkt is not None:
            yield pkt


def _pcap_frames(data: bytes):
    e = "<" if data[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    nano = data[:4] in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack_from(e + "I", data, 20)[0] & 0x0FFFFFFF
    off = 24
    while off + 16 <= len(data):
        sec, frac, incl, _ = struct.unpack_from(e + "IIII", data, off)
        off += 16
        yield sec + frac / (1e9 if nano else 1e6), linktype, data[off:off + incl]
        off += incl


def _pcapng_frames(data: bytes):
    off = 0
    e = "<"
    ifaces: list[tuple[int, float]] = []          # (linktype, 타임스탬프 단위 초)
    while off + 12 <= len(data):
        btype = struct.unpack_from(e + "I", data, off)[0]
        if btype == PCAPNG_SHB:
            bom = data[off + 8:off + 12]
            e = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            ifaces = []                            # 새 구역마다 인터페이스 번호가 다시 시작
        blen = struct.unpack_from(e + "I", data, off + 4)[0]
        if blen < 12 or off + blen > len(data):
            break
        body = data[off + 8:off + blen - 4]
        if btype == PCAPNG_IDB:
            linktype = struct.unpack_from(e + "H", body, 0)[0]
            ifaces.append((linktype, _if_tsresol(body[8:], e)))
        elif btype == PCAPNG_EPB:
            iface, hi, lo, cap = struct.unpack_from(e + "IIII", body, 0)
            if iface < len(ifaces):
                linktype, unit = ifaces[iface]
                yield ((hi << 32) | lo) * unit, linktype, body[20:20 + cap]
        off += blen


def _if_tsresol(opts: bytes, e: str) -> float:
    """IDB 옵션 if_tsresol(9). 없으면 마이크로초."""
    off = 0
    while off + 4 <= len(opts):
        code, ln = struct.unpack_from(e + "HH", opts, off)
        if code == 0:
            break
        if code == 9 and ln >= 1:
            v = opts[off + 4]
            return 2.0 ** -(v & 0x7F) if v & 0x80 else 10.0 ** -v
        off += 4 + ln + (-ln) % 4
    return 1e-6


def _udp_from_frame(linktype: int, f: bytes, t: float) -> UdpPacket | None:
    if linktype == LT_ETHERNET:
        if len(f) < 14:
            return None
        ethertype, off = struct.unpack_from(">H", f, 12)[0], 14
        while ethertype in (0x8100, 0x88A8) and len(f) >= off + 4:   # VLAN 태그
            ethertype, off = struct.unpack_from(">H", f, off + 2)[0], off + 4
        if ethertype != 0x0800:
            return None
        ip = f[off:]
    elif linktype == LT_LINUX_SLL:
        if len(f) < 16 or struct.unpack_from(">H", f, 14)[0] != 0x0800:
            return None
        ip = f[16:]
    elif linktype == LT_LINUX_SLL2:
        if len(f) < 20 or struct.unpack_from(">H", f, 0)[0] != 0x0800:
            return None
        ip = f[20:]
    elif linktype in (LT_NULL, LT_LOOP):
        ip = f[4:]
    elif linktype in (LT_RAW, LT_IPV4):
        ip = f
    else:
        return None
    if len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != socket.IPPROTO_UDP:
        return None
    ihl = (ip[0] & 0x0F) * 4
    if struct.unpack_from(">H", ip, 6)[0] & 0x3FFF:                  # MF 비트나 조각 오프셋
        return None
    total = struct.unpack_from(">H", ip, 2)[0]
    udp = ip[ihl:total]
    if len(udp) < 8:
        return None
    sport, dport, ulen = struct.unpack_from(">HHH", udp, 0)
    return UdpPacket(t, (socket.inet_ntoa(ip[12:16]), sport),
                     (socket.inet_ntoa(ip[16:20]), dport), udp[8:ulen])
