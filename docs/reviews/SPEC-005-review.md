# Приёмка SPEC-005 — ввод и валидация даты и времени

## Статус

Доменная валидация, подсказки, timezone и передача данных следующему сервисному этапу реализованы. **Полная приёмка отложенной публикации заблокирована SPEC-008/009**: таблицы `posts` и записи в БД пока нет; `PostService.create_post` по-прежнему возвращает только DTO. В данном этапе не добавлялась запись `posts` и не запускался Scheduler для публикации.

## Проверка критериев

| Требования | Проверка | Результат |
|---|---|---|
| US-005-01, AC-005-01/10/12, TC-005-01/02/10, BR-005-05/07 | `test_future_dates_are_normalized`, `test_comparison_uses_instant_not_naive_clock`, `test_success_hands_utc_local_timezone_and_photo_to_next_service` | ✅ UTC 12:00 при локальном московском 15:00, сравнение по UTC |
| AC-005-02/03/04, TC-005-03/04, BR-005-01/02 | `test_invalid_values_have_precise_code`, `test_error_does_not_call_post_service_or_discard_photo` | ✅ Строгая ширина, разделители, отсутствие лишних пробелов; свободный текст отклоняется |
| AC-005-05/06/07, TC-005-05…08, BR-005-03/04 | `test_invalid_values_have_precise_code` | ✅ Календарь, високосный год, часы и минуты; различные коды `INVALID_DATE` и `INVALID_TIME` |
| AC-005-08/09, TC-005-09/11, BR-005-06 | `test_equal_and_past_are_rejected`, `test_error_does_not_call_post_service_or_discard_photo` | ✅ Равное и прошлое время отклоняются |
| AC-005-11, BR-005-08…10, IT-005-02/03 | `test_telegram_error_then_correction_without_resending_photo`, `test_error_does_not_call_post_service_or_discard_photo` | ✅ FSM/метаданные фото сохранены, запрос к сервису постов при ошибке отсутствует, БД не изменена |
| §26/27, ERR-005-01…04 | `test_user_messages_match_spec`, `test_telegram_error_then_correction_without_resending_photo` | ✅ Тексты ответов сверены с §26/27, код ошибки передаётся сервисом |
| §29/30, IT-005-01 | `test_profile_timezone_changes_utc_instant`, `test_telegram_prefers_timezone_saved_in_user_profile`, `test_defaults_apply_when_optional_values_are_absent` | ✅ Пояс из профиля приоритетнее `DEFAULT_TIMEZONE=Europe/Moscow` |
| §35 | `test_error_does_not_call_post_service_or_discard_photo`, `test_success_hands_utc_local_timezone_and_photo_to_next_service` | ✅ Логи с user_id без введённой строки |
| BR-005-11, IT-005-04, §43 | `test_success_hands_utc_local_timezone_and_photo_to_next_service` | ✅ Photo file_id, UTC datetime, local datetime, timezone попадают в вызов `PostService`; **не** в БД |
| E2E §40 | `test_telegram_error_then_correction_without_resending_photo` | ✅ Fake Telegram: `/start` → фото → неверная дата → прошлое → исправление → handoff; без повторного фото |
| E2E §39, DoD «создаётся публикация» | Нет полной реализации SPEC-008/009 | ⏳ Только до сервисной заглушки. Нельзя утверждать, что существует сохранённая публикация |

## Ограничения и последующие действия

1. SPEC-006 ещё не предоставлена: переходы DST (пропущенные или двусмысленные локальные часы) работают по стандартному `zoneinfo` без отдельной политики. Эта политика должна быть определена SPEC-006.
2. По SPEC-003 прежний успешный путь после возврата заглушки `PostService` переводит FSM в `IDLE` и отвечает «Публикация создана», хотя реальной записи нет. После внедрения SPEC-009 подтверждение должно означать **фактическое** успешное сохранение. Не считать этот текст доказательством созданного поста.
3. `parse_local_datetime` оставлен как совместимый API SPEC-003; пользовательский handler использует новый строгий `parse_publication_datetime`.
4. Проверки идут через fake Telegram API, без реального Telegram. Docker не запускался (в этой среде нет Docker).
5. Примеры 07–08.10.2026 на текущую дату 11.10.2026 уже в прошлом; тесты используют фиксированные часы 07.10.2026 11:59 UTC.

## Контроль чувствительности тестов

Проверены три временные мутации с восстановлением исходного кода: разрешение однозначного дня (1 failed), принятие равного времени (`<=` → `<`, 2 failed), ошибочная пометка локального времени как UTC (8 failed). Все три обнаружены тестами.

## Прогон

Python 3.12.11: `python -m pytest -q -p no:cacheprovider` — **258 passed**. `ruff check .` — чисто; `ruff format --check .` — **61 файл** форматирован; `git diff --check` — чисто. Регрессия SPEC-001…004 включена в полный прогон.
