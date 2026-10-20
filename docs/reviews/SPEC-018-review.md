# Проверка SPEC-018 — Команда `/cancel`

## Объём и результат

Реализованы разбор команды и ID, `PostCancellationService`, owner-scoped atomic `PENDING → CANCELLED`, безопасные ответы по статусам, удаление Scheduler job и best-effort поведение при отсутствующем job/ошибке очистки. Остаточный callback обращается к `PublicationService`; atomic claim отвергает `CANCELLED`, Publisher не вызывается. Post сохраняется в БД и перестаёт отображаться в `/my_posts`.

## Проверки

- `tests/test_spec018.py`: **10 passed**.
- Полный набор, кроме проверки версии среды: **459 passed, 1 deselected**.
- `ruff check .`: чисто.
- `ruff format --check .`: чисто (82 файла).
- `git diff --check`: чисто.

Sandbox использует Python 3.11.2, тогда как проект требует Python 3.12+. `tests/test_smoke.py::test_python_is_312_or_newer` не включён в полный прогон именно из-за версии интерпретатора; тесты SPEC-018 и остальные проверки выполнены.

## Матрица приёмки

| Критерий | Проверка | Статус |
|---|---|---|
| AC-018-01 — извлечение ID команды | `test_tc_018_01_02_03_post_id_parsing`, handler test | ✅ |
| AC-018-02 — own `PENDING` → `CANCELLED` | repository, service и E2E tests | ✅ |
| AC-018-03 — чужой Post не меняется/не раскрывается | repository/service rejection test | ✅ |
| AC-018-04…07 — остальные статусы не отменяются повторно | repository/service status test | ✅ |
| AC-018-08 — после успеха снимается Scheduler job | E2E: активный date-job удаляется после `/cancel` | ✅ |
| AC-018-09 — отсутствие job не откатывает статус | service + `SchedulerService.remove_post` integration test | ✅ |
| AC-018-10 — остаточный callback не вызывает Publisher | failing-remove integration test вызывает due callback после отмены | ✅ |
| AC-018-11…12 — отсутствующий/некорректный ID не вызывает cancel | parser и handler tests | ✅ |
| Concurrency — cancel vs atomic claim | `test_cancel_and_claim_race_allows_exactly_one_transition` | ✅ |
| `/my_posts` integration — отменённая запись исчезает из списка | `test_cancel_command_e2e_updates_post_removes_job_and_hides_it_from_my_posts` | ✅ |
