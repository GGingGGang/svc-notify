# 구현 기록

## 구현 범위

- NTF-001~011 / REM-003: Telegram 채널 연결은 일회성 `/start` 챌린지를 실제 Telegram webhook update로 증명한다. 채널 동의, 연결 해제, 조용한 시간대, 30일 발송 이력, 분당 테스트 알림 제한을 구현했다. `push`/`email` 이벤트는 Telegram으로 바꾸지 않고 `skipped/channel_unsupported`로 끝낸다.
- Telegram 목적지는 AES-GCM 암호문으로 저장한다. 삭제 tombstone은 암호화 키 회전에 영향을 받지 않는 UUID SHA-256 지문이다. 사용자 잠금 행으로 채널/설정/dispatch 쓰기와 계정 삭제를 직렬화하고, 삭제 이후 지연 due는 `cancelled/account_deleted`로 ACK할 수 있으며 개인 데이터 이력을 만들지 않는다.
- durable NATS 소비자는 reminder 결과를 저장한 뒤 result subject에 발행하고 ACK한다. 같은 dispatch attempt는 기존 상태를 재사용하며, 처리 중 worker가 30초 넘게 중단된 상태는 `unknown`으로 저장해 재전송하지 않는다. 결과에는 제한된 reason code만 포함한다.
- core 최신 validity 및 auth 계정 활성 상태를 호출해 발송 직전 검사한다. 내부 채널 확인 API `GET /internal/users/{user_id}/channels`는 `INTERNAL_SERVICE_TOKEN` bearer로 보호하고 검증·동의된 채널만 목적지 없이 반환한다.
- Docker 빌드에서 마이그레이션 파일을 패키지 내 `app/migrations` 경로로 읽는다. MySQL TLS 검증은 기본 필수이며, TLS 해제는 `APP_ENV=test`에서만 허용한다. 설정이 비어 있으면 delivery가 비활성화되지만 liveness는 유지되고 readiness는 DB/NATS를 실제 검사한다.
- `OPS001`: `notification_attempts_total{outcome}`는 첫 durable attempt completion에만 증가한다(outcome: accepted, retryable_failure, permanent_failure, unknown, skipped, cancelled). result 재발행은 이 카운터에 포함하지 않는다. 카운터는 프로세스 내 Prometheus 값이므로 재시작/DB commit 직후 crash의 경계에서 exactly-once 누적을 보장하지 않으며, 감사 가능한 원본은 MySQL attempt ledger다. 개인정보가 될 수 있는 ID/주소는 label에 사용하지 않는다.

## 확인

- `python -m pip install -e .` — 성공.
- `python -m unittest discover -s tests` — 성공, 24 tests. HTTP readiness/internal bearer, tracing, retry-safe channel/consent/preflight flow, deleted-user late due, unsupported channel, quiet-hours midnight wrap, opaque destination 암호화, provider 수락/응답 유실 분류 검증 포함.
- `python -m compileall -q app tests` — 성공.
- Telegram 실제 전송은 수행하지 않았다. provider adapter 결과와 서비스 흐름은 fake/mock으로 검사한다.
- 현재 개발 환경에 MySQL, NATS, Docker가 없어 마이그레이션의 실제 DB 실행, NATS 재연결/ACK crash window, 다중 프로세스 삭제 경합을 통합 검증하지 못했다. 운영 반영 전 MySQL 및 NATS 통합 테스트가 필요하다.

## 남은 요구사항 및 선행 조건

- ACC-009~013 / ACN-001~009 계정 이메일 인증 및 복구 메일은 auth 담당 구현·운영 SMTP 설정이 선행한다. 이 서비스에서는 이메일 목적지/메일 발송을 구현하지 않는다.
- REM-003 core 변경(telegram 채널 저장/검증)은 core 서비스 enum 및 일정 생성 요청 검증이 선행한다. notify 내부 capability URL과 서비스 bearer 계약은 구현했다.
- NTF 운영 활성화는 운영자가 Telegram bot token/username/webhook secret, 32-byte `NOTIFY_ENCRYPTION_KEY`, MySQL TLS 자격증명, NATS 연결, core/auth 내부 URL 및 `INTERNAL_SERVICE_TOKEN`을 설정하고 webhook 경로를 라우팅해야 한다. 설정 전 사용자 테스트 알림은 `channel_unavailable`로 실패 폐쇄된다.
