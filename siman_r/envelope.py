"""DIS 공통 봉투 (opcon_autonomy_comm_v8 4장·5장).

SIMAN-R Action Request-R(56) / Action Response-R(57) PDU와 기본 SIMAN Data PDU(20)에
Fixed Datum 3개(PAYLOAD_LANG/VERSION/ENCODING) + Variable Datum 1개(PAYLOAD_BODY, JSON)를
싣고 푸는 코드. 모든 필드는 빅엔디언 (IEEE 1278.1-2012).
"""
from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 7          # DIS 7
FAMILY_SIMAN = 5              # Simulation Management
FAMILY_SIMAN_R = 10           # Simulation Management with Reliability

PDU_DATA = 20                 # 확인 없는 주기 보고·heartbeat (4.12절)

PDU_ACTION_REQUEST_R = 56
PDU_ACTION_RESPONSE_R = 57

# Required Reliability Service
RELIABILITY_ACKED = 0         # 확인 요구
RELIABILITY_UNACKED = 1       # 확인 불요

# Request Status (10.3절 매핑, SISO-REF-010 기준 최종 확인 필요)
STATUS_OTHER = 0
STATUS_PENDING = 1
STATUS_EXECUTING = 2
STATUS_PARTIALLY_COMPLETE = 3
STATUS_COMPLETE = 4
STATUS_REJECTED = 5
STATUS_RETRANSMIT_LATER = 7
FINAL_STATUSES = {STATUS_COMPLETE, STATUS_REJECTED, STATUS_RETRANSMIT_LATER}

RESULT_TO_STATUS = {
    "ACCEPTED": STATUS_PENDING,
    "IN_PROGRESS": STATUS_EXECUTING,
    "COMPLETED": STATUS_COMPLETE,
    "TEMPORARILY_REJECTED": STATUS_RETRANSMIT_LATER,
    "DENIED": STATUS_REJECTED,
    "UNSUPPORTED": STATUS_REJECTED,
    "FAILED": STATUS_REJECTED,
}

# 사용자 정의 값 (5.2절, 10.2절). 설명용 예시값이며 인터페이스 규약에서 확정한다.
DATUM_PAYLOAD_LANG = 500001
DATUM_PAYLOAD_VERSION = 500002
DATUM_PAYLOAD_ENCODING = 500003
DATUM_PAYLOAD_BODY = 500004
ACTION_ID_VML_MESSAGE = 500100

LANG_CBML, LANG_VML, LANG_ENV = 1, 2, 3
ENCODING_JSON = 1

HEADER_FMT = ">BBBBIHBx"      # 12 B
ENTITY_FMT = ">HHH"           # 6 B
MAX_PDU_SIZE = 8192
ALL = 0xFFFF


class DecodeError(ValueError):
    pass


@dataclass(frozen=True)
class EntityId:
    site: int
    app: int
    entity: int

    def pack(self) -> bytes:
        return struct.pack(ENTITY_FMT, self.site, self.app, self.entity)

    @classmethod
    def parse(cls, s: str) -> "EntityId":
        site, app, ent = (int(x) for x in s.split("/"))
        return cls(site, app, ent)

    def matches(self, other: "EntityId") -> bool:
        """수신 ID 비교. 0xFFFF(전체)는 와일드카드."""
        return all(a == b or a == ALL
                   for a, b in zip((self.site, self.app, self.entity),
                                   (other.site, other.app, other.entity)))

    def __str__(self) -> str:
        return f"{self.site}/{self.app}/{self.entity}"


def dis_timestamp(now: float | None = None) -> int:
    """절대시간 모드 타임스탬프 (시각 기준 경과 시간, 단위 3600/2^31 s, LSB=1)."""
    now = time.time() if now is None else now
    units = int((now % 3600.0) / 3600.0 * (1 << 31))
    return (units << 1) | 1


@dataclass
class Payload:
    """PAYLOAD_LANG/VERSION/ENCODING + PAYLOAD_BODY ({"type":..., "body":...})."""
    type: str
    body: dict[str, Any] = field(default_factory=dict)
    lang: int = LANG_VML
    version: int = 0x0100         # major*256 + minor (1.0)
    encoding: int = ENCODING_JSON

    def to_json_bytes(self) -> bytes:
        return json.dumps({"type": self.type, "body": self.body},
                          ensure_ascii=False, separators=(",", ":")).encode()


@dataclass
class ActionRequestR:
    exercise_id: int
    originating: EntityId
    receiving: EntityId
    request_id: int
    payload: Payload
    reliability: int = RELIABILITY_ACKED
    action_id: int = ACTION_ID_VML_MESSAGE
    timestamp: int = 0
    pdu_type: int = PDU_ACTION_REQUEST_R


@dataclass
class ActionResponseR:
    exercise_id: int
    originating: EntityId
    receiving: EntityId
    request_id: int
    request_status: int
    payload: Payload
    timestamp: int = 0
    pdu_type: int = PDU_ACTION_RESPONSE_R


# ---------------------------------------------------------------- 인코딩

def _datum_records(p: Payload) -> bytes:
    fixed = struct.pack(">IIIIII",
                        DATUM_PAYLOAD_LANG, p.lang,
                        DATUM_PAYLOAD_VERSION, p.version,
                        DATUM_PAYLOAD_ENCODING, p.encoding)
    body = p.to_json_bytes()
    pad = (-len(body)) % 8                      # Variable Datum마다 64비트 경계 패딩
    # Datum Length는 바이트가 아니라 "비트" 단위 (4.12.1절)
    var = struct.pack(">II", DATUM_PAYLOAD_BODY, len(body) * 8) + body + b"\x00" * pad
    return struct.pack(">II", 3, 1) + fixed + var


