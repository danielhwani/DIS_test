# SIMAN-R 핸드셰이크 프로토타입

운용통제(상위 수준 시뮬레이터·운용콘솔)와 자율주행 모듈(하위 수준 시뮬레이터·실차 게이트웨이)을 DIS로 연동하기 위한 **공통 봉투와 명령-응답 핸드셰이크** 프로토타입이다. 설계 근거는 `opcon_autonomy_comm_v8.docx`(4·5장, 3.3절, 10.9절)이며, Python 표준 라이브러리만 사용한다 (Python 3.8 이상).

## 왜 이것부터 만들었나

VML(차량 관리), C-BML(임무), 시나리오·환경 관리 메시지는 모두 같은 DIS 봉투에 JSON으로 실려 같은 Request ID 핸드셰이크로 주고받는다 (설계 문서 3.1절). 이 층이 틀리면 그 위의 모든 메시지를 다시 만들어야 하므로, 페이로드 언어보다 봉투와 신뢰성 규칙을 먼저 검증한다.

## 프로토타입이 하는 일

### 1. DIS 봉투 인코딩·디코딩 (`siman_r/envelope.py`)

표준 DIS 7 PDU를 수정 없이 사용하고, 그 안의 Datum Record에 JSON 페이로드를 싣는다.

| 영역 | 내용 |
|---|---|
| 공통 헤더 (12 B) | DIS 7, Exercise ID, PDU Type, Protocol Family 10(SIMAN-R), 절대시간 타임스탬프 |
| PDU | Action Request-R(56): 송신·수신 Entity ID, Required Reliability Service, Request ID, Action ID<br>Action Response-R(57): 송신·수신 Entity ID, Request ID, Request Status |
| Fixed Datum 3개 | PAYLOAD_LANG(2 = VML), PAYLOAD_VERSION(1.0 = 256), PAYLOAD_ENCODING(1 = JSON) |
| Variable Datum 1개 | PAYLOAD_BODY = `{"type": 메시지명, "body": {...}}` |

- 모든 필드는 빅엔디언이다. Variable Datum 길이는 **비트 단위**로 넣고, Datum 값마다 64비트 경계까지 패딩한다 (설계 문서 4.12.1절).
- 수신 측은 헤더 12바이트만 읽고 PDU 종류를 판별한다. 처리기가 없는 PDU(Entity State 등)는 무시한다.

### 2. 차량 측 서버 (`siman_r/vehicle_server.py`)

실차 게이트웨이나 시뮬레이터 수신부 역할을 하는 UDP 서버다.

- **고정 포트에서 대기하고, 요청을 보낸 주소로 응답한다.** 콘솔 IP가 바뀌어도 차량 설정을 고칠 필요가 없다 (3.3절).
- **안전 격리:** 승인되지 않은 Exercise ID와, 다른 차량 앞으로 온(Receiving ID가 다른) PDU는 응답 없이 버린다 (8장). VML이 아닌 언어(예: 날씨 = 3)는 미지원으로 응답한다.
- **멱등성:** (콘솔 Entity ID, Request ID) 쌍으로 처리한 요청을 기억한다. 같은 요청이 다시 오면 재실행하지 않고 마지막으로 보낸 응답만 다시 보낸다. 진행 중인 명령의 기록은 최종 응답을 보낼 때까지 지우지 않고, 완료된 기록은 60초 뒤 정리한다.
- **제어권:** 한 차량에 명령할 수 있는 콘솔은 하나다. 다른 콘솔의 제어권 요청과, 제어권 없는 콘솔의 명령은 거부한다.
- **처리하는 VML 메시지**

