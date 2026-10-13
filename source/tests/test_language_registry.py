"""Тесты sales_language_registry: загрузка/проверка реестра языков FIRST.

Покрытие: полный EN/RU пример из ТЗ (точное равенство результата),
отрицательные примеры §9, расширяемость синтетическим третьим языком (de-DE
через fixtures) и вся тестовая матрица §10. Все негативные кейсы работают на
глубоких копиях fixture-корпуса и изолируют ровно одну ошибку: ожидается
конкретный код LanguageRegistryError, и str(error) равен только этому коду.
Algorithm stage order см. в докстринге модуля sales_language_registry.
"""
import ast
import copy
import json
import pathlib
import unittest

import sales_language_registry
from sales_language_registry import (
    LanguageRegistryError,
    validate_language_pack,
    load_language_registry,
    build_policy_language_fields,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "first_language_registry"

SENTINEL = "PRIVATE-sentinel-82zq data"


def _load_fixture(name):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


def _pack(*, locale="en-GB", version="en-v1", template_id="explain",
          kind="QUESTION", subject="", body="Synthetic approved example.",
          fact_ids=None):
    return {
        "schema_version": "FIRST_LANGUAGE_PACK_V1",
        "locale": locale,
        "version": version,
        "templates": {
            template_id: {
                "kind": kind,
                "locale": locale,
                "subject": subject,
                "body": body,
                "fact_ids": ["fact-test"] if fact_ids is None else fact_ids,
            }
        },
    }


def _registry(active=("en-GB", "ru-RU"), default="en-GB",
              versions=None, registry_version="registry-v1"):
    if versions is None:
        versions = {"en-GB": "en-v1", "ru-RU": "ru-v1"}
    return {
        "schema_version": "FIRST_LANGUAGE_REGISTRY_V1",
        "registry_version": registry_version,
        "default_locale": default,
        "active_locales": list(active),
        "language_pack_versions": versions,
    }


def _packs():
    return [_pack(), _pack(locale="ru-RU", version="ru-v1",
                           body="Синтетический пример для проверки.",
                           fact_ids=["fact-test"])]


def _copy_corpus():
    registry = _registry()
    packs = _packs()
    approved = ["fact-test"]
    return copy.deepcopy(registry), copy.deepcopy(packs), copy.deepcopy(approved)


def _expect_code(assert_that, code, function, *args, **kwargs):
    with assert_that.assertRaises(LanguageRegistryError) as raised:
        function(*args, **kwargs)
    error = raised.exception
    assert_that.assertEqual(str(error), code)
    assert_that.assertEqual(error.code, code)
    assert_that.assertIsInstance(error, ValueError)


class LanguageRegistryTest(unittest.TestCase):
    def test_full_en_ru_example_matches_expected_result(self):
        approved = ["fact-test"]
        registry = {
            "schema_version": "FIRST_LANGUAGE_REGISTRY_V1",
            "registry_version": "registry-v1",
            "default_locale": "en-GB",
            "active_locales": ["en-GB", "ru-RU"],
            "language_pack_versions": {"en-GB": "en-v1", "ru-RU": "ru-v1"},
        }
        packs = [
            {"schema_version": "FIRST_LANGUAGE_PACK_V1", "locale": "en-GB", "version": "en-v1",
             "templates": {"explain": {"kind": "QUESTION", "locale": "en-GB", "subject": "",
                                       "body": "Synthetic approved example.",
                                       "fact_ids": ["fact-test"]}}},
            {"schema_version": "FIRST_LANGUAGE_PACK_V1", "locale": "ru-RU", "version": "ru-v1",
             "templates": {"explain": {"kind": "QUESTION", "locale": "ru-RU", "subject": "",
                                       "body": "Синтетический пример для проверки.",
                                       "fact_ids": ["fact-test"]}}},
        ]
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        self.assertEqual(loaded, {
            "schema_version": "FIRST_LANGUAGE_REGISTRY_LOADED_V1",
            "registry_version": "registry-v1",
            "default_locale": "en-GB",
            "active_locales": ["en-GB", "ru-RU"],
            "language_pack_versions": {"en-GB": "en-v1", "ru-RU": "ru-v1"},
            "approved_fact_ids": ["fact-test"],
            "packs_by_locale": {
                "en-GB": packs[0],
                "ru-RU": packs[1],
            },
        })
        result = build_policy_language_fields(loaded, locale="ru-RU")
        self.assertEqual(result, {
            "locale": "ru-RU",
            "allowed_locales": ["en-GB", "ru-RU"],
            "language_pack_versions": {"en-GB": "en-v1", "ru-RU": "ru-v1"},
            "approved_templates": {"explain": {"body": "Синтетический пример для проверки.",
                                               "fact_ids": ["fact-test"], "locale": "ru-RU"}},
        })

    def test_synthetic_third_language_extends_without_code_changes(self):
        registry = _load_fixture("registry_three_locales.json")
        packs = [_load_fixture("pack_en_gb.json"), _load_fixture("pack_ru_ru.json"),
                 _load_fixture("pack_de_de.json")]
        loaded = load_language_registry(registry, packs, approved_fact_ids=["fact-test"])
        self.assertEqual(loaded["active_locales"], ["en-GB", "ru-RU", "de-DE"])
        result = build_policy_language_fields(loaded, locale="de-DE")
        self.assertEqual(result["locale"], "de-DE")
        self.assertEqual(result["approved_templates"]["explain"]["body"],
                         "Synthetisches Beispiel zur Erweiterung.")

    def test_same_template_id_across_locales_keeps_bodies_separate(self):
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        self.assertEqual(loaded["packs_by_locale"]["en-GB"]["templates"]["explain"]["body"],
                         "Synthetic approved example.")
        self.assertEqual(loaded["packs_by_locale"]["ru-RU"]["templates"]["explain"]["body"],
                         "Синтетический пример для проверки.")
        en_result = build_policy_language_fields(loaded, locale="en-GB")
        ru_result = build_policy_language_fields(loaded, locale="ru-RU")
        self.assertNotEqual(en_result["approved_templates"]["explain"]["body"],
                            ru_result["approved_templates"]["explain"]["body"])
        self.assertEqual(en_result["approved_templates"]["explain"]["locale"], "en-GB")
        self.assertEqual(ru_result["approved_templates"]["explain"]["locale"], "ru-RU")

    def test_duplicate_pack_locale_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs.append(copy.deepcopy(packs[1]))
        _expect_code(self, "PACK_SET_MISMATCH", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_missing_pack_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs.pop()
        _expect_code(self, "PACK_SET_MISMATCH", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_extra_inactive_pack_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs.append(_pack(locale="it-IT", version="it-v1"))
        _expect_code(self, "PACK_SET_MISMATCH", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_pack_version_mismatch_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[1]["version"] = "ru-v2"
        _expect_code(self, "PACK_VERSION_MISMATCH", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_template_locale_mismatch_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[1]["templates"]["explain"]["locale"] = "en-GB"
        _expect_code(self, "TEMPLATE_LOCALE_MISMATCH", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_template_locale_non_string_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[1]["templates"]["explain"]["locale"] = 42
        _expect_code(self, "TEMPLATE_LOCALE_MISMATCH", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_unapproved_fact_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[1]["templates"]["explain"]["fact_ids"] = ["not-approved"]
        _expect_code(self, "UNAPPROVED_FACT", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_duplicate_active_locale_rejected(self):
        registry, packs, approved = _copy_corpus()
        registry["active_locales"].append("ru-RU")
        _expect_code(self, "INVALID_LOCALE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_default_locale_not_in_active_rejected(self):
        registry, packs, approved = _copy_corpus()
        registry["default_locale"] = "fi-FI"
        _expect_code(self, "INVALID_LOCALE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_version_mapping_keyset_mismatch_rejected(self):
        registry, packs, approved = _copy_corpus()
        registry["language_pack_versions"]["it-IT"] = "it-v1"
        _expect_code(self, "PACK_SET_MISMATCH", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_invalid_pack_schema_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[0]["schema_version"] = "FIRST_LANGUAGE_PACK_V2"
        _expect_code(self, "INVALID_SCHEMA", load_language_registry,
                     registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        del packs[0]["locale"]
        _expect_code(self, "INVALID_SCHEMA", load_language_registry,
                     registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        packs[0]["verified"] = True
        _expect_code(self, "INVALID_SCHEMA", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_invalid_registry_schema_rejected(self):
        registry, packs, approved = _copy_corpus()
        registry["schema_version"] = "FIRST_LANGUAGE_REGISTRY_V2"
        _expect_code(self, "INVALID_SCHEMA", load_language_registry,
                     registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        del registry["active_locales"]
        _expect_code(self, "INVALID_SCHEMA", load_language_registry,
                     registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        registry["bonus"] = 1
        _expect_code(self, "INVALID_SCHEMA", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_invalid_loaded_schema_rejected_by_build(self):
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        damaged = copy.deepcopy(loaded)
        damaged["schema_version"] = "FIRST_LANGUAGE_REGISTRY_LOADED_V2"
        _expect_code(self, "INVALID_SCHEMA", build_policy_language_fields,
                     damaged, locale="en-GB")
        damaged = copy.deepcopy(loaded)
        damaged.pop("packs_by_locale")
        _expect_code(self, "INVALID_SCHEMA", build_policy_language_fields,
                     damaged, locale="en-GB")
        damaged = copy.deepcopy(loaded)
        damaged["extra_field"] = True
        _expect_code(self, "INVALID_SCHEMA", build_policy_language_fields,
                     damaged, locale="en-GB")

    def test_unknown_kind_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[1]["templates"]["explain"]["kind"] = "unknownkind"
        _expect_code(self, "INVALID_TEMPLATE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_initial_empty_subject_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[0] = _pack(kind="INITIAL", subject="")
        _expect_code(self, "INVALID_TEMPLATE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_non_initial_spaces_only_subject_stored_unchanged(self):
        registry, packs, approved = _copy_corpus()
        packs[1]["templates"]["explain"]["subject"] = "  "
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        self.assertEqual(loaded["packs_by_locale"]["ru-RU"]["templates"]["explain"]["subject"], "  ")

    def test_body_whitespace_or_non_string_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[0]["templates"]["explain"]["body"] = "  "
        _expect_code(self, "INVALID_TEMPLATE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        packs[0]["templates"]["explain"]["body"] = 123
        _expect_code(self, "INVALID_TEMPLATE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_invalid_locale_format_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[0]["locale"] = "en_GB"
        packs[0]["templates"]["explain"]["locale"] = "en_GB"
        _expect_code(self, "INVALID_LOCALE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        registry["default_locale"] = ""
        _expect_code(self, "INVALID_LOCALE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        packs[0]["locale"] = 123
        _expect_code(self, "INVALID_LOCALE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_query_locale_not_active_rejected(self):
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        _expect_code(self, "LOCALE_NOT_ACTIVE", build_policy_language_fields,
                     loaded, locale="de-DE")

    def test_query_locale_invalid_format_rejected(self):
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        _expect_code(self, "INVALID_LOCALE", build_policy_language_fields,
                     loaded, locale="de_DE")
        _expect_code(self, "INVALID_LOCALE", build_policy_language_fields,
                     loaded, locale="")

    def test_query_locale_non_string_rejected(self):
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        _expect_code(self, "INVALID_INPUT", build_policy_language_fields,
                     loaded, locale=None)
        _expect_code(self, "INVALID_INPUT", build_policy_language_fields, loaded, locale=7)

    def test_invalid_identifiers_rejected(self):
        registry, packs, approved = _copy_corpus()
        registry["registry_version"] = "-broken"
        _expect_code(self, "INVALID_IDENTIFIER", load_language_registry,
                     registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        packs[0]["version"] = ""
        _expect_code(self, "INVALID_IDENTIFIER", load_language_registry,
                     registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        del packs[0]["templates"]["explain"]
        packs[0]["templates"]["#bad"] = {
            "kind": "QUESTION", "locale": "en-GB", "subject": "", "body": "b",
            "fact_ids": ["fact-test"]}
        _expect_code(self, "INVALID_IDENTIFIER", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_invalid_fact_set_rejected(self):
        registry, packs, approved = _copy_corpus()
        _expect_code(self, "INVALID_FACT_SET", load_language_registry,
                     registry, packs, approved_fact_ids="fact-test")
        _expect_code(self, "INVALID_FACT_SET", load_language_registry,
                     registry, packs, approved_fact_ids=["fact-test", "fact-test"])

    def test_non_identifier_fact_entry_rejected(self):
        registry, packs, approved = _copy_corpus()
        _expect_code(self, "INVALID_FACT_SET", load_language_registry,
                     registry, packs, approved_fact_ids=["bad fact!"])
        _expect_code(self, "INVALID_FACT_SET", validate_language_pack,
                     packs[0], approved_fact_ids=[42])
        _expect_code(self, "INVALID_FACT_SET", validate_language_pack,
                     packs[0], approved_fact_ids=None)

    def test_duplicate_fact_id_in_template_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[0]["templates"]["explain"]["fact_ids"] = ["fact-test", "fact-test"]
        _expect_code(self, "INVALID_TEMPLATE", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_empty_approved_fact_ids_with_empty_pack_fact_ids_allowed(self):
        registry, packs, approved = _copy_corpus()
        packs[0]["templates"]["explain"]["fact_ids"] = []
        packs[1]["templates"]["explain"]["fact_ids"] = []
        loaded = load_language_registry(registry, packs, approved_fact_ids=[])
        self.assertEqual(loaded["approved_fact_ids"], [])
        result = build_policy_language_fields(loaded, locale="en-GB")
        self.assertEqual(result["approved_templates"]["explain"]["fact_ids"], [])

    def test_empty_templates_rejected(self):
        registry, packs, approved = _copy_corpus()
        packs[0]["templates"] = {}
        _expect_code(self, "INVALID_SCHEMA", load_language_registry,
                     registry, packs, approved_fact_ids=approved)

    def test_unicode_preserved_unchanged(self):
        body = "Синтетический пример — «проверка», см. https://example.com/…  "
        registry, packs, approved = _copy_corpus()
        packs[1]["templates"]["explain"]["body"] = body
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        self.assertEqual(loaded["packs_by_locale"]["ru-RU"]["templates"]["explain"]["body"], body)
        result = build_policy_language_fields(loaded, locale="ru-RU")
        self.assertEqual(result["approved_templates"]["explain"]["body"], body)

    def test_command_like_template_text_stays_text(self):
        snippet = "{name} $(echo hi) __import__('os').system('id')"
        registry, packs, approved = _copy_corpus()
        packs[0]["templates"]["explain"]["body"] = snippet
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        self.assertEqual(loaded["packs_by_locale"]["en-GB"]["templates"]["explain"]["body"], snippet)
        result = build_policy_language_fields(loaded, locale="en-GB")
        self.assertEqual(result["approved_templates"]["explain"]["body"], snippet)

    def test_loaded_result_has_exact_keys_and_no_status_fields(self):
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        self.assertEqual(
            set(loaded),
            {"schema_version", "registry_version", "default_locale", "active_locales",
             "language_pack_versions", "approved_fact_ids", "packs_by_locale"})
        for pack in loaded["packs_by_locale"].values():
            self.assertEqual(set(pack), {"schema_version", "locale", "version", "templates"})
            self.assertNotIn("verified", pack)
            self.assertNotIn("approved", pack)
            self.assertNotIn("grant", pack)
        result = build_policy_language_fields(loaded, locale="en-GB")
        self.assertEqual(set(result),
                         {"locale", "allowed_locales", "language_pack_versions", "approved_templates"})

    def test_inputs_not_mutated_and_no_shared_nested_objects(self):
        registry, packs, approved = _copy_corpus()
        snapshot = copy.deepcopy((registry, packs, approved))
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        self.assertEqual((registry, packs, approved), snapshot)
        result = build_policy_language_fields(loaded, locale="ru-RU")
        # Mutate the results; originals must stay independent.
        loaded["packs_by_locale"]["ru-RU"]["templates"]["explain"]["fact_ids"].append("fact-x")
        loaded["active_locales"].append("xx-XX")
        result["approved_templates"]["explain"]["body"] = "mutated"
        result["approved_templates"]["explain"]["fact_ids"].append("fact-x")
        self.assertEqual((registry, packs, approved), snapshot)
        # Mutate the originals; previous results must stay independent.
        packs[1]["templates"]["explain"]["fact_ids"].append("fact-y")
        registry["active_locales"].append("yy-YY")
        self.assertEqual(loaded["packs_by_locale"]["ru-RU"]["templates"]["explain"]["fact_ids"],
                         ["fact-test", "fact-x"])
        self.assertEqual(loaded["active_locales"], ["en-GB", "ru-RU", "xx-XX"])

    def test_repeated_calls_are_equal(self):
        registry, packs, approved = _copy_corpus()
        loaded_one = load_language_registry(registry, packs, approved_fact_ids=approved)
        loaded_two = load_language_registry(registry, packs, approved_fact_ids=approved)
        self.assertEqual(loaded_one, loaded_two)
        self.assertIsNot(loaded_one, loaded_two)
        first = build_policy_language_fields(loaded_one, locale="en-GB")
        second = build_policy_language_fields(loaded_two, locale="en-GB")
        self.assertEqual(first, second)

    def test_damaged_loaded_rejected_on_revalidation(self):
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        damaged = copy.deepcopy(loaded)
        damaged["packs_by_locale"]["ru-RU"]["templates"]["explain"]["body"] = "   "
        _expect_code(self, "INVALID_TEMPLATE", build_policy_language_fields,
                     damaged, locale="ru-RU")
        damaged = copy.deepcopy(loaded)
        del damaged["packs_by_locale"]["ru-RU"]
        _expect_code(self, "PACK_SET_MISMATCH", build_policy_language_fields,
                     damaged, locale="ru-RU")
        damaged = copy.deepcopy(loaded)
        damaged["language_pack_versions"]["ru-RU"] = "ru-v0"
        _expect_code(self, "PACK_VERSION_MISMATCH", build_policy_language_fields,
                     damaged, locale="ru-RU")
        damaged = copy.deepcopy(loaded)
        damaged["packs_by_locale"]["ru-RU"]["templates"]["explain"]["fact_ids"] = ["not-approved"]
        _expect_code(self, "UNAPPROVED_FACT", build_policy_language_fields,
                     damaged, locale="ru-RU")

    def test_fact_ids_invalid_types_via_public_functions(self):
        # Возврат независимой проверки: fact_ids допускается только список.
        # "" и {} итерировались молча (пустой обход), None/42/False давали
        # TypeError — теперь INVALID_TEMPLATE во всех трёх функциях.
        for bad in ("", {}, None, 42, False):
            pack = _pack()
            pack["templates"]["explain"]["fact_ids"] = bad
            _expect_code(self, "INVALID_TEMPLATE", validate_language_pack,
                         pack, approved_fact_ids=["fact-test"])
            registry, packs, approved = _copy_corpus()
            packs[0]["templates"]["explain"]["fact_ids"] = bad
            _expect_code(self, "INVALID_TEMPLATE", load_language_registry,
                         registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        damaged = copy.deepcopy(loaded)
        damaged["packs_by_locale"]["ru-RU"]["templates"]["explain"]["fact_ids"] = None
        _expect_code(self, "INVALID_TEMPLATE", build_policy_language_fields,
                     damaged, locale="ru-RU")

    def test_kind_invalid_types_via_public_functions(self):
        # Возврат независимой проверки: [] и {} вызывали TypeError при
        # membership в frozenset (не-hashable) — теперь строковая проверка
        # до вхождения, INVALID_TEMPLATE во всех трёх функциях.
        for bad in ([], {}, None, 42, False):
            _expect_code(self, "INVALID_TEMPLATE", validate_language_pack,
                         _pack(kind=bad), approved_fact_ids=["fact-test"])
            registry, packs, approved = _copy_corpus()
            packs[0]["templates"]["explain"]["kind"] = bad
            _expect_code(self, "INVALID_TEMPLATE", load_language_registry,
                         registry, packs, approved_fact_ids=approved)
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        damaged = copy.deepcopy(loaded)
        damaged["packs_by_locale"]["ru-RU"]["templates"]["explain"]["kind"] = []
        _expect_code(self, "INVALID_TEMPLATE", build_policy_language_fields,
                     damaged, locale="ru-RU")

    def test_swapped_packs_with_equal_versions_rejected(self):
        # Возврат независимой проверки: перестановка пакетов между ключами
        # реестра при одинаковых версиях не ломает ни keyset, ни версии —
        # ловит только сверка pack.locale с ключом packs_by_locale.
        versions = {"en-GB": "v1", "ru-RU": "v1"}
        registry = _registry(versions=versions)
        packs = [_pack(version="v1"),
                 _pack(locale="ru-RU", version="v1",
                       body="Синтетический пример для проверки.")]
        loaded = load_language_registry(registry, packs, approved_fact_ids=["fact-test"])
        swapped = copy.deepcopy(loaded)
        swapped["packs_by_locale"]["en-GB"], swapped["packs_by_locale"]["ru-RU"] = (
            swapped["packs_by_locale"]["ru-RU"], swapped["packs_by_locale"]["en-GB"])
        _expect_code(self, "PACK_SET_MISMATCH", build_policy_language_fields,
                     swapped, locale="en-GB")
        _expect_code(self, "PACK_SET_MISMATCH", build_policy_language_fields,
                     swapped, locale="ru-RU")

    def test_pack_inner_locale_inactive_or_duplicate_rejected(self):
        # Неактивная внутренняя локаль: пакет с валидной структурой, но
        # pack.locale вне активного набора под существующим ключом.
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        damaged = copy.deepcopy(loaded)
        damaged["packs_by_locale"]["ru-RU"]["locale"] = "it-IT"
        damaged["packs_by_locale"]["ru-RU"]["templates"]["explain"]["locale"] = "it-IT"
        _expect_code(self, "PACK_SET_MISMATCH", build_policy_language_fields,
                     damaged, locale="ru-RU")
        # Дублирующаяся внутренняя локаль: под обоими ключами один и тот же
        # pack.locale (en-GB).
        damaged = copy.deepcopy(loaded)
        damaged["packs_by_locale"]["ru-RU"]["locale"] = "en-GB"
        damaged["packs_by_locale"]["ru-RU"]["templates"]["explain"]["locale"] = "en-GB"
        _expect_code(self, "PACK_SET_MISMATCH", build_policy_language_fields,
                     damaged, locale="en-GB")

    def test_damage_of_unrequested_locale_pack_detected(self):
        # Повреждение пакета языка, не выбранного запросом, но в активном
        # наборе: полная ревалидация ловит и его (уточнение возврата:
        # ревалидация полная по активным локалям, не только по запросу).
        registry, packs, approved = _copy_corpus()
        loaded = load_language_registry(registry, packs, approved_fact_ids=approved)
        damaged = copy.deepcopy(loaded)
        damaged["packs_by_locale"]["en-GB"]["templates"]["explain"]["body"] = "   "
        _expect_code(self, "INVALID_TEMPLATE", build_policy_language_fields,
                     damaged, locale="ru-RU")

    def test_invalid_base_types_rejected(self):
        registry, packs, approved = _copy_corpus()
        valid = _registry()
        _expect_code(self, "INVALID_INPUT", load_language_registry,
                     "not-a-dict", _packs(), approved_fact_ids=approved)
        _expect_code(self, "INVALID_INPUT", load_language_registry,
                     valid, "not-a-list", approved_fact_ids=approved)
        _expect_code(self, "INVALID_INPUT", load_language_registry,
                     valid, ["not-a-dict"], approved_fact_ids=approved)
        _expect_code(self, "INVALID_INPUT", validate_language_pack,
                     "not-a-dict", approved_fact_ids=approved)
        _expect_code(self, "INVALID_INPUT", build_policy_language_fields,
                     "not-a-dict", locale="en-GB")

    def test_error_str_returns_only_code_and_no_sentinel_leak(self):
        # Sentinel data is planted in every field that could leak into an
        # error message; str(error) must equal the bare code.
        pack = _pack(version=SENTINEL)
        _expect_code(self, "INVALID_IDENTIFIER", validate_language_pack,
                     pack, approved_fact_ids=["fact-test"])
        pack = _pack()
        pack["templates"]["explain"]["locale"] = SENTINEL
        _expect_code(self, "TEMPLATE_LOCALE_MISMATCH", validate_language_pack,
                     pack, approved_fact_ids=["fact-test"])
        pack = _pack(body=SENTINEL + " claim", fact_ids=["not-approved"])
        _expect_code(self, "UNAPPROVED_FACT", validate_language_pack,
                     pack, approved_fact_ids=["fact-test"])
        registry, packs, approved = _copy_corpus()
        packs[1]["templates"]["explain"]["body"] = SENTINEL
        packs[1]["templates"]["explain"]["fact_ids"] = [SENTINEL]
        try:
            load_language_registry(registry, packs, approved_fact_ids=approved)
            self.fail("expected an error")
        except LanguageRegistryError as error:
            self.assertEqual(str(error), "INVALID_TEMPLATE")
            self.assertNotIn(SENTINEL, str(error))
            self.assertNotIn(SENTINEL, error.code)

    def test_module_is_stdlib_only_no_network_or_driver_imports(self):
        source_path = pathlib.Path(sales_language_registry.__file__)
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, {"copy", "re"})
        for banned in ("psycopg", "socket", "requests", "urllib", "subprocess",
                       "os.environ", "__import__", "open("):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()