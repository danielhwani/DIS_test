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

# 테스트 (tests/ 아래 전부)
python3 -m unittest -v

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

## 기록과 재생 (`siman_r/recorder.py`, `player.py`, `monitor.py`, `pcapread.py`)

콘솔과 차량 사이에 **기록 중계기**를 두고, 오가는 PDU를 양방향 모두 저장한다. 설계 문서 11장의 게이트웨이 위치 로거에 해당한다. 콘솔·차량 코드는 바꾸지 않으므로 나중에 UE5나 소대 시뮬레이션처럼 코드를 고칠 수 없는 체계 사이에도 그대로 끼울 수 있다.

```
콘솔 ──▶ 기록 중계기(:3001) ──▶ 차량(:3000)
콘솔 ◀── 기록 중계기        ◀── 차량
              │
              ├─ recordings/session_<시각>.jsonl   원본 바이트 + 해석 필드 (한 줄 = 한 패킷)
              └─ recordings/session_<시각>.pcap    Wireshark로 바로 열람
```

- **JSONL 한 줄:** 기록 시작 후 경과 시간(`t_rel`), UTC 시각, 방향, 실제 송수신 주소, 헤더 필드(Exercise ID, PDU 종류), SIMAN-R 필드(Entity ID, Request ID, Request Status), JSON 페이로드, 원본 바이트(`raw`, hex). 해석 필드는 원본 바이트에서 언제든 다시 만들 수 있으며, 재생은 원본 바이트만 사용한다 (설계 문서 11.1절의 원본 층·해석 층).
- **pcap:** 이더넷 없이 IPv4/UDP 헤더만 붙인다(LINKTYPE_IPV4). 주소는 중계기가 아니라 실제 콘솔·차량 주소로 남긴다. Wireshark는 UDP 3000번을 DIS로 해석하므로, 차량 포트가 다르면 "Decode As → DIS"로 지정한다.
- **재생(playback):** 원본 바이트를 원래 시간 간격대로 다시 송출한다. 중계기의 `.jsonl`·`.pcap`뿐 아니라 Wireshark·tshark로 저장한 `.pcapng`도 재생할 수 있다 (아래 "Wireshark 캡처"). 재생한 명령이 실차에 닿지 않도록 **Exercise ID를 99로 바꿔** 보내고, 기록과 같은 Exercise ID는 지정할 수 없다. 꼭 그대로 보내야 하면 `--keep-exercise`를 명시한다 (설계 문서 11.4절).
- **모니터:** 받은 PDU를 해석해 한 줄씩 출력하는 수신기. 재생 결과를 눈으로 확인하는 용도.

```bash
# 녹화: 터미널 3개
python3 -m siman_r.vehicle_server --port 3000 --drop 0.3          # 터미널 1: 차량
python3 -m siman_r.recorder --listen 3001 --vehicle 127.0.0.1:3000 # 터미널 2: 기록 중계기 (Ctrl+C로 종료·저장)
python3 -m siman_r.console_client --vehicle 127.0.0.1:3001         # 터미널 3: 콘솔은 중계기(3001)로 보낸다

# 재생
python3 -m siman_r.player recordings/session_<시각>.jsonl --print --speed 0   # 화면에 타임라인만
python3 -m siman_r.monitor --port 4000                                        # 수신기 (다른 터미널)
python3 -m siman_r.player recordings/session_<시각>.jsonl --target 127.0.0.1:4000 --speed 2
python3 -m siman_r.player recordings/session_<시각>.jsonl --target 127.0.0.1:3000 --from console
#   ↑ 차량으로 보내면 Exercise ID가 달라 "✗ Exercise ID 99 거부"로 버려진다 (안전 격리 확인)
```

`--from console`은 콘솔이 보낸 요청만, `--from vehicle`은 차량이 보낸 응답만 재생한다. `--speed 0`은 기다리지 않고 바로 보낸다.

**색상:** 중계기·재생기·모니터는 메시지 종류별로 줄 색을 바꾼다. 모니터는 방향을 모르므로 방향이 아니라 종류로 구분한다.

| 색 | 메시지 |
|---|---|
| 청록 | 요청 (Action Request-R: 접속, 명령, 재전송, 상태 재질의) |
| 노랑 / 파랑 / 초록 | 응답: 접수(Pending) / 진행(Executing) / 완료(Complete) |
| 빨강 / 자주 | 응답: 거부·실패(Rejected) / 일시 거부(Retransmit Later) |
| 굵은 빨강 | 해석 실패한 패킷 |

터미널에 출력할 때만 색을 쓰고, 파일로 저장하거나 파이프로 넘길 때와 `NO_COLOR` 환경 변수가 있을 때는 끈다. `--no-color`로 직접 끌 수도 있다.

### Wireshark 캡처

