# Проверка SPEC-001…SPEC-004 по критериям приёмки

Дата проверки: 2026-10-10. Ветка `arena/1b9daf2a-postmaster`, коммит-основание `9779dd5`.

Подробная проверка SPEC-001…SPEC-003 с мутациями M1–M11 и закрытием открытых вопросов —
в `docs/reviews/SPEC-001-003-review.md`. Этот документ добавляет полную сверку SPEC-004 и
регрессию предыдущих спецификаций после её реализации.

## Итог

Все критерии приёмки SPEC-004 (US, AC, BR, ERR, TC, IT, E2E и Definition of Done §37) выполнены.
При проверке найдены и закрыты два пробела в тестовом покрытии (см. ниже). Регрессии по
SPEC-001…SPEC-003 нет: полный прогон — **212 passed**, `ruff check` и `ruff format --check`
чистые, мутации M1–M11 обнаружены.

## Методика

1. Формулировки US/AC/BR/ERR/TC/IT/E2E/DoD взяты из `docs/specs/SPEC-004.md` дословно.
2. Каждая формулировка сопоставлена с тестом, который её проверяет (таблицы ниже).
3. Тексты ответов и ключи FSM сверены с кодовыми блоками спецификации автоматически
   (`tests/test_spec_texts.py`): правка текста без правки спецификации ломает тест.
4. Прогнаны полный набор тестов, мутации M1–M11 и проверки ruff.
5. Критерии SPEC-001…SPEC-003 перепроверены после изменений SPEC-004 (регрессия).

## Найденные и закрытые пробелы

| № | Пробел | Решение |
|---|---|---|
| 1 | Текст §25 («📸 Фото получил!…») в тесте сравнивался с константой обработчика, а не со спецификацией: правка константы не ломала бы тесты | `tests/test_spec_texts.py` сверяет §25, §26 и ключи §16 с текстом `docs/specs/SPEC-004.md`. Мутации M9–M11 подтверждают, что правки ловятся |
| 2 | §30 («file_id и file_unique_id не показываются пользователю») явно не проверялся | `test_replies_never_show_file_identifiers`: ни один ответ бота не содержит значений из черновика FSM |

## SPEC-004 — User Stories

| Критерий | Где проверяется | Статус |
|---|---|---|
| US-004-01 Фото принимается, бот просит дату | `test_dialog_flow.py::test_full_scenario_creates_post_and_returns_to_idle` | ✅ |
| US-004-02 `file_id` сохраняется и используется в дальнейшем | `test_dialog_flow.py::test_photo_step_saves_metadata_to_fsm`; `test_dialog_service.py::test_valid_date_creates_post_and_returns_to_idle` (file_id уходит в `create_post`) | ✅ |
| US-004-03 Максимальный вариант фото | `test_photo.py::test_largest_photo_size_is_selected` (3 порядка массива) | ✅ |
| US-004-04 Переход к выбору времени с инструкцией | `test_dialog_service.py::test_photo_moves_to_waiting_datetime_and_keeps_file`; текст §25 в полном сценарии | ✅ |

## SPEC-004 — Acceptance Criteria

| Критерий | Где проверяется | Статус |
|---|---|---|
| AC-004-01 Приём фотографии в `WAITING_PHOTO` | `test_dialog_flow.py::test_full_scenario_creates_post_and_returns_to_idle` | ✅ |
| AC-004-02 Извлечение `file_id` | `test_photo.py::test_one_photo_size_returns_its_file_id`, `::test_all_metadata_is_captured` | ✅ |
| AC-004-03 Выбор 1280×1280 из 90/320/1280 | `test_photo.py::test_largest_photo_size_is_selected` (в том числе не по порядку) | ✅ |
| AC-004-04 Сохранение `file_unique_id` | `test_photo.py::test_all_metadata_is_captured`; `test_dialog_service.py::test_photo_draft_carries_all_metadata` | ✅ |
| AC-004-05 Без локального файла | `test_dialog_flow.py::test_photo_leaves_no_image_files_on_disk` | ✅ |
| AC-004-06 Переход в `WAITING_DATETIME` | `test_dialog_service.py::test_photo_moves_to_waiting_datetime_and_keeps_file`; `wait_for_state` в потоковых тестах | ✅ |
| AC-004-07 Подсказка «ДД.ММ.ГГГГ ЧЧ:ММ» | `test_dialog_flow.py::test_full_scenario…` (вхождение) + `test_spec_texts.py::test_photo_accepted_text_matches_spec` (текст §25 дословно) | ✅ |
| AC-004-08 Текст вместо фото | `test_dialog_flow.py::test_text_while_waiting_photo_gets_photo_hint` (состояние и текст §26); `test_dialog_service.py::test_invalid_message_keeps_state_and_draft[WAITING_PHOTO-text]` (пост не создаётся) | ✅ |
| AC-004-09 Повторная фотография | `test_dialog_flow.py::test_repeated_photo_replaces_previous_and_asks_date_again`; `test_dialog_service.py::test_replaced_photo_rewrites_all_metadata` | ✅ |

