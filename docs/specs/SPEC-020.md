# SPEC-020. Поддержка caption

**Проект:** Telegram-бот «PostMaster»
**Этап:** MVP
**Версия:** 1.0
**Статус:** реализовано

## 1. Назначение

Поддержать необязательную подпись к фотографии на всём пути от Telegram Update до сохранённого `Post` и `ReminderPublisher`/`TelegramPublisher`. Пользователь добавляет caption стандартным способом — прикрепляет текст к фото в Telegram. Отдельный FSM-шаг не вводится.

## 2. Scope и зависимости

В scope входят nullable `Post.caption`, сохранение/чтение в Repository, передача исходного текста через диалог и `PostService`, и отправка с фото обоими Publisher. Поле `posts.caption TEXT NULL` и базовая поддержка publisher/repository уже существовали; SPEC-020 доводит пользовательский путь и фиксирует поведение пустого значения.

Зависит от SPEC-008, 009, 014 и 015. Не входят генерация, перефразирование, перевод, редактор или изменение подписи после создания.

## 3. Flow создания

```text
Telegram photo Update (message.caption)
  → PhotoService / PhotoData.caption
  → DialogService draft[caption]
  → PostService.create_scheduled_post(caption=...)
  → PostRepository / posts.caption
  → Post.caption
  → Publisher.send_photo(..., caption=...)
```

Пользователь отправляет фото с подписью либо без неё и затем вводит дату. При повторной отправке фото на шаге даты заменяются и фотография, и её подпись.

## 4. Правила значения

- `None` и пустая/состоящая только из пробелов подпись означают отсутствие caption и сохраняются как SQL `NULL`.
- Непустой пользовательский текст сохраняется без trim, переписывания, генерации и смысловых изменений.
- Caption необязателен для создания Post.
- Handler получает подпись из `message.caption`; временное значение живёт в FSM-черновике до создания Post.

## 5. Publisher

При непустом caption оба Publisher передают точно то же значение как keyword `caption` к `send_photo`. При отсутствии/пустой подписи вызывают `send_photo(chat_id, file_id)` без параметра caption. Publisher не логирует сам caption и не изменяет его.

Если Telegram отклонит caption, ошибка остаётся общей ошибкой отправки и проходит существующий publication error flow; отдельная retry/error taxonomy не входит в SPEC-020.

## 6. Acceptance Criteria

- **AC-020-01:** Post создаётся с `caption=NULL`.
- **AC-020-02:** Post создаётся с непустым caption.
- **AC-020-03…04:** caption сохраняется и возвращается без потери.
- **AC-020-05…06:** ReminderPublisher и TelegramPublisher получают значение Post.caption.
- **AC-020-07:** при отсутствии/пустом caption `send_photo` вызывается без keyword `caption`.
- **AC-020-08:** при наличии caption он передан вместе с фото.
- **AC-020-09:** Publisher сохраняет пользовательский текст без изменений.

## 7. Тестирование и инварианты

Проверяются запись/повторное чтение текста и SQL NULL; значения `None`, пустая строка и пробелы; точная передача multiline/Unicode-текста обоим Publisher; интеграция Telegram photo caption → FSM → создание Post → Repository.

Инварианты: подпись необязательна; хранится вместе с Post; используется Publisher без изменений; её отсутствие не блокирует публикацию.
