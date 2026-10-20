# План разработки SPEC-018 «Команда `/cancel`»

- Спецификация: [docs/specs/SPEC-018.md](../specs/SPEC-018.md)
- Статус: ✅ реализовано и проверено (459 тестов пройдены; smoke-проверка версии Python исключена из-за Python 3.11 в sandbox)

## 1. Решения

| № | Решение | Причина |
|---|---|---|
| D1 | Сохранить один conditional `UPDATE` с фильтрами post ID, owner ID и `PENDING`; публичный метод назвать `cancel_pending_post`, оставив alias `cancel_post` | Не ослаблять существующий атомарный контракт SPEC-008/013 и не ломать совместимость |
| D2 | Создать `PostCancellationService`, который разрешает пользователя, запускает atomic cancel и только затем удаляет job | Handler остаётся тонким, Scheduler не выбирает/не меняет статус Post |
| D3 | При `rowcount=0` перечитывать запись только для выбора ответа; чужой и отсутствующий Post скрывать одинаковым сообщением | Не раскрывать принадлежность чужой публикации; изменение всё равно защищено conditional update |
| D4 | Добавить `SchedulerService.remove_post`; отсутствующий job — штатный исход, исключение cleanup не откатывает отмену | БД является источником истины; callback не пройдёт atomic claim для `CANCELLED` |
| D5 | Разбирать только один положительный ASCII integer; все ошибки формата отвечать до сервиса | Защитить Repository от некорректного ID |

## 2. Этапы

1. Добавить cancel-service, handler и ответы; подключить DI.
2. Добавить Scheduler job removal с безопасной обработкой `JobLookupError`.
3. Оформить atomic repository API с обратной совместимостью.
4. Добавить unit, repository, concurrency, scheduler integration и E2E-тесты.
5. Обновить README, спецификацию и review; выполнить тесты и статические проверки.