## SPEC-004 — Бизнес-правила и ошибки

| Критерий | Где проверяется | Статус |
|---|---|---|
| BR-004-01 Фото принимается только из `photo`-сообщений | обработчик `content_types=["photo"]`; `test_dialog_service.py::test_invalid_message_keeps_state_and_draft` (текст/прочее не принимается) | ✅ |
| BR-004-02 `file_id` — основной идентификатор | `test_dialog_service.py::test_valid_date_creates_post_and_returns_to_idle` | ✅ |
| BR-004-03 Скачивание не требуется | AC-004-05; в коде нет обращений к `getFile`/файловой системе | ✅ |
| BR-004-04 Максимальный доступный размер | `test_photo.py::test_largest_photo_size_is_selected` | ✅ |
| BR-004-05 Фото доступно следующему шагу | черновик FSM → `create_post` (тесты сервиса) | ✅ |
| BR-004-06 Переход в `WAITING_DATETIME` | AC-004-06 | ✅ |
| BR-004-07 Бот запрашивает дату и время | текст §25 (AC-004-07) | ✅ |
| BR-004-08 Фото не создаёт публикацию в БД | `test_dialog_flow.py::test_photo_does_not_touch_database` (снимок БД до/после) | ✅ |
| BR-004-09 Замена фото и повторный запрос даты | `test_dialog_flow.py::test_repeated_photo…`; `test_dialog_service.py::test_new_photo_on_date_step_replaces_previous_and_asks_again` | ✅ |
| ERR-004-01 Update без фото: FSM не меняется, запросить фото | `test_dialog_flow.py::test_text_while_waiting_photo_gets_photo_hint` и `test_dialog_service.py::test_invalid_message_keeps_state_and_draft[WAITING_PHOTO-other]`; ответ — текст §26 | ✅ |
| ERR-004-02 Пустой `photo[]`: не сохранять, не переходить, ошибка в логах | `test_dialog_flow.py::test_empty_photo_array_asks_photo_and_keeps_state`; `test_photo.py::test_empty_list_is_processing_error`, `::test_handle_photo_logs_processing_error` | ✅ |
| ERR-004-03 Нет `file_id`: обработка неуспешна | `test_photo.py::test_selected_size_without_file_id_is_processing_error`, `::test_missing_file_id_on_largest_is_not_replaced_by_smaller` (unit-уровень; сквозной невозможен — telebot не разбирает `PhotoSize` без `file_id`) | ✅ |

## SPEC-004 — Unit, Integration, E2E и DoD

| Критерий | Где проверяется | Статус |
|---|---|---|
| TC-004-01 Один PhotoSize | `test_photo.py::test_one_photo_size_returns_its_file_id` | ✅ |
| TC-004-02 Несколько PhotoSize → 1280×1280 | `test_photo.py::test_largest_photo_size_is_selected` | ✅ |
| TC-004-03 Метаданные (5 полей) | `test_photo.py::test_all_metadata_is_captured`; `test_dialog_service.py::test_photo_draft_carries_all_metadata`; `test_spec_texts.py::test_fsm_draft_keys_match_spec` | ✅ |
| TC-004-04 Пустой список | `test_photo.py::test_empty_list_is_processing_error`; `test_dialog_flow.py::test_empty_photo_array…` (состояние не меняется) | ✅ |
| IT-004-01 Update → handler → PhotoData → FSM | `test_dialog_flow.py::test_photo_step_saves_metadata_to_fsm` | ✅ |
| IT-004-02 Верный `telegram_file_id` в FSM | там же (черновик сверяется целиком) | ✅ |
| IT-004-03 Замена `file_id` новым | `test_dialog_flow.py::test_repeated_photo…` | ✅ |
| E2E-004-01 Полный сценарий до SPEC-005 | `test_dialog_flow.py::test_full_scenario_creates_post_and_returns_to_idle` | ✅ |