| 요청 | 응답 | 동작 |
|---|---|---|
| `Request_Connection` | `Response_Connection` | 접속, 제어권 부여, 현재 모드·설정 버전·지원 메시지 목록(능력 정보) 응답 |
| `Command_AutonomousOperation` (START) | `Response_CommandResult` | 같은 Request ID로 Pending → Executing → Complete를 차례로 보냄 (경로 계획 2.5 s, 임무 5 s 모의) |
| `Command_AutonomousOperation` (STOP) | `Response_CommandResult` | 즉시 Complete |
| 그 밖의 메시지 | `Response_CommandResult` | UNSUPPORTED (Request Status 5) |

- **손실 모의:** `--drop` 옵션으로 응답 패킷을 일정 확률로 버려 무선 구간 손실을 흉내 낸다.

### 3. 운용콘솔 클라이언트 (`siman_r/console_client.py`)

- 응답을 기대하는 요청마다 Request ID를 붙인다. 카운터 시작값은 시각 기반이라 재시작해도 과거 번호와 잘 겹치지 않는다 (5.3절).
- **재전송:** 응답이 1초 안에 오지 않으면 같은 Request ID, 같은 바이트로 최대 3회 재전송한다. 그래도 응답이 없으면 포기한다.
- **상태 재질의:** 첫 응답(Pending 등)을 받은 뒤 최종 상태(Complete·Rejected)를 기다리는 동안, 차량에서 3초 동안 아무 응답이 없으면 같은 Request ID로 다시 보내 최신 상태를 묻는다. 응답이 오고 있으면 묻지 않는다. 차량은 이를 중복 요청으로 처리해 최신 응답만 돌려주므로, Executing·Complete 응답이 손실돼도 복구된다. 30초 안에 최종 상태가 오지 않으면 시간 초과로 처리한다.
- 자기 앞이 아닌 응답, 이미 포기한 Request ID의 늦은 응답, 같은 상태의 중복 응답은 무시한다.
- 단독 실행 시 `Request_Connection`으로 접속한 뒤 `Command_AutonomousOperation START`를 보내고 완료까지 기다린다.

### 교환 흐름

```
콘솔 (2/1/1)                                   차량 (1/3/1)
  │ Action Request-R  req=N  Request_Connection     │
  │ ───────────────────────────────────────────────▶│
  │ Action Response-R req=N  status 4 (Complete)     │  제어권 부여
  │◀─────────────────────────────────────────────── │
  │ Action Request-R  req=N+1  AutonomousOperation   │
  │ ───────────────────────────────────────────────▶│
  │ Action Response-R req=N+1  status 1 (Pending)    │
  │◀─────────────────────────────────────────────── │
  │ (3초간 응답 없으면 같은 req=N+1로 재질의) ─ ─ ─▶│  재실행 없이 최신 응답 재송신
  │ Action Response-R req=N+1  status 2 (Executing)  │
  │◀─────────────────────────────────────────────── │
  │ Action Response-R req=N+1  status 4 (Complete)   │
  │◀─────────────────────────────────────────────── │
```

응답이 손실되면 콘솔이 같은 Request ID로 다시 보내고, 차량은 다시 실행하지 않고 저장해 둔 응답을 돌려준다 (설계 문서 10.9절 예시 1·2).

### Request Status 매핑 (설계 문서 10.3절)

| JSON `result` | Request Status |
|---|---|
| ACCEPTED | 1 Pending |
| IN_PROGRESS | 2 Executing |
| COMPLETED | 4 Complete |
| DENIED / UNSUPPORTED / FAILED | 5 Request Rejected (`reason_code`로 사유 구분) |
| TEMPORARILY_REJECTED | 7 Retransmit Request Later |

## 실행

모든 명령은 프로젝트 폴더에서 실행한다 (다른 폴더에서는 `ModuleNotFoundError: No module named 'siman_r'`가 난다).

