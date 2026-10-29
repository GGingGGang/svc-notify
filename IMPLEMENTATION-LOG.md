# 구현 기록

## 기능 및 요구사항 근거

- NTF-001: Telegram `/start` one-time challenge로 수신 소유를 증명하고 목적지 직접 입력을 허용하지 않는다. webhook은 private chat 및 `from.id == chat.id`만 처리한다. `test_telegram_start_webhook_rejects_group_and_non_owner`, `test_channel_ownership_and_telegram_challenge_are_one_time`, `test_legacy_tombstone_blocks_resurrection_and_migrates_to_user_lock`.
- NTF-002/010: 미확인·미동의 채널과 취소된 동의는 보내지 않는다. 조용한 시간은 auth의 시간대를 사용하고 자정 넘는 구간을 처리한다. `test_unverified_or_unconsented_channel_is_skipped`, `test_consent_revoked_before_provider_call_is_not_sent`, `test_reminder_during_dnd_is_durable_skip`, `test_explicit_test_notification_respects_dnd`.
- NTF-003/004/005/006/007: 일정 제목/현지 시작시간/시간대/열기 경로만 전송한다. 성공 상태는 provider receipt가 있을 때만 기록한다. 응답 유실은 `unknown`으로 종결해 재전송하지 않고, 429는 retryable, 4xx는 permanent로 분리한다. `test_reminder_has_only_minimal_schedule_content`, `test_provider_acceptance_requires_receipt`, `test_lost_provider_response_is_unknown_and_not_retryable`, `test_provider_errors_are_classified_without_raw_error_text`.
- NTF-008: 이력 쿼리는 30일 범위를 제한하며 기본 날짜 범위도 정확히 30일이다. `test_history_default_range_is_valid_30_days`; 실제 날짜 기반 조회/retention은 MySQL 환경에서 함께 수행한다.
- NTF-009: 테스트 알림은 명시 요청 및 idempotency key를 요구하고, per-user/channel 60초 DB 제한을 둔다. stale processing 30초 후 결과를 unknown으로 고정한다. `test_test_notification_is_rate_limited_per_user_channel`, `test_test_notification_in_progress_expires_to_unknown`; DND 동작은 위 service 테스트.
- NTF-011: 미설정 서비스는 liveness를 유지하고, DB/NATS readiness는 각각 확인하며 실제 provider 발송 기능은 disabled로 표시한다. 기존 일정 저장과 알림 장애를 분리한다.
- REM-003/006/007/012: core 및 batch 내부 인증 preflight로 일정 유효성 및 현재 dispatch attempt/generation을 검사한다. metadata-only 수정은 current core revision과 batch 최신 title을 반영하되 예정시각/channel 변경은 보내지 않는다. MySQL 동시 duplicate claim·delete race 및 NATS publish-failure/redelivery 통합 회귀 테스트가 CI에 있다. 로컬 mock 증거: `test_batch_preflight_checks_dispatch_attempt_and_auth`, `test_core_preflight_includes_current_revision_and_exact_remind_instant`, `test_metadata_only_title_change_uses_latest_batch_title`, `test_changed_reminder_generation_is_not_sent`.
- OPS001: `notification_attempts_total{outcome}`는 첫 durable attempt completion만 센다. `notification_dispatch_lag_seconds`는 가장 최근 처리된 due 시각의 지연을 보인다. 재발행은 별도 sent 횟수로 잡지 않는다. 프로세스 내 Prometheus 카운터는 재시작/DB commit 직후 crash에 대해 exactly-once를 보장하지 않으며, MySQL attempt ledger가 감사 원본이다. 식별자/목적지 label은 없다.

## 검사 결과

- `python -m pip install -e .` — 성공.
- `python -m unittest discover -s tests` — 50 tests 중 40 통과, 10개 통합 테스트는 로컬 MySQL/NATS 부재로 skip.
- `python -m compileall -q app tests` 및 `git diff --check` — 성공.
- `.github/workflows/test.yml`은 MySQL 8.4 및 NATS JetStream 서비스를 시작하고 `NOTIFY_MYSQL_TEST=1`, `NOTIFY_NATS_TEST=1`로 실제 MySQL transaction/concurrent duplicate/delete race와 NATS result publish-failure/redelivery 테스트를 실행한다. 이 Windows 작업환경에는 Docker가 없어 해당 통합 경로를 여기서 실행하지 못했다.
- 실제 Telegram provider 또는 실제 수신자에게 전송하지 않았다. provider 경계 테스트는 mock만 사용한다.

## 운영 선행 조건 및 미완료

- 운영은 `MYSQL_HOST/USER/PASSWORD/DATABASE`, TLS 검증, 32-byte URL-safe base64 `NOTIFY_ENCRYPTION_KEY`, `NATS_URL`, `INTERNAL_SERVICE_TOKEN`, auth/core/batch 내부 URL, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`, `PUBLIC_APP_URL`, `CORS_ALLOWED_ORIGINS` 설정을 요구한다.
- `GET /internal/users/{user_id}/channels`는 core가 telegram/verified/consent 여부를 확인하는 내부 API다. core의 telegram 선택지/일정 생성 검증과 배포 내부 URL/token 일치는 core 및 운영 설정 선행이다.
- 외부 Telegram webhook ingress 설정 및 운영 비밀값 생성/배포는 svc-notify 코드 범위 밖이며 실제 설정 전 provider 발송을 하지 않는다.
- ACC-009~013 / ACN-001~009 계정 이메일 인증/복구 메일은 auth 담당 구현 및 운영 SMTP 설정 선행이다.
