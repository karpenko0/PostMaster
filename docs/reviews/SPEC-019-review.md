# Проверка SPEC-019 — Команда `/help`

## Объём и результат

Добавлен `/help` с инструкцией по созданию публикации, актуальным шаблоном даты, примером, описанием режимов Reminder/Telegram и всеми текущими командами MVP. Handler зарегистрирован до generic text handler и не обращается к FSM, сервисам диалога, БД или Scheduler. Сбой Telegram send обрабатывается логированием с ID пользователя и типом исключения.

## Проверки

- `tests/test_spec019.py`: **12 passed**.
- Полный набор, кроме проверки версии среды: **472 passed, 1 deselected**.
- `ruff check .`: чисто.
- `ruff format --check .`: чисто (84 файла).
- `git diff --check`: чисто.

Sandbox использует Python 3.11.2, проект требует Python 3.12+. Поэтому `tests/test_smoke.py::test_python_is_312_or_newer` исключён из полного прогона; остальной набор и все SPEC-019 тесты прошли.

## Матрица приёмки

| Критерий | Проверка | Статус |
|---|---|---|
| AC-019-01 — `/help` отправляет текст | `test_help_works_in_idle_without_database_or_scheduler_changes` | ✅ |
| AC-019-02…04 — команды, формат и пример | `test_tc_019_01_02_help_text_has_commands_date_example_and_modes` | ✅ |
| AC-019-05 — работает в IDLE | `test_help_works_in_idle_without_database_or_scheduler_changes` | ✅ |
| AC-019-06 — работает в WAITING_PHOTO | `test_help_preserves_waiting_photo_state` | ✅ |
| AC-019-07…09 — WAITING_DATETIME, состояние и фото-данные сохраняются | `test_help_preserves_photo_and_waiting_datetime_then_flow_continues` | ✅ |
| AC-019-10 — нет изменений БД/Scheduler и после help сценарий продолжается | DB/Scheduler snapshots и продолжение до создания одной публикации в E2E | ✅ |
| ERR-019-01 — ошибка отправки логируется без деталей исключения | `test_help_send_failure_is_logged_without_fsm_access` | ✅ |