### Definition of Done (§37)

| Пункт | Статус |
|---|---|
| бот принимает Telegram photo | ✅ |
| корректно обрабатывается `photo[]` | ✅ |
| выбирается максимальный размер | ✅ |
| извлекается `file_id` | ✅ |
| извлекается `file_unique_id` | ✅ |
| доступны `file_size`, `width`, `height` | ✅ (включая отсутствие `file_size`) |
| фотография не требует локального хранения | ✅ |
| данные сохраняются в FSM | ✅ |
| состояние меняется на `WAITING_DATETIME` | ✅ |
| пользователю отправляется инструкция по вводу даты | ✅ |
| текст вместо фото корректно обрабатывается | ✅ |
| повторная фотография заменяет текущую | ✅ |
| unit tests проходят | ✅ |
| integration tests проходят | ✅ |
| E2E-сценарий проходит | ✅ |

Дополнительно: §29 (логи без бинарных данных) — `test_photo.py::test_handle_photo_logs_success_without_binary_data`;
§30 (идентификаторы не показываются) — `test_dialog_flow.py::test_replies_never_show_file_identifiers`;
§25/§26/§16 сверяются с текстом спецификации — `tests/test_spec_texts.py`.

## Регрессия SPEC-001…SPEC-003

Изменения SPEC-004 (метаданные в черновике, тексты §25/§26, `PhotoService`) не задевают критерии
прежних спецификаций. Все их тесты проходят; полный прогон — 212 passed.

| Спецификация | Критерии | Подтверждение | Статус |
|---|---|---|---|
| SPEC-001 | AC-01…AC-06, TC-01…TC-03 | `test_smoke.py` (46), `test_deployment.py` (5), `test_polling.py` (4), `test_entrypoint.py` (4), `test_contracts.py` (5), `test_architecture.py` (12) | ✅ |
| SPEC-002 | AC-01…AC-04, TC-01…TC-04 | `test_users.py` (13), `test_start_command.py` (5), `test_database.py` (4) | ✅ |
| SPEC-003 | AC-01…AC-04, TC-01…TC-03 | `test_dialog_fsm.py` (24), `test_dialog_service.py` (24), `test_dialog_store.py` (6), `test_dialog_flow.py` (14) | ✅ |

Открытые вопросы SPEC-001…SPEC-003 (`POST_MODE`/`PROCESSING`, текст SPEC-007, спецификация
ввода даты и часовых поясов) остаются открытыми — они не относятся к SPEC-004. Тексты даты
пока прежние (D8 плана SPEC-003, пример `07.10.2026 15:00` в §25 задан спецификацией дословно).

## Мутации

| № | Мутация | Результат |
|---|---|---|
| M1 | выбор минимального размера вместо максимального | 9 failed ✅ |
| M2 | пустой список `photo[]` не ошибка | 3 failed ✅ |
| M3 | не проверяется пустой `file_id` | 2 failed ✅ |
| M4 | ключ черновика `photo_file_id` вместо `telegram_file_id` | 15 failed ✅ |
| M5 | в черновик не пишутся `width`/`height` | 8 failed ✅ |
| M6 | старый текст принятия фото вместо §25 | 2 failed ✅ |
| M7 | другой текст запроса фото вместо §26 | 2 failed ✅ |
| M8 | молчание при ошибке обработки фото | 1 failed ✅ |
| M9 | текст §25 без эмодзи (правка константы) | 1 failed ✅ |
| M10 | другой текст §26 (правка константы) | 3 failed ✅ |
| M11 | ключ FSM `width` → `w` | 9 failed ✅ |

M1–M8 — проверка реализации SPEC-004, M9–M11 — проверка закрытия пробела 1.

## Что не проверено

- Работа с реальным `api.telegram.org` (тесты идут на fake API).
- Сборка и запуск Docker-контейнера (в среде нет Docker; содержимое Dockerfile проверено тестами
  `test_deployment.py` и dry-run без Docker).
- Windows.
- ERR-004-03 на сквозном уровне: pyTelegramBotAPI не разбирает `PhotoSize` без `file_id`
  (конструктор требует аргумент), поэтому случай покрыт unit-тестами и проверкой в `PhotoService`.
