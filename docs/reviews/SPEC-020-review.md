# Проверка SPEC-020 — Поддержка caption

## Объём и результат

Проверена реализация поддержки caption: сохранение в БД, передача в Publisher'ы,
нормализация, end-to-end через Long Polling. Полный прогон на Python 3.14.6:
**539 passed**, 1 skipped. `ruff check .`, `ruff format --check .` — успешно.

Исходный текст SPEC-020: `docs/specs/SPEC-020-поддержка-caption.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| posts.caption TEXT NULL | уже было в schema: `caption: Mapped[str \| None]` | ✅ |
| Caption сохраняется с Post | `test_caption_save_and_read`, `test_caption_unicode_in_db`, `test_caption_long_text` | ✅ PhotoService → draft → PostService → БД |
| Caption передаётся в Publisher'ы | `test_telegram_publisher_passes_caption`, `test_reminder_publisher_passes_caption_to_photo`, `test_publication_service_passes_caption_to_telegram/reminder` | ✅ Оба Publisher принимают caption |
| Отсутствие caption не блокирует публикацию | `test_publication_no_caption_does_not_block`, `test_caption_null_save_and_read` | ✅ caption=None — допустимое состояние |
| Publisher не меняет текст caption | `test_telegram_publisher_does_not_modify_caption`, `test_reminder_publisher_does_not_modify_caption` | ✅ Publisher передаёт caption as-is |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| save/read | `test_caption_save_and_read`, `test_caption_unicode_in_db`, `test_caption_long_text` | ✅ |
| null | `test_caption_null_save_and_read`, `test_publication_no_caption_does_not_block` | ✅ |
| Reminder | `test_reminder_publisher_passes_caption_to_photo`, `test_reminder_publisher_caption_none`, `test_publication_service_passes_caption_to_reminder` | ✅ |
| Telegram | `test_telegram_publisher_passes_caption`, `test_telegram_publisher_caption_none`, `test_publication_service_passes_caption_to_telegram` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | Caption хранится в PhotoData | SPEC-020 §3: caption приходит с фото — естественное место хранения |
| D2 | Caption хранится в FSM draft, передаётся в PostService.create_scheduled_post | DialogService оркестрирует сценарий; caption — часть данных черновика |
| D3 | Нормализация caption в PhotoService: пустая строка и пробелы → None | §1 SPEC-020 «optional caption»: None — отсутствие подписи |
| D4 | Publisher получает caption как `str \| None`, не меняет текст | §7 SPEC-020: «Publisher не меняет текст» |
| D5 | Caption не передаётся в send_message (только в send_photo) | Telegram API: caption — атрибут photo, не message |
| D6 | Caption длинного текста (1024 символа) сохраняется без потерь | Telegram лимит: 1024 символа в caption |
| D7 | Unicode (эмодзи, кириллица) сохраняется без изменений | TEXT колонка в SQLite |

## Сценарии

### Happy path: пользователь отправляет фото с caption

```
Telegram → bot: photo + caption="Hello!"
→ dialog.on_photo
  → photo_service.handle_photo(..., caption="Hello!")
    → PhotoData(caption="Hello!")
  → dialog_service.receive_photo
    → _photo_draft: {..., "caption": "Hello!"}
→ через 5 секунд: пользователь отправляет дату
→ dialog_service.receive_text
  → draft["caption"] → caption="Hello!"
  → post_service.create_scheduled_post(..., caption="Hello!")
→ БД: posts.caption = "Hello!"
→ Scheduler → PublicationService → Publisher
→ Telegram API: send_photo(..., caption="Hello!")
```

### Без caption

```
Telegram → bot: photo (caption is None)
→ PhotoService.handle_photo → PhotoData(caption=None)
→ draft: {photo_id, ..., NO "caption" key}
→ PostService.create_scheduled_post(..., caption=None)
→ БД: posts.caption = NULL
→ Publisher: send_photo(..., caption=None)
```

### Caption из одних пробелов

```
Telegram → bot: photo + caption="   "
→ PhotoService.handle_photo → PhotoData(caption=None)  # нормализация
→ БД: posts.caption = NULL
```

## Ограничения scope

- Caption длиннее 1024 символов не отклоняется явно — БД хранит как есть, Telegram API обрежет.
  Лучше валидировать длину в `PostService.create_scheduled_post`, но это вне scope MVP.
- Не валидируется формат caption (Markdown, HTML) — Publisher передаёт как plain text.
  Telegram поддерживает ограниченный Markdown/HTML, но мы не используем.
- Caption нельзя изменить после создания поста. Это можно сделать отдельной SPEC.

## Связь с другими модулями

| Модуль | Что делает с caption |
|---|---|
| domain/photo.py | `PhotoData.caption: str \| None = None` |
| services/photo_service.py | Принимает caption, нормализует пустые/пробельные → None |
| services/dialog_service.py | Сохраняет caption в draft, передаёт в create_scheduled_post |
| services/post_service.py | Принимает caption, передаёт в repository.create_post |
| repositories/post_repository.py | Сохраняет в posts.caption (TEXT NULL), читает в Post |
| publishers/telegram_publisher.py | Передаёт caption в send_photo |
| publishers/reminder_publisher.py | Передаёт caption в send_photo |

## Команды

```bash
python -m pytest tests/test_spec020.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
