# Проверка SPEC-015 — Telegram Publisher

## Объём и результат

Проверена реализация `TelegramPublisher`, перечисления `TelegramErrorCode` (`ERR-015-01 … ERR-015-10`), контракта `TelegramClient` (`chat_id: str | int`), интеграции с `PublicationService` и `SchedulerService`, а также сквозных сценариев автопубликации в Telegram-канал через `FakeTelegram`.

Полный прогон на Python 3.12.11: **425 passed**, без падений и пропусков. `ruff check .`, `ruff format --check .`, `git diff --check` — чисто.

Контроль чувствительности: 8 мутаций M1–M8 (отключение проверки `publication_type == TELEGRAM`, отключение проверки `status == PROCESSING`, пропуск проверки `target_id`, пропуск проверки `telegram_file_id`, игнорирование `caption`, пометка ошибки прав как `retryable=True`, пометка таймаута как `retryable=False`, подмена `INVALID_FILE_ID` на `TELEGRAM_TARGET_UNAVAILABLE`) обнаружены тестами `tests/test_spec015.py`.

## Матрица приёмки (AC-015-01 … AC-015-15)

| Критерий | Проверка | Статус |
|---|---|---|
| AC-015-01 — `publication_type=TELEGRAM`, `status=PROCESSING` запускает flow | `test_tc_015_01_successful_publish_sends_photo_to_target_without_local_files` | ✅ |
| AC-015-02 — автоматическая публикация в `target_id` | `test_tc_015_01...`, `test_e2e_59_automatic_channel_publication_from_dialog_to_published` | ✅ |
| AC-015-03 — используется `post.telegram_file_id` | `test_tc_015_01...`, `test_it_015_01_02_03_04_database_post_and_status_ownership` | ✅ |
| AC-015-04 — локальный файл не требуется | `test_tc_015_01...` (`list(tmp_path.iterdir()) == []`) | ✅ |
| AC-015-05 — передача `caption`, если он заполнен | `test_tc_015_02_caption_is_passed_to_send_photo`, `test_it_015_01_02_03_04...` | ✅ |
| AC-015-06 — публикация без `caption` при `caption=None` | `test_tc_015_03_no_caption_sends_photo_without_caption` | ✅ |
| AC-015-07 — `target_id=None` → API не вызывается, `TARGET_REQUIRED` | `test_tc_015_04_missing_target_returns_target_required_without_calling_api` | ✅ |
| AC-015-08 — `telegram_file_id=None` → `FILE_ID_MISSING` | `test_tc_015_05_missing_file_returns_file_id_missing_without_calling_api` | ✅ |
| AC-015-09 — `publication_type=REMINDER` → API не вызывается, `INVALID_PUBLICATION_TYPE` | `test_tc_015_06_wrong_type_does_not_call_telegram` | ✅ |
| AC-015-10 — `status != PROCESSING` → `INVALID_POST_STATUS` | `test_tc_015_07_wrong_status_rejects_direct_publication` | ✅ |
| AC-015-11 — ошибка прав бота → `success=False`, `TELEGRAM_PERMISSION_DENIED` | `test_unit_52_permission_denied_returns_telegram_permission_denied`, `test_e2e_62_bot_lost_permissions_marks_failed_and_never_published` | ✅ |
| AC-015-12 — недоступный канал → `success=False`, `TELEGRAM_TARGET_UNAVAILABLE` | `test_unit_54_invalid_file_id_and_target_unavailable_errors`, `test_e2e_63_invalid_target_channel_marks_failed` | ✅ |
| AC-015-13 — таймаут API возвращается как ошибка (`TELEGRAM_API_TIMEOUT`, `retryable=True`) | `test_unit_53_timeout_and_network_errors_are_marked_retryable`, `test_it_015_57_publication_service_handles_permission_and_timeout_errors` | ✅ |
| AC-015-14 — успешная отправка → `PublishResult(success=True)` | `test_tc_015_01...`, `test_it_015_56...` | ✅ |
| AC-015-15 — финальный статус меняется только в `PublicationService` | `test_it_015_01_02_03_04_database_post_and_status_ownership` | ✅ |

## Сверка тестов, ошибок, правил и инвариантов (§34–65)

| Раздел SPEC-015 | Доказательство | Статус |
|---|---|---|
| §34–38 Коды ошибок `ERR-015-01…10` | `test_telegram_publisher_contracts_and_spec015_error_codes`, `test_tc_015_04…07`, `test_unit_52…54` | ✅ |
| §46–49 Логирование и маскировка секретов | `test_tc_015_01...`, `test_tc_015_02...`, `test_unit_53...` | ✅ |
| §51–54 Unit Tests `TC-015-01…07`, permission, timeout, invalid `file_id` | `test_tc_015_01…07`, `test_unit_52…54` | ✅ |
| §55–58 Integration Tests `IT-015-01…04`, `PublicationService`, ошибки, `SPEC-013` | `test_it_015_01_02_03_04...`, `test_it_015_56...`, `test_it_015_57...`, `test_it_015_58...` | ✅ |
| §59–63 E2E-сценарии (автопубликация, Recovery, Missed Job, потеря прав, invalid target) | `test_e2e_59...`, `test_e2e_60...`, `test_e2e_61...`, `test_e2e_62...`, `test_e2e_63...` | ✅ |
| §64 Инварианты `INV-015-01…09` | Подтверждены модульными, интеграционными и сквозными тестами | ✅ |

## Границы scope (§4)

- Единый резолвер / абстракция `Publisher` (SPEC-016), ввод `caption` пользователем в диалоге (SPEC-020) и полноценное расписание повторных попыток (SPEC-022) не входят в scope SPEC-015.

## Команды проверки

```bash
/tmp/venv312/bin/python -m pytest -q -p no:cacheprovider
/tmp/venv312/bin/ruff check .
/tmp/venv312/bin/ruff format --check .
git diff --check
```
