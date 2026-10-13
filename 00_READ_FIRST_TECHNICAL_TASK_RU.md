# F1-19 — Подготовка надёжной передачи заинтересованного клиента

Исполнитель: **Дмитрий стажер**. Проект: **sales-manager-first / Autonomous Sales Manager FIRST**.
Техническая ревизия: R1; передача: R1. Пакет подготовлен для ручной передачи владельцем. Первоначальная выдача разработчику: не подтверждена. Получение и начало: не подтверждены. Доработка не выдавалась.

## 1. Что нужно сдать

Реализовать подготовка надёжной передачи заинтересованного клиента по точному API ниже, добавить собственные положительные и отрицательные тесты и подготовить воспроизводимый возврат. Результат должен работать на входах из EXAMPLES.json и обеспечивать критерии раздела5. Работа относится к FG10 и будет соединена с общей системой отдельным инженером.

FIRST — одна активная настраиваемая кампания/предложение и один ящик, первоначально EN/RU с расширяемыми версиями языковых пакетов. Обычная переписка в принятом будущем контуре автономна; неопределённые/запрещённые действия блокируются. Настоящие бизнес-данные и доступы не требуются для этой разработки.

## 2. Исходная база и зависимости

Полная identity текущей SOURCE: `0176e66cfd4d95d5980188b0d9adb0d5cfb5cc9946c3400be66e06ce69f494c5`. В source/ находятся56 безопасных необходимых файлов, включая принятые компоненты, SQL, тесты и synthetic fixtures. Их точные SHA256 заданы неизменным SOURCE_SNAPSHOT_MANIFEST.json. Все старые файлы остаются неизменными.

- `source/sales_first_handoff.py` — принятая неизменная реализация/контекст.

SHARED_CONTRACTS.json фиксирует общие структуры, типы, версии и правила. CONTRACT.json фиксирует ваш API; EXAMPLES.json — буквальные входы и ожидаемые результаты. Для новых зависимостей используются явные injected protocols и синтетические test doubles строго по опубликованному контракту. Они не заменяют будущую совместную приёмку настоящих реализаций. Уже выданные F1-02, F1-03, F1-10 не переписываются.

## 3. Разрешённые файлы

Пути относительно корня этого пакета:
- `source/sales_handoff_intent.py`
- `source/tests/wave3/test_f119.py`
- `source/tests/fixtures/wave3/f119/*.json`

Новые документы и логи возврата разрешены вне source/. Не редактировать основной runtime, чужие файлы, исходные migrations, manifests, source dependencies или runners. У каждого нового участка один владелец. Общие runtime/Make/DB изменения делает один отдельный инженер после приёмки возвратов.

## 4. Точный программный контракт

### `build_handoff_intent(context: dict, recipient: dict, interest: dict) -> dict`

Вход: Exact F109 FIRST_HANDOFF_INPUT_V1; recipient FIRST_HANDOFF_RECIPIENT_V1 {schema_version,client_id,runtime_campaign_id,recipient_ref:str<=128,approval_ref:str<=128}; trusted interest FIRST_INTEREST_EVIDENCE_V1 {schema_version,client_id,runtime_campaign_id,lead_id,decision:INTEREST|QUESTION|UNCERTAIN,evidence_ref:str<=128}.

Выход:
```json
"{schema_version:FIRST_HANDOFF_INTENT_V1,status:PREPARED|HOLD,handoff_id:str|null,content_sha256:str|null,recipient_ref:str|null,context:accepted F109 assembled context|null,code:OK|INTEREST_NOT_CONFIRMED}"
```
Ошибки: INVALID_INPUT, SCOPE_MISMATCH, F109 HandoffContextError codes propagated.


Нормативные типы, наборы полей, порядок проверок, коды отказов и состояния содержатся в CONTRACT.json и SHARED_CONTRACTS.json. JSON-примеры времени преобразуются в aware datetime там, где API требует datetime. Поля явно перечисленных структур обязательны; неизвестные запрещены, если конкретный существующий контракт не допускает их. Не усекать ID или историю. Не выдавать строковый marker за проверенные права. В чистых функциях входы не меняются, I/O и скрытые часы/окружение отсутствуют; время передаётся явно. PG-операции выполняются только в собственной одноразовой базе и подтверждают успех после commit.

