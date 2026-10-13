# -*- coding: utf-8 -*-
"""Чистый загрузчик/валидатор реестра языков FIRST и языковых пакетов.

Принимает уже разобранные JSON-совместимые dict/list и возвращает проверенные
независимые глубокие копии и минимальные языковые поля для policy-стороны
(Camel). Никакого файлового I/O, сети, БД или обращений к окружению; все
функции детерминированы и никогда не изменяют входные объекты.

Алгоритм — стадии валидации (порядок задокументирован автором; при нескольких
нарушениях первым срабатывает более ранний код):

1. базовые типы аргументов (INVALID_INPUT);
2. список approved_fact_ids: тип, уникальность, формат элементов
   (INVALID_FACT_SET);
3. схема контейнера: schema_version, затем точный набор ключей (INVALID_SCHEMA);
4. идентификаторы (INVALID_IDENTIFIER);
5. локали: формат, дубликаты active_locales, принадлежность default,
   формат запрошенной локали (INVALID_LOCALE);
6. состав/версии пакетов: keyset language_pack_versions, валидация каждого
   пакета, missing/extra/duplicate локали пакетов (PACK_SET_MISMATCH), затем
   совпадение версий пакета с mapping (PACK_VERSION_MISMATCH);
7. содержимое шаблонов — по каждому template в порядке вставки ключей:
   точный fieldset (INVALID_SCHEMA) → kind (INVALID_TEMPLATE) → совпадение
   template.locale с pack.locale (TEMPLATE_LOCALE_MISMATCH) → subject/body
   (INVALID_TEMPLATE) → структура fact_ids и дубликаты (INVALID_TEMPLATE) →
   принадлежность ссылок approved_fact_ids (UNAPPROVED_FACT).

Для build_policy_language_fields активность запрошенной локали проверяется
после всех стадий (LOCALE_NOT_ACTIVE), чтобы статус активности не перекрывал
детальные ошибки структуры.
"""
import copy
import re

# identifier из ТЗ §4: латиница/цифры и знаки . _ : -, max 128 символов.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# locale_id из ТЗ §4: локальный формат ID, не полный BCP47-парсер;
# тире-сегменты после первого слова; длина 1..64; регистр сохраняется.
_LOCALE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")
# Пять допустимых kind шаблона из ТЗ §5.
_TEMPLATE_KINDS = frozenset(("INITIAL", "QUESTION", "OBJECTION", "FOLLOWUP", "HANDOFF"))
# Точные маркеры схем и «ровные» наборы ключей из ТЗ §§5-6.
_PACK_SCHEMA = "FIRST_LANGUAGE_PACK_V1"
_REGISTRY_SCHEMA = "FIRST_LANGUAGE_REGISTRY_V1"
_REGISTRY_LOADED_SCHEMA = "FIRST_LANGUAGE_REGISTRY_LOADED_V1"
_PACK_KEYS = frozenset(("schema_version", "locale", "version", "templates"))
_TEMPLATE_KEYS = frozenset(("kind", "locale", "subject", "body", "fact_ids"))
_REGISTRY_KEYS = frozenset(("schema_version", "registry_version", "default_locale",
                            "active_locales", "language_pack_versions"))
_REGISTRY_LOADED_KEYS = frozenset((
    "schema_version", "registry_version", "default_locale", "active_locales",
    "language_pack_versions", "approved_fact_ids", "packs_by_locale"))


class LanguageRegistryError(ValueError):
    """Ошибка валидации, несущая только машиночитаемый код.

    code: str; str(error) возвращает только code — без входных данных,
    локалей и текстов шаблонов (проверено тестом с sentinel-данными).
    Наследует ValueError по контракту ТЗ §3.
    """

    def __init__(self, code):
        # В args хранится только код: никаких деталей входов не попадает
        # в исключение, поэтому утечка данных через print/error невозможна.
        super().__init__(code)

    @property
    def code(self):
        return self.args[0]

    def __str__(self):
        # Возврат ровно кода: str(error) == code (ТЗ §3/§8).
        return self.args[0]


