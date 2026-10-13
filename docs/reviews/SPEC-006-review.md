# Приёмка SPEC-006 — часовые зоны

## Итог

Timezone-подсистема, разрешение пояса с fallback, local↔UTC, проверка будущего времени в UTC, передача timezone+UTC в сервисный контракт и обратный форматированный показ в существующем сценарии реализованы. **Полная приёмка SPEC-006 пока невозможна**: таблица `posts` и запись в неё относятся к SPEC-008/009, реальные задания Scheduler — к SPEC-010, `/my_posts` — SPEC-017, `/cancel` — SPEC-018.

## Матрица требований

| Требования | Проверки | Статус |
|---|---|---|
| US-006-01/02/04, AC-006-01/02/03, BR-006-01…03, ERR-006-01/02, TC-006-01/03 | `test_local_to_utc_and_roundtrip`, `test_missing_or_invalid_user_timezone_falls_back`, `test_valid_user_timezone_overrides_default`, `test_profile_timezone_fallback_conversion_and_handoff` | ✅ Профиль / null / неверный пояс; дефолт из конфигурации, не литерал в сервисе |
| US-006-03, AC-006-04, BR-006-08, TC-006-02 | `test_local_to_utc_and_roundtrip`, `test_utc_input_does_not_need_to_be_reinterpreted`, `test_profile_timezone_fallback_conversion_and_handoff` | ✅ UTC→local→строка для существующего ответа после даты; `/my_posts` ещё нет |
| AC-006-05, BR-006-09, TC-006-04, IT-006-04 | `test_server_timezone_does_not_affect_conversion`, `test_profile_timezone_fallback_conversion_and_handoff` с `TZ=Pacific/Honolulu` | ✅ ОС и пользовательские часы различаются; TZ восстановлен после теста |
| AC-006-06/07, BR-006-10, TC-006-05 | `test_future_check_is_made_in_utc`; регрессия `test_schedule_spec005.py` | ✅ `12:00 UTC` после `11:30 UTC` и до `12:30 UTC` |
| BR-006-04…06, INV-006-01/04, §32 | `test_post_service_handoff_requires_utc_and_publication_timezone`, `test_profile_timezone_fallback_conversion_and_handoff` | ✅ Контракт DTO и вызов сервиса содержат UTC+timezone; **хранение posts в БД не проверено** |
| BR-006-07, INV-006-02, §33 | `test_scheduler_configuration_uses_utc` | ✅ APScheduler настроен на UTC; **jobs ещё нет** |
| IT-006-01, E2E §41–42 до точки вызова PostService | `test_profile_timezone_fallback_conversion_and_handoff`, регрессия `test_spec005_flow.py` | ✅ Telegram → users → дата → timezone → UTC → заглушка PostService |
| AC-006-08/09, IT-006-02/03, E2E §41–42 после PostService | Нет модели/репозитория `posts` (SPEC-008/009) | ⏳ Нельзя утверждать о сохранённых полях в БД и обратном чтении публикации |
| AC-006-10, BR-006-08/INV-006-05 для `/my_posts`, вывод `/cancel` | Команды относятся к SPEC-017/018 | ⏳ Есть общий конвертер, но этих экранов пока нет |

## Проверка качества

- Python **3.12.11**, весь проект: `python -m pytest -q -p no:cacheprovider` — **278 passed**.
- `ruff check .` — без ошибок; `ruff format --check .` — 64 файла; `git diff --check` — без замечаний.
- Проверка чувствительности: три временные мутации с восстановлением исходника — потеря fallback (7 failed), пометка локального времени как UTC (8 failed), пропуск обратной конвертации (5 failed).
- Используется fake Telegram, не реальный `api.telegram.org`. Docker не запускался (в среде нет Docker).

## Нерешённое в предоставленном ТЗ

SPEC-006 не задаёт политику для несуществующего или неоднозначного локального времени при DST-переходе (gap/fold). Не добавлено самодельное правило: для naive ввода применяется стандартное поведение `zoneinfo`, `fold=0`. До использования таких дат в реальных публикациях продукту потребуется явное решение по этому правилу.

Даты в примерах `07.10.2026` на текущую дату уже прошли; тесты фиксируют `now=07.10.2026` при проверке будущего времени.