@dataclass
class DataPdu:
    """기본 SIMAN Data PDU (4.12.1절). 자발 송신이면 request_id = 0."""
    exercise_id: int
    originating: EntityId
    receiving: EntityId
    payload: Payload
    request_id: int = 0
    timestamp: int = 0
    pdu_type: int = PDU_DATA


def _header(pdu_type: int, exercise_id: int, timestamp: int, length: int,
            family: int = FAMILY_SIMAN_R) -> bytes:
    return struct.pack(HEADER_FMT, PROTOCOL_VERSION, exercise_id, pdu_type,
                       family, timestamp, length, 0)


def encode(pdu: ActionRequestR | ActionResponseR | DataPdu) -> bytes:
    ts = pdu.timestamp or dis_timestamp()
    family = FAMILY_SIMAN_R
    if isinstance(pdu, DataPdu):
        family = FAMILY_SIMAN
        fields = (pdu.originating.pack() + pdu.receiving.pack()
                  + struct.pack(">I4x", pdu.request_id))
    elif isinstance(pdu, ActionRequestR):
        fields = (pdu.originating.pack() + pdu.receiving.pack()
                  + struct.pack(">B3xII", pdu.reliability, pdu.request_id, pdu.action_id))
    else:
        fields = (pdu.originating.pack() + pdu.receiving.pack()
                  + struct.pack(">II", pdu.request_id, pdu.request_status))
    rest = fields + _datum_records(pdu.payload)
    length = 12 + len(rest)
    if length > MAX_PDU_SIZE:
        raise ValueError(f"PDU {length} B > {MAX_PDU_SIZE} B")
    return _header(pdu.pdu_type, pdu.exercise_id, ts, length, family) + rest


# ---------------------------------------------------------------- 디코딩

@dataclass
class Header:
    version: int
    exercise_id: int
    pdu_type: int
    family: int
    timestamp: int
    length: int


def peek_header(data: bytes) -> Header:
    """1단계 식별: 헤더 12바이트만으로 PDU 종류 판별 (5.1절)."""
    if len(data) < 12:
        raise DecodeError("short header")
    v, ex, t, fam, ts, ln, _ = struct.unpack_from(HEADER_FMT, data)
    if ln != len(data):
        raise DecodeError(f"length field {ln} != datagram {len(data)}")
    return Header(v, ex, t, fam, ts, ln)


def _decode_datums(data: bytes, off: int) -> Payload:
    n_fixed, n_var = struct.unpack_from(">II", data, off)
    off += 8
    fixed: dict[int, int] = {}
    for _ in range(n_fixed):
        did, val = struct.unpack_from(">II", data, off)
        fixed[did] = val
        off += 8
    body_bytes = None
    for _ in range(n_var):
        did, nbits = struct.unpack_from(">II", data, off)
        off += 8
        nbytes = (nbits + 7) // 8
        value = data[off:off + nbytes]
        off += nbytes + (-nbytes) % 8
        if did == DATUM_PAYLOAD_BODY:
            body_bytes = value
    if off > len(data):
        raise DecodeError("datum records overrun PDU")
    if body_bytes is None:
        raise DecodeError("no PAYLOAD_BODY datum")
    enc = fixed.get(DATUM_PAYLOAD_ENCODING, ENCODING_JSON)
    if enc != ENCODING_JSON:
        raise DecodeError(f"unsupported PAYLOAD_ENCODING {enc}")
    try:
        obj = json.loads(body_bytes)
        return Payload(type=obj["type"], body=obj.get("body", {}),
                       lang=fixed.get(DATUM_PAYLOAD_LANG, 0),
                       version=fixed.get(DATUM_PAYLOAD_VERSION, 0),
                       encoding=enc)
    except (ValueError, KeyError, TypeError) as e:
        raise DecodeError(f"bad JSON payload: {e}") from e


def decode(data: bytes) -> ActionRequestR | ActionResponseR | DataPdu | None:
    """처리기가 있는 PDU만 디코딩하고, 모르는 PDU는 None (4.3절: 무시)."""
    h = peek_header(data)
    if h.version != PROTOCOL_VERSION or (h.family, h.pdu_type) not in (
            (FAMILY_SIMAN_R, PDU_ACTION_REQUEST_R), (FAMILY_SIMAN_R, PDU_ACTION_RESPONSE_R),
            (FAMILY_SIMAN, PDU_DATA)):
        return None
    try:
        orig = EntityId(*struct.unpack_from(ENTITY_FMT, data, 12))
        recv = EntityId(*struct.unpack_from(ENTITY_FMT, data, 18))
        if h.pdu_type == PDU_DATA:
            (req_id,) = struct.unpack_from(">I", data, 24)
            return DataPdu(h.exercise_id, orig, recv, _decode_datums(data, 32), req_id,
                           h.timestamp)
        if h.pdu_type == PDU_ACTION_REQUEST_R:
            rel, req_id, action_id = struct.unpack_from(">B3xII", data, 24)
            return ActionRequestR(h.exercise_id, orig, recv, req_id,
                                  _decode_datums(data, 36), rel, action_id, h.timestamp)
        if h.pdu_type == PDU_ACTION_RESPONSE_R:
            req_id, status = struct.unpack_from(">II", data, 24)
            return ActionResponseR(h.exercise_id, orig, recv, req_id, status,
                                   _decode_datums(data, 32), h.timestamp)
    except struct.error as e:
        raise DecodeError(str(e)) from e
    return None