def _require(ok, code):
    # Паттерн проекта (как в sales_contracts._require): булево условие + код.
    if not ok:
        raise LanguageRegistryError(code)


def _is_identifier(value):
    # identifier: полнотекстовый fullmatch regex из ТЗ; не-строка — не identifier.
    return isinstance(value, str) and bool(_IDENTIFIER_RE.fullmatch(value))


def _is_locale(value):
    # locale_id: fullmatch regex, длина 1..64 (ТЗ §4); сравнение точное,
    # регистр сохраняется, enum языков не зашивается.
    return (isinstance(value, str) and 1 <= len(value) <= 64
            and bool(_LOCALE_RE.fullmatch(value)))


def _is_text(value):
    # text из ТЗ §4: непустая после strip(); значение возвращается без
    # изменения — Unicode/пробелы сохраняются, strip применяется только
    # для проверки, не для нормализации.
    return isinstance(value, str) and bool(value.strip())


def _checked_fact_set(approved_fact_ids):
    """Проверить список approved_fact_ids и вернуть его как set.

    Пустой список допустим (ТЗ §4: пакеты без фактических утверждений).
    Ошибки: не-список, не-identifier элемент, дубликат — INVALID_FACT_SET.
    Возвращаемое set используется только для мембершип-проверок fact_ids.
    """
    if not isinstance(approved_fact_ids, list):
        raise LanguageRegistryError("INVALID_FACT_SET")
    seen = set()
    for fact_id in approved_fact_ids:
        # Порядок условий даёт детерминизм: сначала формат элемента,
        # затем уникальность; не-строковые элементы невозможны тут только
        # из-за _is_identifier, поэтому set.add безопасен.
        if not _is_identifier(fact_id) or fact_id in seen:
            raise LanguageRegistryError("INVALID_FACT_SET")
        seen.add(fact_id)
    return seen


