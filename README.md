# Sales Manager FIRST — F1-19 R1

Подготовка передачи заинтересованного клиента менеджеру.

## Статус

Исходный пакет распакован без изменения файлов. CHECK_PACKAGE.py: PACKAGE_INPUTS_PASS. Реализация F1-19 ещё не начата; приёмка компонента не проводилась.

## Задача

Добавить build_handoff_intent(context, recipient, interest). При подтверждённом INTEREST подготовить PREPARED с помощью принятого модуля F1-09. При QUESTION или UNCERTAIN вернуть HOLD. Проверять входы и область данных. Сохранять полную предоставленную историю. Отправка уведомлений и запись в журнал выполняются отдельными компонентами.

## С чего начать

1. Прочитать [ТЗ](00_READ_FIRST_TECHNICAL_TASK_RU.md).
2. Изучить CONTRACT.json, SHARED_CONTRACTS.json и EXAMPLES.json.
3. Подготовить отдельную venv с Python 3.12.x по ENVIRONMENT_AND_CHECKS_RU.md.
4. Проверить пакет и сохранить исходные результаты.
5. Добавить только разрешённые файлы, выполнить проверки и подготовить возврат по RETURN_RU.md.

## Разрешённые новые файлы

- source/sales_handoff_intent.py
- source/tests/wave3/test_f119.py
- source/tests/fixtures/wave3/f119/*.json

56 исходных файлов source защищены контрольными суммами. Существующие модули, manifests, runners, зависимости и SQL не изменять. Документы и логи создаются вне source/.

## Проверки из корня репозитория

```bash
python -B CHECK_PACKAGE.py
python -B run_checks.py --source source --output evidence/before
python -B run_checks.py --source source --candidate f119 --output evidence/after
```

Требуется настоящий psycopg[binary]==3.2.9. Исходные 204 успешных компонентных теста зафиксированы автором пакета в baseline_evidence/; они не означают успешную проверку будущего F1-19. До реализации candidate должен сообщать NOT_IMPLEMENTED.

## Сдача

ZIP с корнем F1-19_RETURN_R1: changes/, RETURN_REPORT.txt, evidence/, DIFF.patch, SHA256SUMS.txt и INTEGRATION_NOTE.txt. Подробности — [RETURN_RU.md](RETURN_RU.md).

Не запускать приложение, worker, production DB, Make или реальные отправки. Интеграцию и независимую приёмку выполняют отдельно.
