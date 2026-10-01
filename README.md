# SIMAN-R 핸드셰이크 프로토타입

`opcon_autonomy_comm_v8.docx`의 공통 봉투(4·5장)와 SIMAN-R 신뢰성 핸드셰이크(3.3절, 10.9절)를 Python 표준 라이브러리만으로 구현한 프로토타입.

| 파일 | 역할 |
|---|---|
| `siman_r/envelope.py` | Action Request-R(56) / Action Response-R(57) 인코딩·디코딩, Fixed Datum 3개 + PAYLOAD_BODY(JSON) |
| `siman_r/vehicle_server.py` | 차량 측 UDP 서버: Exercise ID·Receiving ID 필터, 중복 요청 제거(멱등성), 제어권, Pending→Executing→Complete |
| `siman_r/console_client.py` | 운용콘솔: Request ID 부여, 타임아웃·같은 ID로 재전송, 응답 짝짓기 |
| `tests/test_siman_r.py` | 바이트 배치(문서 4.12.2절 Hex 대조) + 손실·재전송·거부 시나리오 |

```bash
python3 -m unittest -v tests.test_siman_r
python3 -m siman_r.vehicle_server --port 3000 [--drop 0.3]   # 터미널 1
python3 -m siman_r.console_client --vehicle 127.0.0.1:3000   # 터미널 2
```

예시값(규약에서 확정 필요): Datum ID 500001~500004, Action ID 500100, 콘솔 2/1/1, 차량 1/3/1, Exercise 1, 타임아웃 1 s × 재시도 3회.

호환성 메모: Action Request-R 레이아웃은 Wireshark 디섹터·v8 문서와 일치한다. Open-DIS Python(`dis7.ActionRequestReliablePdu`)은 Action ID 뒤에 패딩 4 B를 추가로 읽어 디코딩이 실패하므로, 연동 시 이 차이를 확인할 것. Action Response-R은 Open-DIS로 정상 디코딩된다.