```bash
cd ~/DIS_test

# 테스트
python3 -m unittest -v tests.test_siman_r

# 터미널 1: 차량 서버 (--drop 0.3 = 응답 30% 손실 모의, Ctrl+C로 종료)
python3 -m siman_r.vehicle_server --port 3000 --drop 0.3

# 터미널 2: 콘솔
python3 -m siman_r.console_client --vehicle 127.0.0.1:3000

# (선택) 터미널 3: 두 번째 콘솔 → 제어권 거부 확인
python3 -m siman_r.console_client --vehicle 127.0.0.1:3000 --entity 2/1/2

# (선택) 실제 패킷 바이트 보기
sudo tcpdump -i lo -X udp port 3000
```

컴퓨터 한 대에서 무선 구간처럼 지연과 손실을 넣어 보려면 `tc netem`을 쓴다. 시험이 끝나면 반드시 해제한다 (켜 두는 동안 이 컴퓨터의 다른 내부 통신도 함께 느려진다).

```bash
sudo tc qdisc add dev lo root netem delay 150ms 50ms loss 15%
sudo tc qdisc del dev lo root
```

## 테스트가 확인하는 것 (`tests/test_siman_r.py`)

- 바이트 배치: 헤더, Entity ID, 신뢰성 필드, Fixed Datum이 설계 문서 4.12.2절의 Hex와 일치하는지, Datum 길이가 비트 단위이고 패딩이 맞는지
- 인코딩·디코딩 왕복(한글 포함), 모르는 PDU 무시, 길이 필드 불일치 거부, 전체 대상(0xFFFF) 주소 매칭
- 접속과 제어권 부여
- 응답 손실 → 같은 Request ID 재전송 → 재실행 없이 이전 응답 재송신 (10.9절 예시 1)
- 요청 손실 → 재전송
- Pending → Executing → Complete 진행 보고 (10.9절 예시 2)
- Executing·Complete 응답 손실 → 상태 재질의로 복구, 재실행 없음
- 응답이 재질의 주기보다 자주 오면 재질의를 보내지 않음
- 기록 유지 시간보다 오래 걸리는 명령도 재질의로 재실행되지 않음
- 제어권 없는 명령 거부, 두 번째 콘솔 제어권 거부, 모르는 메시지 UNSUPPORTED
- 승인되지 않은 Exercise ID는 응답 없이 버림 → 콘솔은 재시도 후 포기

## 아직 포함하지 않은 것

- 양방향 heartbeat와 통신 단절 판정, 단절 시 차량 안전 동작 (권장 다음 단계)
- 제어권 반납·이양 절차
- Set Data-R / Data-R(설정 명령), Event Report-R(이벤트 보고), Data PDU 1 Hz 주기 보고와 Data Query 구독
- Entity State 등 표준 PDU 송신
- 메시지 인증(HMAC)·암호화: 지금은 같은 네트워크의 누구나 명령을 보낼 수 있다
- C-BML, 시나리오·환경 관리 언어

## 확정이 필요한 값

모두 설명용 예시값이며 인터페이스 규약에서 확정한다.

| 항목 | 현재 값 |
|---|---|
| Datum ID | 500001(LANG) ~ 500004(BODY) |
| Action ID ("VML 메시지") | 500100 |
| Entity ID | 콘솔 2/1/1, 차량 1/3/1 |
| Exercise ID | 1 |
| 신뢰성 운영 파라미터 | 응답 대기 1 s × 재시도 3회, 상태 재질의(무응답 시) 3 s, 완료 대기 30 s, 중복 기록 유지 60 s |

완료 대기 30초는 설계 문서 예시의 260초짜리 임무에는 짧다. 명령 종류별로 정하거나 규약에서 확정해야 한다.

## 호환성 메모

Action Request-R 바이트 배치는 Wireshark DIS 디섹터, 설계 문서와 일치한다. Open-DIS Python(`dis7.ActionRequestReliablePdu`)은 Action ID 뒤에 패딩 4 B를 추가로 읽어 이 PDU를 디코딩하지 못하므로, 상대 체계가 Open-DIS를 쓴다면 이 차이를 확인한다. Action Response-R은 Open-DIS로 정상 디코딩된다.
