# Проверка SPEC-017 — Команда `/my_posts`

## Объём и результат

Реализованы `PostQueryService`, команда `/my_posts`, поиск пользователя через `UserService`, запрос собственных `PENDING` через `PostRepository.list_pending_by_user`, сортировка по времени, локальное отображение по сохранённой timezone публикации, ID и локализованные названия типов. Ошибки загрузки и отсутствие пользователя обрабатываются контролируемо; длинный ответ разбивается на части. Команда зарегистрирована через DI в приложении.

## Проверки

- `tests/test_spec017.py`: **6 passed**.
- Полный набор, кроме проверки версии среды: **447 passed, 1 deselected**.
- `ruff check .`: чисто.
- `ruff format --check .`: чисто (79 файлов).
- `git diff --check`: чисто.

Полный тестовый набор в sandbox использует Python 3.11.2, тогда как проект требует Python 3.12+. Поэтому `tests/test_smoke.py::test_python_is_312_or_newer` пропущен в итоговом прогоне и отдельно падает из-за версии интерпретатора, а не реализации SPEC-017. Дальнейшая runtime-валидация должна проходить на Python 3.12+.

## Матрица приёмки

| Критерий | Проверка | Статус |
|---|---|---|
| AC-017-01 — команда работает для существующего пользователя | `test_my_posts_end_to_end_shows_only_current_users_pending_posts` | ✅ |
| AC-017-02…03 — только `PENDING` текущего владельца | `test_repository_filters_by_owner_and_pending_and_orders_by_time`, E2E | ✅ |
| AC-017-04 — сортировка по UTC времени | repository test и проверка порядка E2E | ✅ |
| AC-017-05 — локальная timezone | `test_tc_017_04_05_format_converts_timezone_and_maps_publication_types`, E2E | ✅ |
| AC-017-06…07 — ID и читаемый тип | форматный тест и E2E | ✅ |
| AC-017-08 — пустой список | E2E: пустой профиль получает `NO_PENDING_POSTS_TEXT` | ✅ |
| AC-017-09…11 — терминальные/активные не-PENDING статусы скрыты | repository test: `PUBLISHED`, `PROCESSING`, `FAILED`, `CANCELLED` | ✅ |
| AC-017-12 — чужие публикации скрыты | repository test и E2E | ✅ |
| Ошибка чтения и неизвестный пользователь | `test_my_posts_handles_missing_user_and_repository_failure` | ✅ |
| Большой список | `test_empty_list_and_large_list_formatting` | ✅ |
