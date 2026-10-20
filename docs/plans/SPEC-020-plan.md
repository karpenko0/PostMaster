# План разработки SPEC-020 «Поддержка caption»

- Спецификация: [docs/specs/SPEC-020.md](../specs/SPEC-020.md)
- Статус: ✅ реализовано и проверено (480 тестов пройдены; smoke-проверка версии Python исключена из-за Python 3.11 в sandbox)

## 1. Решения

| № | Решение | Причина |
|---|---|---|
| D1 | Получать подпись из `message.caption` вместе с фото, не вводить отдельный FSM-шаг | Это естественный Telegram UX и сохраняет прежнюю последовательность `/start → photo → date` |
| D2 | Расширить `PhotoData` опциональным caption и хранить его в draft до создания Post | Передать поле через имеющийся Application/Dialog flow без прямого доступа handler к Repository |
| D3 | Пустую/пробельную строку нормализовать в `None`/SQL NULL; непустой текст хранить буквально | Пустой caption равен отсутствию, а содержимое пользователя нельзя переписывать |
| D4 | Publisher не менять: он уже передаёт caption при наличии; усилить тестами обоих контрактов | Реализации Reminder и Telegram поддерживают caption по SPEC-014/015 |
| D5 | Замена фото в `WAITING_DATETIME` перезаписывает также caption | Caption связан с конкретным изображением |

## 2. Этапы

1. Обновить `PhotoData`, `PhotoService`, photo handler и dialog draft.
2. Передавать draft caption в `PostService`; нормализовать пустое значение и сохранить NULL.
3. Добавить fake Telegram caption и repository, Publisher и E2E тесты.
4. Обновить сообщение после фото, `/help`, README, спецификацию и review.
5. Запустить полный набор тестов и линтеры.
