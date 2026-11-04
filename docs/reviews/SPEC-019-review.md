# Проверка SPEC-019 — Команда help

## Объём и результат

Проверена реализация команды `/help`: текст справки, регистрация handler, сохранение
FSM в любом состоянии. Полный прогон на Python 3.14.6: **518 passed**, 1 skipped.
`ruff check .`, `ruff format --check .` — успешно.

Исходный текст SPEC-019: `docs/specs/SPEC-019-команда-help.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Не меняет state | `test_help_preserves_idle_state`, `test_help_preserves_waiting_photo_state`, `test_help_preserves_waiting_datetime_state` | ✅ Handler не вызывает `dialog_service.*` |
| Не очищает временные данные | `test_help_preserves_draft_data` | ✅ Handler не вызывает `dialog_service.reset()` |
| Описывает только существующие функции | `test_help_text_describes_commands`, `test_help_text_does_not_describe_non_existing_features` | ✅ BR-019-01: только `/start`, `/my_posts`, `/cancel`, `/help` |
| Показывает команды, формат даты и workflow | `test_help_text_describes_commands`, `test_help_text_describes_date_format`, `test_help_text_describes_workflow` | ✅ Все три блока в HELP_TEXT |
| Доступна в любом FSM state | 3 теста preservation, `test_help_works_after_other_commands` | ✅ Handler не зависит от FSM |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| content | `test_help_text_describes_commands`, `test_help_text_describes_date_format`, `test_help_text_describes_workflow`, `test_help_text_does_not_describe_non_existing_features` | ✅ |
| IDLE | `test_help_preserves_idle_state` | ✅ |
| WAITING_PHOTO | `test_help_preserves_waiting_photo_state` | ✅ |
| WAITING_DATETIME | `test_help_preserves_waiting_datetime_state` | ✅ |
| FSM preservation | `test_help_preserves_idle_state`, `test_help_preserves_waiting_photo_state`, `test_help_preserves_waiting_datetime_state`, `test_help_preserves_draft_data` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | Handler без зависимостей (не принимает сервисы) | §7 SPEC-019: handler не должен менять FSM или очищать данные |
| D2 | Текст справки — модульная константа `HELP_TEXT` | Удобно тестировать и обновлять; BR-019-01: только существующие функции |
| D3 | Handler регистрируется ПЕРВЫМ в registry | pyTelegramBotAPI матчит handlers в порядке регистрации — `/help` должен быть до text-handler, иначе попадёт в DialogService |
| D4 | `chat_types=["private"]` | Решение D14 плана SPEC-003 |
| D5 | Не зависит от FSM state — работает одинаково в IDLE/WAITING_PHOTO/WAITING_DATETIME | SPEC-019 §1: «доступна в любом FSM state» |
| D6 | Не вызывает `dialog_service.*`, `state.reset()`, и т. п. | §7 SPEC-019: «Не меняет state и не очищает временные данные» |
| D7 | Текст не упоминается как «/help» команда в середине предложений, только в начале строк | UX: легко читать |

## Сценарии

### Happy path

```
/help
→ handler: chat_types=private, commands=["help"] сматчилось
→ bot.send_message(chat.id, HELP_TEXT)
```

### В IDLE

```
/start (переводит в WAITING_PHOTO)
→ пользователь не отправил фото
→ состояние: WAITING_PHOTO
/help
→ handler: только отправляет текст, FSM не меняется
→ состояние: WAITING_PHOTO
```

### В WAITING_DATETIME

```
/start → WAITING_PHOTO
фото → WAITING_DATETIME
/help
→ handler: отправляет текст, FSM не меняется
→ состояние: WAITING_DATETIME (draft не очищен)
→ пользователь может продолжать с датой
```

## Ограничения scope

- Текст справки не интернационализирован — только русский язык.
- Нет интерактивных кнопок в справке — обычный текст.
- Справка описывает только текущие возможности MVP; добавление новых команд требует
  обновления HELP_TEXT и теста `test_help_text_describes_commands`.

## Команды

```bash
python -m pytest tests/test_spec019.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