def _validate_pack_shape(pack, fact_set):
    """Проверить один языковой пакет и вернуть его глубокую копию.

    Используется напрямую load_language_registry и re-валидацией в
    build_policy_language_fields. Публичный validate_language_pack добавляет
    лишь проверку базового типа и fact_set. Стадии 3-7.
    """
    if not isinstance(pack, dict):
        raise LanguageRegistryError("INVALID_INPUT")
    # Стадия 3: schema_version первым, затем точный набор ключей pack —
    # любые лишние/недостающие поля (включая verified/approved/grant-маркеры)
    # отсекаются здесь.
    if pack.get("schema_version") != _PACK_SCHEMA or set(pack) != _PACK_KEYS:
        raise LanguageRegistryError("INVALID_SCHEMA")
    # Стадия 4: version — identifier.
    if not _is_identifier(pack["version"]):
        raise LanguageRegistryError("INVALID_IDENTIFIER")
    # Стадия 5: locale pack — валидная локаль; доступ по ключу безопасен,
    # т.к. fieldset уже проверен.
    if not _is_locale(pack["locale"]):
        raise LanguageRegistryError("INVALID_LOCALE")
    # templates: непустой dict (ТЗ §5); тип/пустота — INVALID_SCHEMA.
    templates = pack["templates"]
    if not isinstance(templates, dict) or not templates:
        raise LanguageRegistryError("INVALID_SCHEMA")
    # Стадии 4+7: перебор шаблонов в порядке вставки ключей (для JSON-объектов
    # порядок детерминирован) — «первая ошибка» воспроизводима.
    for template_id, template in templates.items():
        # Ключ templates — тоже identifier (ТЗ §5).
        if not _is_identifier(template_id):
            raise LanguageRegistryError("INVALID_IDENTIFIER")
        # Точный fieldset шаблона: {kind, locale, subject, body, fact_ids}.
        if not isinstance(template, dict) or set(template) != _TEMPLATE_KEYS:
            raise LanguageRegistryError("INVALID_SCHEMA")
        # kind — строка из пяти значений ТЗ (unknown kind → INVALID_TEMPLATE).
        # Тип проверяется явно ДО membership в frozenset: не-hashable значения
        # (списки/словари) в противном случае вызывали бы TypeError (возврат
        # независимой проверки 12.10).
        kind = template["kind"]
        if not isinstance(kind, str) or kind not in _TEMPLATE_KINDS:
            raise LanguageRegistryError("INVALID_TEMPLATE")
        # locale шаблона обязан совпасть с pack.locale (ТЗ §5). Не-строковое
        # значение тоже не совпадёт — TEMPLATE_LOCALE_MISMATCH.
        if template["locale"] != pack["locale"]:
            raise LanguageRegistryError("TEMPLATE_LOCALE_MISMATCH")
        subject = template["subject"]
        # subject — строка; для INITIAL непустая после strip (ТЗ §5);
        # у остальных kind пустая допустима; значение сохраняется как есть.
        if not isinstance(subject, str):
            raise LanguageRegistryError("INVALID_TEMPLATE")
        if template["kind"] == "INITIAL" and not subject.strip():
            raise LanguageRegistryError("INVALID_TEMPLATE")
        # body — text (непустой после strip); хранится как непрозрачный текст:
        # подстроки-«команды» не интерпретируются и не рендерятся.
        if not _is_text(template["body"]):
            raise LanguageRegistryError("INVALID_TEMPLATE")
        # Стадия 7 (fact_ids): тип list проверяется ЯВНО до обхода — по ТЗ
        # допускается только список; не-список (строки/словари итерировались
        # бы молча, None/числа/булевы вызывали бы TypeError) → INVALID_TEMPLATE.
        # Пустой список [] остаётся допустимым (ТЗ §5).
        fact_ids = template["fact_ids"]
        if not isinstance(fact_ids, list):
            raise LanguageRegistryError("INVALID_TEMPLATE")
        seen_facts = set()
        # Структура fact_ids: элементы — identifier, без дубликатов; один
        # цикл даёт детерминированный порядок ошибок.
        for fact_id in fact_ids:
            if not _is_identifier(fact_id) or fact_id in seen_facts:
                raise LanguageRegistryError("INVALID_TEMPLATE")
            seen_facts.add(fact_id)
        # Отдельным проходом — принадлежность ссылок approved_fact_ids:
        # структурно корректная, но неодобренная ссылка → UNAPPROVED_FACT.
        for fact_id in fact_ids:
            if fact_id not in fact_set:
                raise LanguageRegistryError("UNAPPROVED_FACT")
    # Полная валидация пройдена: возвращаем глубокую копию, независимую от
    # входа (результат не разделяет вложенные mutable-объекты).
    return copy.deepcopy(pack)


def validate_language_pack(pack, *, approved_fact_ids):
    """Проверить один языковой пакет и вернуть независимую глубокую копию.

    Возврат сохраняет те же значения без добавления verified/approved/grant
    полей (ТЗ §5). Входные объекты не изменяются.
    """
    if not isinstance(pack, dict):
        raise LanguageRegistryError("INVALID_INPUT")
    fact_set = _checked_fact_set(approved_fact_ids)
    return _validate_pack_shape(pack, fact_set)


