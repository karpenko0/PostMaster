# Проверка SPEC-014 — Reminder Publisher

## Объём и результат

Проверена реализация `ReminderPublisher`, контрактов `PublishResult`, `TelegramClient` и `BotTelegramClient`, метода `UserRepository.get_by_id`, интеграции с `PublicationService` (SPEC-013) и `SchedulerService`, а также сквозных сценариев доставки напоминания через `FakeTelegram`.

Полный прогон на Python 3.12.11: **396 passed**, без падений и пропусков. `ruff check .`, `ruff format --check .`, `git diff --check` — чисто.

Контроль чувствительности: 8 мутаций M1–M8 (отключение проверки `publication_type == REMINDER`, отключение проверки `telegram_file_id`, пропуск проверки `user is None`, перестановка порядка `send_message` и `send_photo`, игнорирование `caption`, подмена кода ошибки `MESSAGE_SEND_FAILED`, подмена `INVALID_FILE_ID`, изменение `REMINDER_TEXT`) обнаружены тестами `tests/test_spec014.py`.

## Матрица приёмки (AC-014-01 … AC-014-12)

| Критерий | Проверка | Статус |
|---|---|---|
| AC-014-01 — отправка в личный чат владельца | `test_tc_014_01_successful_reminder_sends_text_then_photo_without_creating_files`, `test_e2e_54_normal_reminder_flow_delivers_message_and_photo_and_marks_published` | ✅ |
| AC-014-02 — первым отправляется `REMINDER_TEXT` | `test_reminder_text_matches_spec014_verbatim`, `test_tc_014_01_successful_reminder_sends_text_then_photo_without_creating_files` | ✅ |
| AC-014-03 — вторым отправляется фотография | `test_tc_014_01_successful_reminder_sends_text_then_photo_without_creating_files`, `test_e2e_54_normal_reminder_flow_delivers_message_and_photo_and_marks_published` | ✅ |
| AC-014-04 — отправка по `telegram_file_id` без файлов на диске | `test_tc_014_01_successful_reminder_sends_text_then_photo_without_creating_files` (`list(tmp_path.iterdir()) == []`) | ✅ |
| AC-014-05 — передача `caption`, если он заполнен | `test_tc_014_05_caption_present_is_forwarded_to_send_photo_and_not_logged`, `test_e2e_55_missed_reminder_executes_after_downtime_and_marks_published` | ✅ |
| AC-014-06 — отправка без подписи при `caption=None` | `test_tc_014_06_caption_absent_sends_photo_without_caption` | ✅ |
| AC-014-07 — `chat_id` берётся из `users.telegram_user_id` | `test_it_014_01_02_03_user_repository_resolution_caption_and_status_ownership` | ✅ |
| AC-014-08 — отсутствие пользователя → `success=False`, `USER_NOT_FOUND` | `test_tc_014_03_user_missing_and_target_user_missing_do_not_call_telegram` | ✅ |
| AC-014-09 — отсутствие `telegram_file_id` → `success=False`, `FILE_ID_MISSING` | `test_tc_014_04_file_missing_fails_before_telegram_calls` | ✅ |
| AC-014-10 — успех दोनों вызовов → `PublishResult(success=True)` | `test_tc_014_01_successful_reminder_sends_text_then_photo_without_creating_files` | ✅ |
| AC-014-11 — ошибка текста или фото → `PublishResult(success=False)` | `test_unit_49_message_error_returns_failure_and_skips_photo`, `test_unit_50_photo_error_and_invalid_file_id_return_failure` | ✅ |
| AC-014-12 — `ReminderPublisher` не меняет `status` в БД самостоятельно | `test_it_014_01_02_03_user_repository_resolution_caption_and_status_ownership` | ✅ |

## Сверка тестов, ошибок, правил и инвариантов (§35–58)

| Раздел SPEC-014 | Доказательство | Статус |
|---|---|---|
| §35–37 Обработка blocked bot, invalid `file_id`, сетевых сбоев | `test_unit_50_photo_error_and_invalid_file_id_return_failure`, `test_it_014_53_publication_service_handles_permanent_and_temporary_reminder_errors`, `test_e2e_56_blocked_bot_returns_failure_and_marks_post_failed` | ✅ |
| §38–41 Логирование без утечки `caption` и `telegram_file_id` | `test_tc_014_01...`, `test_tc_014_05...`, `test_unit_49...` | ✅ |
| §42 Коды `ERR-014-01…06` и `INVALID_FILE_ID` | `test_tc_014_02…04`, `test_unit_49`, `test_unit_50` | ✅ |
| §48–50 Unit Tests `TC-014-01…06`, ошибка сообщения, ошибка фото | `test_tc_014_01…06`, `test_unit_49`, `test_unit_50` | ✅ |
| §51–53 Integration Tests `IT-014-01…03`, `PublicationService`, ошибки | `test_it_014_01_02_03...`, `test_it_014_52...`, `test_it_014_53...` | ✅ |
| §54–56 E2E-сценарии (обычное напоминание, missed reminder, blocked bot) | `test_e2e_54...`, `test_e2e_55...`, `test_e2e_56...` | ✅ |
| §57 Инварианты `INV-014-01…07` | Подтверждены модульными, интеграционными и сквозными тестами | ✅ |

## Границы scope (§4)

- Автопубликация в Telegram-канал (`TelegramPublisher`, SPEC-015), редактирование/отмена напоминаний (SPEC-019/020) и полноценный цикл повторных попыток по расписанию (SPEC-022) не входят в scope SPEC-014.

## Команды проверки

```bash
/tmp/venv312/bin/python -m pytest -q -p no:cacheprovider
/tmp/venv312/bin/ruff check .
/tmp/venv312/bin/ruff format --check .
git diff --check
```