**실시간 캡처 준비 (처음 한 번):** `sudo apt install wireshark tshark`로 설치하고, 설치 중 "Should non-superusers be able to capture packets?"에 Yes를 고른 뒤 `sudo usermod -aG wireshark $USER`를 실행하고 **다시 로그인**한다. 다시 로그인하기 전에 GUI로 캡처하면 `Couldn't run /usr/bin/dumpcap in child process: Permission denied`가 난다 (`id -nG`에 `wireshark`가 있는지 확인).

**GUI로 캡처:** `wireshark &` → 시작 화면 Capture 영역의 `...using this filter:` 칸에 `udp port 3000` 입력 → 인터페이스 목록에서 `Loopback: lo` 더블클릭 → 위쪽 표시 필터 칸에 `dis` 입력. 그다음 차량과 콘솔을 실행한다. 캡처 필터를 비워 두면 DNS 등 다른 패킷도 파일에 함께 저장된다 (표시 필터 `dis`는 화면에서만 거른다).

**터미널로 캡처:** `tshark -i lo -f "udp port 3000"` (저장하려면 `-w recordings/x.pcapng`)

**캡처 파일 재생:** 재생기가 파일 내용을 보고 JSONL·pcap·pcapng를 구분하므로 옵션은 같다.

```bash
python3 -m siman_r.player recordings/gui_capture_test.pcapng --print
python3 -m siman_r.player recordings/gui_capture_test.pcapng --target 127.0.0.1:4000 --speed 2
```

- 캡처 파일에는 방향 정보가 없으므로 **차량 포트(기본 3000)**로 판단한다. 목적지가 3000이면 `console->vehicle`, 출발지가 3000이면 `vehicle->console`이고, 3000을 지나지 않는 패킷(DNS 등)은 버린다. 중계기를 끼우고 3000-3001을 함께 캡처해도 콘솔↔중계기 구간은 빠지므로 같은 패킷이 두 번 재생되지 않는다.
- 차량을 다른 포트로 띄웠다면 `--vehicle-port 3002`처럼 지정한다.
- 지원 형식: pcap(마이크로·나노초), pcapng(시각 단위 옵션 포함), 링크 계층 Ethernet(VLAN 포함)·Linux cooked(SLL/SLL2)·loopback·raw IPv4. IPv4 위 UDP만 다루고, 조각난 IP 패킷은 건너뛴다.
- 재생한 패킷을 Wireshark로 다시 보려면 4000번처럼 DIS 기본 포트가 아닌 곳은 패킷 오른쪽 클릭 → **Decode As...** → `DIS`로 지정한다.

한계: 차량 서버의 `--drop`은 차량이 보내기 전에 패킷을 버리므로, 그 손실은 중계기 기록에 나타나지 않는다(보낸 적이 없는 패킷). 기록에서는 응답이 빠진 자리와 콘솔의 재전송·재질의로 드러난다. 실제 무선 손실을 기록으로 보려면 양 끝단에서도 기록해야 한다 (설계 문서 11.2절).

## 테스트가 확인하는 것

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
- (`tests/test_recording.py`) pcap 헤더·IP 체크섬·UDP 포트·페이로드 배치
- 중계기를 거쳐도 핸드셰이크가 정상이고, 양방향 패킷이 시간 순서대로 모두 기록되며, 해석 필드를 원본 바이트에서 다시 만들 수 있음
- 메시지 종류별 색상 규칙, 기본값은 색 없음
- 재생 시 Exercise ID만 바뀌고 나머지 바이트는 원본과 같음, 차량으로 재생한 명령은 실행되지 않음
- pcapng(마이크로초·나노초 시각 단위)와 중계기 pcap 읽기, 차량 포트로 방향 판단, 다른 포트·잡음 패킷 제외, `--vehicle-port`

## 아직 포함하지 않은 것

- 양방향 heartbeat와 통신 단절 판정, 단절 시 차량 안전 동작 (권장 다음 단계)
- 제어권 반납·이양 절차
- Set Data-R / Data-R(설정 명령), Event Report-R(이벤트 보고), Data PDU 1 Hz 주기 보고와 Data Query 구독
- Entity State 등 표준 PDU 송신
- 메시지 인증(HMAC)·암호화: 지금은 같은 네트워크의 누구나 명령을 보낼 수 있다
- C-BML, 시나리오·환경 관리 언어
- 재실행(re-execution): 기록에서 명령만 뽑아 시뮬레이터에 새로 입력하는 도구. 지금 재생기에 `--keep-exercise`를 주어 차량으로 보내면, 차량이 그 Request ID를 기억하는 동안(완료 후 60 s)은 중복으로 보고 실행하지 않지만, 차량을 재시작한 뒤라면 **실제로 다시 실행된다**
- Parquet 변환과 DuckDB 분석 (JSONL은 DuckDB `read_json`으로 바로 읽을 수 있음)

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