## 5. Обязательная проверка

1. INTEREST exact context positive
2. QUESTION/UNCERTAIN HOLD
3. wrong lead/recipient scope reject
4. same handoff_id modified hash conflict handled journal
5. history>100 refused by F109
6. missing history refused
7. no truncation IDs>128 refused
8. delivery_status stays NOT_DISPATCHED

Реальные вызовы своей реализации обязательны; mock самой проверяемой функции запрещён. Synthetic adapters допускаются на внешних границах по точному ABI. Для PG-задач mock не подтверждает транзакции: нужны реальные два соединения, конкуренция, ошибки commit, перезапуск и восстановление. Проверки содержат assertions ожидаемых результатов и отсутствия запрещённых изменений, а не только количество пройденных тестов.

Из корня пакета:
```
python -B CHECK_PACKAGE.py
python -B run_checks.py --source source --output evidence/before
python -B run_checks.py --source source --candidate f119 --output evidence/after
```
Сначала прочитать ENVIRONMENT_AND_CHECKS_RU.md. Исходная база была проверена на Python3.12.14/psycopg3.2.9:204 компонентных теста успешны; raw logs в baseline_evidence/. Это не результат ещё отсутствующей новой реализации. До добавления собственных tests candidate-режим должен сообщать отсутствие реализации, а не PASS. Полная root-регрессия и live-процесс здесь не запускаются.

## 6. Подключение в общую систему

Производители/потребители и конкретные места будущего соединения:
- `sales_manager.process_notify_reply`
- `sales_store.record_reply`
- `task_store.create_task`

Integrator sources validated classifier evidence/full history; creates one journal action for handoff intent; human ownership after agreed durable transition; trusted notification adapter/outcome reconciliation still required. Literal F119->HANDOFF ActionV1 golden vector in vectors/f119.json; template_id null by kind, fact_ids from complete context.facts; product_id maps explicit trusted binding.offer_id, no guessed F105 product_id field.

Кандидат сдаёт новый компонент и INTEGRATION_NOTE.txt с точным порядком вызовов, зависимостями и примером. Инженер внедрения отвечает за последовательное соединение с настоящими accepted dependencies, миграции/сохранность данных, исправление общего call-site и integrated positive/fault tests. Подключение, runtime identity и полнота соответствующего FG проверяются отдельно; установка ещё одного файла без caller не считается подключением функции.

## 7. Вне вашего задания

- No classifier implementation
- No notification sends
- No history truncation
- No whole-campaign stop
- New journal adapter16MiB canonicalpayload cap may reject oversized otherwise valid F109 context; preserve original full history/context, never trim it.

Не запускать приложение/worker, не обращаться к настоящим Make/mailbox/клиентам и не применять миграции к рабочей БД. NO_OUTBOUND в исходной базе сохраняется. Разработка eligibility/intent в synthetic tests не выдаёт live grant. Реальные права на действия и разрешённый пилот — отдельный этап владельца.

## 8. Порядок выполнения и сдачи

Сначала проверить среду, исходные хеши и совместимость контракта. Затем записать собственную матрицу проверок, реализовать только свой allowlist, выполнить обязательные tests и подготовить возврат по RETURN_RU.md. При конфликте версий/файлов/схемы остановить затронутую правку и сообщить координатору конкретный вход, ожидаемое и фактическое поведение. Не изменять общий контракт молча.

Приёмка компонента: полный контракт, все относящиеся tests, неизменный baseline, доказательный возврат и отдельная проверка. Runtime-подключение и приёмка продукта не подменяются этим результатом. После возврата и каждого внедрения координатор выдаёт владельцу отчёт о принятом объёме, изменении FIRST_INDEX_V1, оставшихся работах и следующем этапе.
