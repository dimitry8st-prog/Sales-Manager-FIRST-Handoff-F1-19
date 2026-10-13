# Приёмка F1-02 R1

Разработчик реализует тесты в test_first_inbox_contract.py по всем Acceptance и зафиксированным решениям CONTRACT.md, включая негативные границы. Нельзя подменять целевой модуль mock: подменяются только внешние store/transport/clock. Независимый QA повторяет отрицательные случаи и добавляет хотя бы один случай на каждую обнаруженную границу.

Из source/ в тестовой среде Python3.12:
```sh
python -m unittest discover -s tests -p 'test_first_inbox_contract.py' -v
```
Требуется exit0 всех новых tests и исходные raw logs. Существующую регрессию запускать по BASELINE_EVIDENCE, без сети/реальнойБД; известные3ошибки не объявлять PASS. Сопоставить baseline и after; новые регрессии запрещены.

Проверить immutability входных словарей/повторяемость результата для pure-функций, а для entrypoint/worker реальные вызовы обработчика с fake adapters. Контракт должен проверяться фактической реализацией, не копией логики в тесте. В packet ещё нет целевой реализации/новых tests: будущая приёмка NOT RUN. Предоставленные source и acceptance spec — вход для разработки.

 stable key on identical replay; distinct mailbox/provider/message gives distinct key; account/recipient mismatch; unicode; malformed timestamps; future timestamp; legitimate old replay accepted; no ID case mutation; prompt-like body preserved as data; no payload mutation; no outgoing calls. Include fixture schema documentation within module docstring.



Полная матрица случаев и ожидаемого поведения: CONTRACT.md. Для ошибок проверить тип и стабильный code; лог не должен содержать ключи/содержимое письма/DSN. Tests в полном цикле с реальными провайдерами не входят в это локальное ТЗ, но обязательны до запуска FIRST.