def load_language_registry(registry, packs, *, approved_fact_ids):
    """Проверить реестр вместе с пакетами и вернуть загруженный результат.

    Результат — dict ровно по схеме FIRST_LANGUAGE_REGISTRY_LOADED_V1 (§6).
    Дополнительные пакеты не считываются, fallback на случайный язык
    отсутствует; для пакета без реестр-версии — PACK_VERSION_MISMATCH.
    """
    # Стадия 1: registry — dict, packs — list.
    if not isinstance(registry, dict) or not isinstance(packs, list):
        raise LanguageRegistryError("INVALID_INPUT")
    # Стадия 2: один и тот же проверенный список факторов для всех пакетов.
    fact_set = _checked_fact_set(approved_fact_ids)
    # Стадия 3: schema_version, затем «ровные» 5 ключей реестра.
    if (registry.get("schema_version") != _REGISTRY_SCHEMA
            or set(registry) != _REGISTRY_KEYS):
        raise LanguageRegistryError("INVALID_SCHEMA")
    versions = registry["language_pack_versions"]
    # Стадия 4: registry_version — identifier.
    if not _is_identifier(registry["registry_version"]):
        raise LanguageRegistryError("INVALID_IDENTIFIER")
    # mapping версий: тип dict (INVALID_SCHEMA), значения — identifier.
    if not isinstance(versions, dict):
        raise LanguageRegistryError("INVALID_SCHEMA")
    for version in versions.values():
        if not _is_identifier(version):
            raise LanguageRegistryError("INVALID_IDENTIFIER")
    # Стадия 5: default_locale валидна.
    if not _is_locale(registry["default_locale"]):
        raise LanguageRegistryError("INVALID_LOCALE")
    # active_locales: непустой list, элементы валидны, дубликат локали —
    # INVALID_LOCALE (ТЗ §8).
    active = registry["active_locales"]
    if not isinstance(active, list) or not active:
        raise LanguageRegistryError("INVALID_LOCALE")
    seen_locales = set()
    for locale in active:
        if not _is_locale(locale) or locale in seen_locales:
            raise LanguageRegistryError("INVALID_LOCALE")
        seen_locales.add(locale)
    # default_locale обязан входить в active_locales.
    if registry["default_locale"] not in seen_locales:
        raise LanguageRegistryError("INVALID_LOCALE")
    # Стадия 6 (keyset mapping): ключи language_pack_versions ровно совпадают
    # с активным набором локалей — «неверный keyset» → PACK_SET_MISMATCH.
    if set(versions) != seen_locales:
        raise LanguageRegistryError("PACK_SET_MISMATCH")
    # Валидация каждого пакета (стадия 7 внутри) с общим fact_set.
    packs_by_locale = {}
    for pack in packs:
        validated = _validate_pack_shape(pack, fact_set)
        locale = pack["locale"]
        # Повтор локали пакета → PACK_SET_MISMATCH (ТЗ §6).
        if locale in packs_by_locale:
            raise LanguageRegistryError("PACK_SET_MISMATCH")
        packs_by_locale[locale] = validated
    # Состав набора: нет missing (недостающего пакета) и нет extra (лишнего
    # неактивного пакета); иначе PACK_SET_MISMATCH.
    if set(packs_by_locale) != seen_locales:
        raise LanguageRegistryError("PACK_SET_MISMATCH")
    # Версии: для каждой активной локали в порядке реестра — совпадение
    # версии пакета с mapping (ТЗ §6).
    for locale in active:
        if packs_by_locale[locale]["version"] != versions[locale]:
            raise LanguageRegistryError("PACK_VERSION_MISMATCH")
    # Результат — ровно 7 ключей (ТЗ §6); все значения — независимые копии
    # (пакеты — копии из валидации; списки/mapping — deepcopy).
    return {
        "schema_version": _REGISTRY_LOADED_SCHEMA,
        "registry_version": registry["registry_version"],
        "default_locale": registry["default_locale"],
        "active_locales": copy.deepcopy(active),
        "language_pack_versions": copy.deepcopy(versions),
        "approved_fact_ids": copy.deepcopy(approved_fact_ids),
        "packs_by_locale": packs_by_locale,
    }


