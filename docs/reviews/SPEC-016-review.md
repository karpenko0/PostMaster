# Проверка SPEC-016 — Абстракция Publisher

## Объём и результат

Проверена реализация единого контракта `Publisher` (`publish(post) -> PublishResult`), компонента `PublisherResolver`, ошибок `PUBLISHER_NOT_FOUND` (`ERR-016-01`) и `INVALID_PUBLICATION_TYPE` (`ERR-016-02`), интеграции с `PublicationService`, `SchedulerService` и `Application.from_settings`, а также расширяемости для будущих площадок (`VKPublisher`).

Полный прогон тестов: **439 passed** (`15 passed` в `tests/test_spec016.py`). `ruff check .`, `ruff format --check .`, `git diff --check` — чисто.

Контроль чувствительности: 6 мутаций M1–M6 (подмена маппинга `REMINDER`/`TELEGRAM`, пропуск проверки пустого типа, возврат чужого `Publisher` вместо `PublisherNotFoundError`, вызов `mark_published` при `PublishResult(success=False)`) обнаружены тестами `tests/test_spec016.py`.

## Матрица приёмки (AC-016-01 … AC-016-09)

| Критерий | Проверка | Статус |
|---|---|---|
| AC-016-01 — `ReminderPublisher` и `TelegramPublisher` реализуют `publish(post) -> PublishResult` | `test_tc_016_04_both_publishers_implement_unified_publish_contract` | ✅ |
| AC-016-02 — для `REMINDER` `PublisherResolver` возвращает `ReminderPublisher` | `test_tc_016_01_02_resolver_returns_registered_reminder_and_telegram_publishers` | ✅ |
| AC-016-03 — для `TELEGRAM` `PublisherResolver` возвращает `TelegramPublisher` | `test_tc_016_01_02_resolver_returns_registered_reminder_and_telegram_publishers` | ✅ |
| AC-016-04 — неизвестный тип не приводит к вызову `Publisher` | `test_tc_016_03_unknown_type_raises_publisher_not_found_and_logs_error`, `test_publication_service_calls_mark_published_on_success_and_error_flow_on_failure` | ✅ |
| AC-016-05 — `Scheduler` вызывает `PublicationService`, а не конкретный `Publisher` | `test_it_016_01_02_03_and_vk_extensibility_without_scheduler_changes`, `tests/test_architecture.py` | ✅ |
| AC-016-06 — `Recovery` и `MissedJobService` не зависят от конкретной реализации `Publisher` | `test_it_016_01_02_03_and_vk_extensibility_without_scheduler_changes` | ✅ |
| AC-016-07 — все `Publisher` возвращают единый `PublishResult` | `test_tc_016_04_both_publishers_implement_unified_publish_contract` | ✅ |
| AC-016-08 — финальный статус обновляется в `PublicationService` | `test_publication_service_calls_mark_published_on_success_and_error_flow_on_failure` | ✅ |
| AC-016-09 — добавление нового `Publisher` (`VKPublisher`) не требует изменения `Scheduler` | `test_it_016_01_02_03_and_vk_extensibility_without_scheduler_changes` | ✅ |

## Сверка тестов, ошибок, правил и инвариантов (§17–29)

| Раздел SPEC-016 | Доказательство | Статус |
|---|---|---|
| §21 `ERR-016-01` (`PUBLISHER_NOT_FOUND`), `ERR-016-02` (`INVALID_PUBLICATION_TYPE`) | `test_tc_016_03_unknown_type_raises_publisher_not_found_and_logs_error`, `test_err_016_02_invalid_publication_type_raises_invalid_publication_type` | ✅ |
| §22 Логирование `Publisher resolved` и `Publisher not found` | `test_tc_016_01_02...`, `test_tc_016_03...`, `test_err_016_02...` | ✅ |
| §24 Unit Tests `TC-016-01…04` | `test_tc_016_01_02...`, `test_tc_016_03...`, `test_tc_016_04...` | ✅ |
| §25 `PublicationService` Tests (`mark_published` и error flow) | `test_publication_service_calls_mark_published_on_success_and_error_flow_on_failure` | ✅ |
| §26 Integration Tests `IT-016-01…03` | `test_it_016_01_02_03_and_vk_extensibility_without_scheduler_changes` | ✅ |
| §27 E2E-сценарий (`REMINDER` и `TELEGRAM`) | `test_e2e_27_scheduler_to_resolver_to_publisher_for_reminder_and_telegram` | ✅ |
| §28 Инварианты `INV-016-01…05` | Подтверждены модульными, интеграционными и сквозными тестами | ✅ |
