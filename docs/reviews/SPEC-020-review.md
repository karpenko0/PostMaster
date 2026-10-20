# Проверка SPEC-020 — Поддержка caption

## Объём и результат

Путь подписи подключён от Telegram `message.caption` через `PhotoData`/FSM draft и `DialogService` до `PostService`/Repository. Captions сохраняются и читаются без изменений. `None`, пустая строка и строка из пробелов нормализуются в `None` и SQL `NULL`. ReminderPublisher и TelegramPublisher передают непустую подпись без изменений; для отсутствующей подписи `send_photo` вызывается без параметра `caption`. Пользователь получает инструкцию приложить подпись к фото. Обновлена документация SPEC-004, отражающая расширение PhotoData/FSM.

## Проверки

- `tests/test_spec020.py`: **8 passed**.
- Связанные тесты фото/диалога/repository/publishers: **155 passed**.
- Полный набор, кроме проверки версии среды: **480 passed, 1 deselected**.
- `ruff check .`: чисто.
- `ruff format --check .`: чисто (85 файлов).
- `git diff --check`: чисто.

Sandbox использует Python 3.11.2, проект требует Python 3.12+. Поэтому `tests/test_smoke.py::test_python_is_312_or_newer` исключён. В полном прогоне присутствовало предупреждение aiosqlite о thread/event-loop shutdown в одном существующем тесте; сами тесты завершились успешно.

## Матрица приёмки

| Критерий | Проверка | Статус |
|---|---|---|
| AC-020-01…04 — NULL/непустой caption сохраняется и round-trip | `test_tc_020_01_02_rt_020_01_02_caption_round_trips_and_empty_is_null`; E2E | ✅ |
| AC-020-05 — ReminderPublisher получает исходный caption | `test_tc_020_03_04_publishers_forward_caption_exactly` | ✅ |
| AC-020-06 — TelegramPublisher получает исходный caption | `test_tc_020_03_04_publishers_forward_caption_exactly` | ✅ |
| AC-020-07 — отсутствие/пустой caption передаётся без keyword | `test_tc_020_05_both_publishers_send_photo_without_empty_caption` | ✅ |
| AC-020-08 — caption отправляется вместе с фото | publisher call assertions и Telegram-photo E2E | ✅ |
| AC-020-09 — текст не изменяется | multiline/Unicode/whitespace-preservation assertions и Repository round-trip | ✅ |
| Пустая подпись не блокирует публикацию | `None`, `""`, whitespace tests и blank-caption E2E | ✅ |