def build_policy_language_fields(loaded, *, locale):
    """Ревалидировать loaded и спроецировать минимальные поля policy (Camel).

    Функция не доверяет произвольному dict только из-за schema_version
    (ТЗ §7): вся структура loaded перепроверяется тем же алгоритмом. Это
    защищает от случайной мутации объекта между вызовами. Возврат — ровно
    {locale, allowed_locales, language_pack_versions, approved_templates}.
    """
    # Стадии 1 (типы): loaded — dict, запрошенная locale — строка.
    if not isinstance(loaded, dict):
        raise LanguageRegistryError("INVALID_INPUT")
    if not isinstance(locale, str):
        raise LanguageRegistryError("INVALID_INPUT")
    # Стадия 2: корректность approved_fact_ids внутри loaded.
    fact_ids = loaded.get("approved_fact_ids")
    fact_set = _checked_fact_set(fact_ids)
    # Стадия 3: schema_version, затем «ровные» 7 ключей результата загрузки.
    if (loaded.get("schema_version") != _REGISTRY_LOADED_SCHEMA
            or set(loaded) != _REGISTRY_LOADED_KEYS):
        raise LanguageRegistryError("INVALID_SCHEMA")
    versions = loaded["language_pack_versions"]
    if not _is_identifier(loaded["registry_version"]):
        raise LanguageRegistryError("INVALID_IDENTIFIER")
    if not isinstance(versions, dict):
        raise LanguageRegistryError("INVALID_SCHEMA")
    for version in versions.values():
        if not _is_identifier(version):
            raise LanguageRegistryError("INVALID_IDENTIFIER")
    # Стадии 5: структура локалей re-проверяется полностью.
    if not _is_locale(loaded["default_locale"]):
        raise LanguageRegistryError("INVALID_LOCALE")
    active = loaded["active_locales"]
    if not isinstance(active, list) or not active:
        raise LanguageRegistryError("INVALID_LOCALE")
    seen_locales = set()
    for active_locale in active:
        if not _is_locale(active_locale) or active_locale in seen_locales:
            raise LanguageRegistryError("INVALID_LOCALE")
        seen_locales.add(active_locale)
    if loaded["default_locale"] not in seen_locales:
        raise LanguageRegistryError("INVALID_LOCALE")
    # Формат запрошенной локали: невалидный → INVALID_LOCALE (активность
    # проверяется позже, статус не перекрывает стадии ТЗ).
    if not _is_locale(locale):
        raise LanguageRegistryError("INVALID_LOCALE")
    if set(versions) != seen_locales:
        raise LanguageRegistryError("PACK_SET_MISMATCH")
    packs_by_locale = loaded["packs_by_locale"]
    if not isinstance(packs_by_locale, dict):
        raise LanguageRegistryError("INVALID_SCHEMA")
    # Keyset packs_by_locale обязан точно совпадать с активным набором.
    if set(packs_by_locale) != seen_locales:
        raise LanguageRegistryError("PACK_SET_MISMATCH")
    # Полная ревалидация пакетов (стадия 7 + версии) по активному порядку.
    # Сверх проверки keyset каждый пакет повторно сверяется СО СВОИМ КЛЮЧОМ:
    # перестановка двух пакетов между ключами реестра (например EN/RU при
    # одинаковой версии) не нарушает ни keyset, ни версии — нарушение
    # соответствия pack.locale ключу → PACK_SET_MISMATCH (возврат
    # независимой проверки 12.10).
    for active_locale in active:
        pack = _validate_pack_shape(packs_by_locale[active_locale], fact_set)
        if pack["locale"] != active_locale:
            raise LanguageRegistryError("PACK_SET_MISMATCH")
        if pack["version"] != versions[active_locale]:
            raise LanguageRegistryError("PACK_VERSION_MISMATCH")
    # Активность запрошенной локали — последняя стадия (ТЗ §8).
    if locale not in seen_locales:
        raise LanguageRegistryError("LOCALE_NOT_ACTIVE")
    # Проекция шаблонов выбранного пакета: ровно body/fact_ids/locale на
    # каждый template_id; kind/subject остаются в packs_by_locale и в
    # проекцию не попадают (ТЗ §7). Все значения — независимые копии.
    selected = packs_by_locale[locale]
    approved_templates = {}
    for template_id, template in selected["templates"].items():
        approved_templates[template_id] = {
            "body": template["body"],
            "fact_ids": copy.deepcopy(template["fact_ids"]),
            "locale": locale,
        }
    return {
        "locale": locale,
        "allowed_locales": copy.deepcopy(active),
        "language_pack_versions": copy.deepcopy(versions),
        "approved_templates": approved_templates,
    }