# FIRST Wave3 — среда и проверка персонального возврата

Используйте Python 3.12.x и настоящий psycopg[binary]==3.2.9. Зависимость подтверждена source/requirements.txt; тесты unittest. pytest не требуется. Создайте отдельную venv и установите зависимости через `python -m pip install -r source/requirements.txt`. Не заменяйте psycopg заглушкой ради PASS. Не запускайте main.py, worker_prod.py, migrate.py, Docker/Make или корневой discovery при выполнении этого задания.

Из каталога персонального пакета:

```
python -B run_checks.py --source source --output evidence/before
```

После реализации, например F1-12:

```
python -B run_checks.py --source source --candidate f112 --output evidence/after
```

Собственные тесты расположите в source/tests/wave3/test_f112.py (для выбранных новых задач соответственно test_f113.py … test_f117.py, test_f119.py, test_f120.py; F1-18 исключён). Runner проверяет все 56 неизменяемых файлов по точным SHA256; работает на одноразовой копии. Старые файлы source не меняются: новые реализации и тесты добавляются только по allowlist вашего ТЗ. Если allowlist предусматривает изменение старого файла, это требует отдельного явно версионированного контракта baseline; переписывать manifest ради зелёной отметки запрещено.

Baseline содержит отдельно binding32, language44, send-receipt52, handoff27 и worker-health49. Worker-health включает собственный localhost HTTP и ограниченные subprocess probes; поэтому localhost разрешён, внешний network запрещён. Пятёрка baseline не включает ingress network, root discovery или PostgreSQL inbox. Это 204 компонентных теста, не полноценная регрессия системы. Исторические три ошибки root и ошибки несовместимого network-denying harness не подмешиваются в этот PASS.

Обычные component suites используют настоящий импорт psycopg, но запрещают фактический psycopg.connect. Child environment не наследует DATABASE_URL, MAKE_* или облачные credentials. Искусственные часы, разрешения и данные в тестах не являются live permission. Runner — вспомогательное ограничение для проверенных тестов, не sandbox произвольного недоверенного кода; внимательно просмотрите собственные subprocess calls.

## Задачи с PostgreSQL

Для тестов транзакций, дедупликации, блокировок, конкурентных соединений, восстановления и миграций нужен настоящий PostgreSQL16 в своей одноразовой среде. Не используйте рабочую БД и её credentials. Создайте отдельный localhost-кластер/контейнер, explicit port, отдельного пользователя, БД sm_wave3_<task>_<unique> и схему sm_wave3_<task>_<unique>. Задайте WAVE3_TEST_DSN, WAVE3_TEST_SCHEMA и WAVE3_DISPOSABLE_POSTGRES=YES. DSN не вставляется в отчёт или лог; пример значений содержит только синтетические placeholders. Не используйте DATABASE_URL как fallback.

```
python -B run_checks.py --source source --candidate f115 --requires-postgres --output evidence/after
```

Runner требует explicit localhost host/port/user/dbname, префиксы БД/схемы sm_wave3_, затем read-only проверяет server_version_num=16.x с connect_timeout5/statement_timeout5/lock_timeout3. Он не создаёт/не мигрирует/не удаляет БД. Ваши DB tests обязаны использовать только WAVE3_TEST_DSN и WAVE3_TEST_SCHEMA; любые эффекты находятся в собственной disposable БД/схеме. После проверки удалите созданные объекты и остановите собственную среду. PG_NOT_RUN, mock DB или skipped tests не доказывают durability/atomicity.

## Приёмочные векторы

Проверяйте поведение по собственным входам и ожидаемым состояниям, а не копируйте внутренние условия реализации. Каждый контракт требует valid chain, malformed/type/control/Unicode boundaries, scope/version mismatch, replay/idempotency, stop/optout/handoff ownership и failure/no-side-effects там, где они относятся к ТЗ. DB задачи дополнительно: rollback после сбоя, два независимых соединения, collision/replay, restart и неоднозначный результат. Проверки release не требуют отправки реального письма.

Сохраните сырые stdout/stderr каждого suite, CHECKS_SUMMARY.json, точную команду, Python/psycopg/PostgreSQL версии, exit codes и список PASSED/FAILED/NOT_RUN. Пустой suite, skipped cases, hash mismatch или timeout означают FAIL/NOT_RUN, а не успешную приёмку. Сохраняйте первоначальные неудачные логи отдельно; не придумывайте исторические exit codes. Component PASS не закрывает продуктовый FG и не доказывает связь с текущим Cloud/Make/mailbox.

F1-11 — исключение для сохранённого задания: --candidate f111 проверяет source/tests/test_first_thread_correlation.py; fixtures остаются в source/tests/fixtures/first_thread_correlation по исходному allowlist. Новая база/передача R2 не меняет семантический контракт технической R1. Для новых задач используется tests/wave3; shared __init__.py создавать не требуется: unittest discovery запускается непосредственно в каталоге.

До реализации команда с --candidate возвращает exit3, status NOT_IMPLEMENTED, pass=false, если обязательный файл тестов отсутствует. Baseline в этой попытке NOT_RUN; его отдельный ранее сохранённый PASS не объявляется PASS новой реализации. Пустой существующий suite тоже FAIL.
