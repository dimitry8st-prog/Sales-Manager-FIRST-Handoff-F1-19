"""Mandatory F1-07 acceptance checks A..O against a real local PostgreSQL.

Run from the returned ``source/`` folder (task section 8)::

    python -m unittest discover -s f1_07_inbox/tests -p 'test_*.py' -v

The harness needs ``F107_TEST_DSN`` (disposable local container only) and
``F107_SCHEMA`` is not used here: every test group creates its own unique
``f1_07_test_<uuidhex>`` schema, migrates it and drops it afterwards after
re-checking the regex, the database and the exact name.  Without
``F107_TEST_DSN`` the run prints ``DB_NOT_RUN`` and exits non-zero — skipped or
mocked database tests are never reported as a successful acceptance.

Expected values here are derived from the task's stated rules (canonical JSON,
byte bounds, event_key formula), never from the module under test.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import textwrap
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from f1_07_inbox.migrate import (  # noqa: E402
    EXPECTED_CHECK_CONSTRAINTS,
    LOCAL_DBNAME,
    LOCAL_USER,
    LocalTargetError,
    apply_migration,
    local_connect_factory,
    parse_local_dsn,
)
from f1_07_inbox.store import InboxStore, InboxStoreError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA_RE = re.compile(r"^f1_07_[a-z0-9_]+$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SCHEMA_PREFIX = "f1_07_test_"
DEFAULT_CAMPAIGN = "11111111-1111-4111-8111-111111111111"
OTHER_CAMPAIGN = "22222222-2222-4222-8222-222222222222"

# The seven CHECK constraints of the fixed DDL (task section 6), written exactly as
# the task's SQL file writes them.  The builders below repeat them so that a table
# they hand to the migration matches the expected structure in every other respect
# and only the part named by the test can refuse it.
CHECK_CLAUSES = (
    "CHECK (octet_length(client_id) BETWEEN 1 AND 512)",
    "CHECK (octet_length(mailbox_ref) BETWEEN 1 AND 512)",
    "CHECK (octet_length(provider) BETWEEN 1 AND 512)",
    "CHECK (octet_length(message_id) BETWEEN 1 AND 512)",
    "CHECK (event_key ~ '^[0-9a-f]{64}$')",
    "CHECK (payload_sha256 ~ '^[0-9a-f]{64}$')",
    "CHECK (jsonb_typeof(normalized_event) = 'object')",
)


def with_checks(column_block: str) -> str:
    """Append the seven fixed CHECK clauses to a hand-written column block."""
    return column_block + ", " + ", ".join(CHECK_CLAUSES)


_MISSING = object()

LOCAL_DSN = os.environ.get("F107_TEST_DSN")
if not LOCAL_DSN:
    print(
        "DB_NOT_RUN: F107_TEST_DSN is not set; the F1-07 acceptance requires a"
        " real disposable local PostgreSQL (task sections 8 and 9).",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(2)

# One factory for the whole run; assigned as an *instance* attribute in setUp so
# that it is never bound as a method of the test case.
LOCAL_FACTORY = local_connect_factory(LOCAL_DSN, connect_timeout=5)


# --- independent expectations (from the stated rules, never from the module) --


def expected_event_key(provider: str, mailbox_ref: str, message_id: str) -> str:
    payload = json.dumps(
        [provider, mailbox_ref, message_id], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def expected_iso_utc(text: str) -> str:
    return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat()


def expected_canonical(event: dict) -> dict:
    canonical = {
        "schema_version": event["schema_version"],
        "client_id": event["client_id"],
        "runtime_campaign_id": event["runtime_campaign_id"],
        "mailbox_ref": event["mailbox_ref"],
        "provider": event["provider"],
        "message_id": event["message_id"],
        "thread_id": event["thread_id"],
        "sender_email": event["sender_email"],
        "recipient_email": event["recipient_email"],
        "received_at": expected_iso_utc(event["received_at"]),
        "body_text": event["body_text"],
        "event_kind": event["event_kind"],
        "in_reply_to": event["in_reply_to"],
        "event_key": event["event_key"],
        "auto_reply_headers": dict(event.get("auto_reply_headers") or {}),
    }
    return canonical


def expected_payload_sha256(event: dict) -> str:
    canonical = expected_canonical(event)
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def make_event(
    message_id: str = "Msg-A",
    *,
    provider: str = "fixture",
    mailbox_ref: str = "mb-test",
    client_id: str = "client-test",
    campaign: str = DEFAULT_CAMPAIGN,
    thread_id: str = "Thread-A",
    sender_email: str = "buyer@example.test",
    recipient_email: str = "seller@example.test",
    received_at: str = "2026-10-11T12:00:00+00:00",
    body_text: str = "Please explain",
    event_kind: str = "INBOUND_MESSAGE",
    in_reply_to=_MISSING,
    headers=_MISSING,
    event_key=None,
) -> dict:
    event = {
        "schema_version": "FIRST_INBOUND_V1",
        "client_id": client_id,
        "runtime_campaign_id": campaign,
        "mailbox_ref": mailbox_ref,
        "provider": provider,
        "message_id": message_id,
        "thread_id": thread_id,
        "sender_email": sender_email,
        "recipient_email": recipient_email,
        "received_at": received_at,
        "body_text": body_text,
        "event_kind": event_kind,
        "in_reply_to": None if in_reply_to is _MISSING else in_reply_to,
        "event_key": event_key or expected_event_key(provider, mailbox_ref, message_id),
    }
    if headers is not _MISSING:
        event["auto_reply_headers"] = headers
    return event


def make_binding(
    *,
    client_id: str = "client-test",
    mailbox_ref: str = "mb-test",
    campaign: str = DEFAULT_CAMPAIGN,
    sender_email: str = "seller@example.test",
) -> dict:
    return {
        "schema_version": "FIRST_INBOX_BINDING_V1",
        "client_id": client_id,
        "runtime_campaign_id": campaign,
        "mailbox_ref": mailbox_ref,
        "sender_email": sender_email,
    }


def fixture_event() -> dict:
    return json.loads((FIXTURES / "normalized_normal.json").read_text(encoding="utf-8"))


def fixture_binding() -> dict:
    return json.loads((FIXTURES / "binding_normal.json").read_text(encoding="utf-8"))


class RecordingFactory:
    """Wraps a connect factory and records every connection handed out."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0
        self.connections = []

    def __call__(self):
        self.calls += 1
        connection = self._inner()
        self.connections.append(connection)
        return connection


class TransactionSpy:
    """Wraps a connection and records what the migration does to the transaction.

    ``apply_migration`` only ever calls ``cursor()``, ``commit()``, ``rollback()``
    and ``close()`` on the object its factory returns, so wrapping it makes the
    rollback observable from the outside instead of being inferred from the
    (unchanged) table.
    """

    def __init__(self, connection):
        self._connection = connection
        self.events = []

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        # Attribute writes go to the wrapped connection as well: a test needs to be
        # able to put the connection the migration receives into autocommit mode.
        if name in ("_connection", "events"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)

    def commit(self):
        self.events.append("commit")
        return self._connection.commit()

    def rollback(self):
        self.events.append("rollback")
        return self._connection.rollback()

    def close(self):
        self.events.append("close")
        return self._connection.close()


def setUpModule() -> None:
    conn = LOCAL_FACTORY()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_database(), current_user")
            version, database, user = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    print(
        "F107_ENV server=%s database=%s user=%s python=%s psycopg=%s platform=%s"
        % (
            version.split(",")[0],
            database,
            user,
            platform.python_version(),
            psycopg.__version__,
            platform.platform(),
        ),
        flush=True,
    )


class PostgresTestCase(unittest.TestCase):
    """Base: one freshly created own schema per test, always dropped again."""

    def setUp(self):
        self.connect_factory = LOCAL_FACTORY
        self.schema = self.new_schema_name()
        self.assertTrue(SCHEMA_RE.fullmatch(self.schema), self.schema)
        self.assertLessEqual(len(self.schema.encode("utf-8")), 63)
        apply_migration(connect_factory=self.connect_factory, schema=self.schema)
        self.store = InboxStore(connect_factory=self.connect_factory, schema=self.schema)
        self.addCleanup(self.drop_own_schema, self.schema)

    @staticmethod
    def new_schema_name() -> str:
        return SCHEMA_PREFIX + uuid.uuid4().hex

    # -- helpers --------------------------------------------------------------

    def drop_own_schema(self, name: str) -> None:
        # Cleanup may drop exactly one schema: our own, just created, verified
        # by regex, database, user and the exact name.  Never public.
        if not SCHEMA_RE.fullmatch(name) or not name.startswith(SCHEMA_PREFIX):
            raise AssertionError("refusing to drop unexpected schema: %r" % name)
        if name in ("public", "pg_catalog", "runtime_next"):
            raise AssertionError("refusing to drop a shared schema")
        conn = self.connect_factory()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user")
                identity = cur.fetchone()
                if identity != (LOCAL_DBNAME, LOCAL_USER):
                    raise AssertionError("refusing to drop outside the local test database")
                cur.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )
            conn.commit()
        finally:
            conn.close()

    def create_schema_with_table(self, name: str, ddl_column_block: str) -> None:
        """Helper for the incompatible-structure checks (always own schema)."""
        conn = self.connect_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(name))
                )
                cur.execute(
                    sql.SQL("CREATE TABLE {}.inbox_events ({});").format(
                        sql.Identifier(name), sql.SQL(ddl_column_block)
                    )
                )
            conn.commit()
        finally:
            conn.close()

    def create_compatible_table(
        self,
        name: str,
        *,
        stored_at: str,
        pk: str = "PRIMARY KEY (inbox_id)",
        unique_tail: str = "",
        checks: tuple = CHECK_CLAUSES,
    ) -> None:
        """Build a table that matches the expected structure except for one part.

        Everything else about it is exactly what the task's DDL produces
        (columns, types, NOT NULL, the primary key, both scoped UNIQUEs, the seven
        CHECK constraints), so with ``checks`` left at its default the check under
        test is the only one that can refuse it — otherwise a neighbouring check
        would decide the outcome and the test would prove nothing about the check it
        names.  Pass ``checks=`` to make the CHECK set itself the deciding part (that
        is what the round-9 test does).  One measured exception lives in
        ``test_n5``: the server rewrites a CHECK written against a ``varchar`` column
        (it adds an explicit ``::text`` cast), so such a table is refused by the type
        comparison *and* by the CHECK comparison; the type and NOT NULL comparisons
        therefore get tables of their own there.
        """
        columns = [
            "inbox_id uuid NOT NULL",
            "client_id text NOT NULL",
            "mailbox_ref text NOT NULL",
            "provider text NOT NULL",
            "message_id text NOT NULL",
            "event_key text NOT NULL",
            "payload_sha256 text NOT NULL",
            "normalized_event jsonb NOT NULL",
            stored_at,
            pk,
            "UNIQUE (client_id, mailbox_ref, provider, message_id)" + unique_tail,
            "UNIQUE (client_id, mailbox_ref, event_key)" + unique_tail,
            *checks,
        ]
        self.create_schema_with_table(name, ", ".join(part for part in columns if part))

    def stored_at_default(self, schema: str):
        """Catalog text of the ``stored_at`` DEFAULT, or ``None`` when it has none."""
        rows = self.fetch(
            "SELECT pg_get_expr(d.adbin, d.adrelid) AS default_expr FROM pg_attrdef d"
            " JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum"
            " WHERE d.adrelid = format('%%I.%%I', %s::text, %s::text)::regclass"
            " AND a.attname = 'stored_at'",
            (schema, "inbox_events"),
        )
        self.assertLessEqual(len(rows), 1, "at most one default per column")
        return rows[0]["default_expr"] if rows else None

    def constraint_deferrability(self, schema: str) -> dict:
        """``{constraint name: condeferrable}`` for the PK and UNIQUE constraints."""
        rows = self.fetch(
            "SELECT conname, condeferrable FROM pg_constraint"
            " WHERE conrelid = format('%%I.%%I', %s::text, %s::text)::regclass"
            " AND contype IN ('p', 'u')",
            (schema, "inbox_events"),
        )
        return {row["conname"]: row["condeferrable"] for row in rows}

    def check_constraints(self, schema: str) -> list:
        """Sorted catalogue text of the CHECK constraints of ``inbox_events``."""
        rows = self.fetch(
            "SELECT pg_get_constraintdef(c.oid) AS constraint_def FROM pg_constraint c"
            " WHERE c.conrelid = format('%%I.%%I', %s::text, %s::text)::regclass"
            " AND c.contype = 'c'",
            (schema, "inbox_events"),
        )
        return sorted(" ".join(row["constraint_def"].split()) for row in rows)

    def assert_migration_refused(self, schema: str) -> None:
        """The migration must refuse with INVALID_SCHEMA, roll back and never commit.

        The refusal happens after the two ``CREATE ... IF NOT EXISTS`` statements,
        so the observable proof is the transaction itself: exactly one rollback,
        no commit, connection closed.  Whatever the table looked like before must
        still hold afterwards — this package never runs an automatic ALTER.
        """
        spies = []

        def factory():
            spy = TransactionSpy(self.connect_factory())
            spies.append(spy)
            return spy

        self.assertCode(
            "INVALID_SCHEMA", apply_migration, connect_factory=factory, schema=schema
        )
        self.assertEqual(len(spies), 1)
        self.assertEqual(spies[0].events, ["rollback", "close"])

    def schema_exists(self, name: str) -> bool:
        """Is the namespace visible from a NEW connection (this process's own? no)."""
        rows = self.fetch("SELECT 1 FROM pg_namespace WHERE nspname = %s", (name,))
        return bool(rows)

    def role_is_superuser(self) -> bool:
        rows = self.fetch("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        return bool(rows[0]["rolsuper"])

    # -- the DDL interposition used by the one-transaction check ----------------
    #
    # A failure *inside* the CREATE TABLE statement can only be produced while the
    # target schema is brand new - anything pre-existing in that schema would mean
    # the schema was not created by this migration.  A database-level DDL event
    # trigger is therefore the way to make the real server refuse the table: the
    # trigger raises only for the one schema named in a session setting, so every
    # other CREATE TABLE (the rest of this suite included) is untouched.  It needs
    # superuser, which the task's own disposable local container provides: its
    # POSTGRES_USER is created as the cluster superuser.

    BLOCKING_TRIGGER_NAME = "f107_test_block_create_table"
    BLOCKING_GUC = "f1_07_test.block_create_table"
    BLOCKING_FUNCTION_DDL = (
        "CREATE FUNCTION {}.block_create_table() RETURNS event_trigger"
        " LANGUAGE plpgsql AS $f107$"
        " DECLARE command record;"
        " BEGIN"
        "   FOR command IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP"
        "     IF command.command_tag = 'CREATE TABLE'"
        "        AND command.schema_name = current_setting('%s', true) THEN"
        "       RAISE EXCEPTION 'f1_07 test: CREATE TABLE is blocked in schema %%',"
        "         command.schema_name;"
        "     END IF;"
        "   END LOOP;"
        " END $f107$" % BLOCKING_GUC
    )

    def create_blocking_event_trigger(self, probe_schema: str) -> None:
        conn = self.connect_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(probe_schema))
                )
                cur.execute(sql.SQL(self.BLOCKING_FUNCTION_DDL).format(
                    sql.Identifier(probe_schema)))
                cur.execute(
                    sql.SQL(
                        "CREATE EVENT TRIGGER %s ON ddl_command_end"
                        " WHEN TAG IN ('CREATE TABLE') EXECUTE FUNCTION {}.block_create_table()"
                        % self.BLOCKING_TRIGGER_NAME
                    ).format(sql.Identifier(probe_schema))
                )
            conn.commit()
        finally:
            conn.close()

    def drop_blocking_event_trigger(self) -> None:
        conn = self.connect_factory()
        try:
            with conn.cursor() as cur:
                cur.execute("DROP EVENT TRIGGER IF EXISTS %s" % self.BLOCKING_TRIGGER_NAME)
            conn.commit()
        finally:
            conn.close()

    def factory_with_block(self, schema_name: str, handed: list, *, autocommit: bool = True):
        """Factory handing over a connection whose trigger guard names exactly
        ``schema_name`` (a different name leaves the trigger inert)."""

        def factory():
            conn = self.connect_factory()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config(%s, %s, false)", (self.BLOCKING_GUC, schema_name)
                )
            conn.commit()
            conn.autocommit = autocommit
            handed.append(conn)
            return conn

        return factory

    def fetch(self, query, params=()) -> list:
        conn = self.connect_factory()
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
            conn.rollback()
        finally:
            conn.close()
        return rows

    def raw_count(self, schema: str = None) -> int:
        name = schema or self.schema
        rows = self.fetch(
            sql.SQL("SELECT count(*) AS total FROM {}.inbox_events").format(
                sql.Identifier(name)
            )
        )
        return rows[0]["total"]

    def raw_row(self, event_key: str, schema: str = None) -> dict:
        name = schema or self.schema
        rows = self.fetch(
            sql.SQL(
                "SELECT inbox_id, payload_sha256, stored_at, normalized_event, message_id"
                " FROM {}.inbox_events WHERE event_key = %s"
            ).format(sql.Identifier(name)),
            (event_key,),
        )
        self.assertEqual(len(rows), 1, "expected exactly one row for the key")
        return rows[0]

    def column_names(self, schema: str) -> list:
        rows = self.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = %s AND table_name = 'inbox_events'"
            " ORDER BY column_name",
            (schema,),
        )
        return [row["column_name"] for row in rows]

    def assertCode(self, expected_code: str, callable_, *args, **kwargs):
        with self.assertRaises(InboxStoreError) as ctx:
            callable_(*args, **kwargs)
        self.assertEqual(ctx.exception.code, expected_code)
        self.assertEqual(str(ctx.exception), expected_code)
        return ctx.exception


# --- A -----------------------------------------------------------------------


class TestAFirstAppend(PostgresTestCase):
    def test_a_first_append_is_appended_and_get_returns_the_event(self):
        # The frozen fixture must equal the independently built expectation.
        self.assertEqual(make_event(), fixture_event())

        event = fixture_event()
        binding = fixture_binding()
        result = self.store.append(event, binding=binding)

        self.assertEqual(result["status"], "APPENDED")
        # The receipt is a closed shape: no extra keys, none missing.
        self.assertEqual(
            set(result),
            {"status", "inbox_id", "client_id", "mailbox_ref", "event_key", "stored_at"},
        )
        self.assertTrue(UUID_RE.fullmatch(result["inbox_id"]), result["inbox_id"])
        self.assertEqual(result["event_key"], event["event_key"])
        self.assertEqual(result["client_id"], "client-test")
        self.assertEqual(result["mailbox_ref"], "mb-test")
        self.assertEqual(result["stored_at"], expected_iso_utc(result["stored_at"]))
        self.assertTrue(result["stored_at"].endswith("+00:00"))

        self.assertEqual(self.raw_count(), 1)
        row = self.raw_row(event["event_key"])
        self.assertEqual(str(row["inbox_id"]), result["inbox_id"])
        self.assertEqual(row["payload_sha256"], expected_payload_sha256(event))
        self.assertEqual(row["normalized_event"], expected_canonical(event))

        got = self.store.get(
            client_id="client-test", mailbox_ref="mb-test", event_key=event["event_key"]
        )
        self.assertIsNotNone(got)
        self.assertEqual(got["inbox_id"], result["inbox_id"])
        self.assertEqual(got["stored_at"], result["stored_at"])
        self.assertEqual(got["event"], expected_canonical(event))


# --- B -----------------------------------------------------------------------


class TestBReplay(PostgresTestCase):
    def test_b_exact_replay_is_duplicate_with_same_id_and_stored_at(self):
        event = fixture_event()
        binding = fixture_binding()
        first = self.store.append(event, binding=binding)
        self.assertEqual(first["status"], "APPENDED")

        replay = self.store.append(copy.deepcopy(event), binding=copy.deepcopy(binding))
        self.assertEqual(replay["status"], "DUPLICATE")
        self.assertEqual(replay["inbox_id"], first["inbox_id"])
        self.assertEqual(replay["stored_at"], first["stored_at"])
        self.assertEqual(self.raw_count(), 1)

        third = self.store.append(event, binding=binding)
        self.assertEqual(third["status"], "DUPLICATE")
        self.assertEqual(third["inbox_id"], first["inbox_id"])
        self.assertEqual(self.raw_count(), 1)


# --- C -----------------------------------------------------------------------


class TestCCanonicalOrdering(PostgresTestCase):
    def test_c_reordered_json_keys_are_a_duplicate(self):
        event = fixture_event()
        binding = fixture_binding()
        self.assertEqual(self.store.append(event, binding=binding)["status"], "APPENDED")

        reordered = {key: event[key] for key in reversed(list(event))}
        self.assertEqual(
            list(reordered)[:2], ["event_key", "in_reply_to"], "keys were not reordered"
        )
        self.assertEqual(
            self.store.append(reordered, binding=binding)["status"], "DUPLICATE"
        )
        self.assertEqual(self.raw_count(), 1)

    def test_c_missing_headers_equal_empty_headers_and_header_order_is_irrelevant(self):
        binding = fixture_binding()

        without = make_event(message_id="Msg-C1")
        self.assertNotIn("auto_reply_headers", without)
        self.assertEqual(self.store.append(without, binding=binding)["status"], "APPENDED")
        with_empty = make_event(message_id="Msg-C1", headers={})
        self.assertEqual(
            self.store.append(with_empty, binding=binding)["status"], "DUPLICATE"
        )

        headers_a = make_event(message_id="Msg-C2", headers={"X-A": "1", "X-B": "2"})
        self.assertEqual(self.store.append(headers_a, binding=binding)["status"], "APPENDED")
        headers_b = make_event(message_id="Msg-C2", headers={"X-B": "2", "X-A": "1"})
        self.assertEqual(
            self.store.append(headers_b, binding=binding)["status"], "DUPLICATE"
        )
        self.assertEqual(self.raw_count(), 2)


# --- D -----------------------------------------------------------------------


class TestDConflicts(PostgresTestCase):
    """Same key, one changed semantic field at a time: conflict, row untouched."""

    def setUp(self):
        super().setUp()
        self.event = fixture_event()
        self.binding = fixture_binding()
        self.assert_status("APPENDED", self.store.append(self.event, binding=self.binding))
        self.before = self.raw_row(self.event["event_key"])

    def assert_status(self, expected, result):
        self.assertEqual(result["status"], expected, result)

    def assert_conflict(self, mutated, *, binding=None):
        event = copy.deepcopy(self.event)
        event.update(mutated)
        self.assertCode(
            "INBOX_EVENT_CONFLICT",
            self.store.append,
            event,
            binding=binding or self.binding,
        )
        self.assertEqual(self.raw_count(), 1)
        after = self.raw_row(self.event["event_key"])
        self.assertEqual(after["inbox_id"], self.before["inbox_id"])
        self.assertEqual(after["payload_sha256"], self.before["payload_sha256"])
        self.assertEqual(after["stored_at"], self.before["stored_at"])
        self.assertEqual(after["normalized_event"], self.before["normalized_event"])
        self.assertEqual(after["normalized_event"], expected_canonical(self.event))

    def test_d1_other_body_is_a_conflict(self):
        self.assert_conflict({"body_text": "Please explain pricing"})

    def test_d2_other_thread_is_a_conflict(self):
        self.assert_conflict({"thread_id": "Thread-B"})

    def test_d3_other_campaign_is_a_conflict_and_not_a_rebinding(self):
        event = copy.deepcopy(self.event)
        event["runtime_campaign_id"] = OTHER_CAMPAIGN
        self.assertCode(
            "INBOX_EVENT_CONFLICT",
            self.store.append,
            event,
            binding=make_binding(campaign=OTHER_CAMPAIGN),
        )
        self.assertEqual(self.raw_count(), 1)
        after = self.raw_row(self.event["event_key"])
        self.assertEqual(after["normalized_event"], expected_canonical(self.event))
        self.assertEqual(after["stored_at"], self.before["stored_at"])

    def test_d4_other_event_kind_is_a_conflict(self):
        self.assert_conflict({"event_kind": "DELIVERY_FAILURE"})

    def test_d5_other_header_values_are_a_conflict(self):
        self.assert_conflict({"auto_reply_headers": {"X-Custom": "1"}})

    def test_d6_other_in_reply_to_is_a_conflict(self):
        self.assert_conflict({"in_reply_to": "Msg-Z"})

    def test_d7_other_sender_email_is_a_conflict(self):
        self.assert_conflict({"sender_email": "buyer2@example.test"})

    def test_d8_other_received_at_is_a_conflict(self):
        self.assert_conflict({"received_at": "2026-10-11T12:00:01+00:00"})


# --- E -----------------------------------------------------------------------


class TestENewMessage(PostgresTestCase):
    def test_e_other_message_id_with_a_new_key_creates_a_second_row(self):
        binding = fixture_binding()
        first = self.store.append(make_event(message_id="Msg-A"), binding=binding)
        second = self.store.append(
            make_event(message_id="Msg-B", body_text="Please explain"), binding=binding
        )
        self.assertEqual(first["status"], "APPENDED")
        self.assertEqual(second["status"], "APPENDED")
        self.assertNotEqual(first["inbox_id"], second["inbox_id"])
        self.assertEqual(self.raw_count(), 2)
        first_row = self.raw_row(make_event("Msg-A")["event_key"])
        second_row = self.raw_row(make_event("Msg-B")["event_key"])
        self.assertEqual(first_row["normalized_event"]["body_text"], "Please explain")
        self.assertEqual(second_row["normalized_event"]["body_text"], "Please explain")
        self.assertEqual(first_row["normalized_event"]["message_id"], "Msg-A")
        self.assertEqual(second_row["normalized_event"]["message_id"], "Msg-B")
        # the body is identical but the record is not: the payload hash covers
        # the message_id as well and the key differs
        self.assertNotEqual(first_row["payload_sha256"], second_row["payload_sha256"])
        self.assertEqual(
            first_row["payload_sha256"], expected_payload_sha256(make_event("Msg-A"))
        )
        self.assertEqual(
            second_row["payload_sha256"], expected_payload_sha256(make_event("Msg-B"))
        )


# --- F -----------------------------------------------------------------------


class TestFScopeIsolation(PostgresTestCase):
    def test_f_other_mailbox_and_other_client_are_independent_rows(self):
        base = self.store.append(make_event(message_id="Msg-A"), binding=fixture_binding())
        other_mailbox = self.store.append(
            make_event(message_id="Msg-A", mailbox_ref="mb-other"),
            binding=make_binding(mailbox_ref="mb-other"),
        )
        other_client = self.store.append(
            make_event(message_id="Msg-A", client_id="client-other"),
            binding=make_binding(client_id="client-other"),
        )
        self.assertEqual(
            [base["status"], other_mailbox["status"], other_client["status"]],
            ["APPENDED", "APPENDED", "APPENDED"],
        )
        self.assertEqual(self.raw_count(), 3)
        # The event key is mailbox scoped only, so the third row shares the key
        # of the first one: the scoped UNIQUE constraint keeps them separate.
        self.assertEqual(other_client["event_key"], base["event_key"])
        self.assertNotEqual(other_client["inbox_id"], base["inbox_id"])

    def test_f_get_in_a_foreign_scope_returns_none(self):
        event = make_event(message_id="Msg-A")
        self.store.append(event, binding=fixture_binding())
        self.assertIsNotNone(
            self.store.get(
                client_id="client-test", mailbox_ref="mb-test", event_key=event["event_key"]
            )
        )
        self.assertIsNone(
            self.store.get(
                client_id="client-other", mailbox_ref="mb-test", event_key=event["event_key"]
            )
        )
        self.assertIsNone(
            self.store.get(
                client_id="client-test", mailbox_ref="mb-other", event_key=event["event_key"]
            )
        )
        self.assertIsNone(
            self.store.get(
                client_id="client-test", mailbox_ref="mb-test", event_key="0" * 64
            )
        )


# --- G -----------------------------------------------------------------------


class TestGIdentifierCase(PostgresTestCase):
    def test_g_message_id_case_is_preserved_and_gives_distinct_keys(self):
        upper = make_event(message_id="Msg-A")
        lower = make_event(message_id="msg-a")
        self.assertNotEqual(upper["event_key"], lower["event_key"])
        binding = fixture_binding()
        first = self.store.append(upper, binding=binding)
        second = self.store.append(lower, binding=binding)
        self.assertEqual([first["status"], second["status"]], ["APPENDED", "APPENDED"])
        self.assertEqual(self.raw_count(), 2)
        self.assertEqual(
            self.raw_row(upper["event_key"])["normalized_event"]["message_id"], "Msg-A"
        )
        self.assertEqual(
            self.raw_row(lower["event_key"])["normalized_event"]["message_id"], "msg-a"
        )


# --- H -----------------------------------------------------------------------


class TestHRejections(PostgresTestCase):
    """Every rejection leaves the table unchanged and the schema valid."""

    def assert_rejected(self, code, event, *, binding=_MISSING):
        self.assert_code_only(code, event, binding=binding)
        self.assertEqual(self.raw_count(), 0)

    def assert_code_only(self, code, event, *, binding=_MISSING):
        bound = fixture_binding() if binding is _MISSING else binding
        self.assertCode(code, self.store.append, event, binding=bound)

    def test_h1_wrong_event_key_value(self):
        event = make_event()
        event["event_key"] = expected_event_key("other-provider", "mb-test", "Msg-A")
        self.assert_rejected("INVALID_EVENT_KEY", event)

    def test_h2_event_key_shape(self):
        for bad in ("A" * 64, "0" * 63, "0" * 65, "g" * 64, 0, None, True):
            with self.subTest(bad=repr(bad)):
                event = make_event()
                event["event_key"] = bad
                self.assert_rejected("INVALID_EVENT_KEY", event)

    def test_h3_scope_mismatch(self):
        # client_id
        event = make_event(client_id="client-other")
        self.assert_rejected("SCOPE_MISMATCH", event)
        # campaign
        event = make_event(campaign=OTHER_CAMPAIGN)
        self.assert_rejected("SCOPE_MISMATCH", event)
        # mailbox (the key depends on the mailbox, so it is recomputed)
        event = make_event(mailbox_ref="mb-other")
        self.assert_rejected("SCOPE_MISMATCH", event)
        # recipient is not our bound mailbox
        event = make_event(recipient_email="someone@example.test")
        self.assert_rejected("SCOPE_MISMATCH", event)
        # a matching binding makes the campaign change a conflict, not a scope error
        event = make_event(campaign=OTHER_CAMPAIGN)
        self.assertCode(
            "SCOPE_MISMATCH", self.store.append, event, binding=fixture_binding()
        )
        self.assertEqual(self.raw_count(), 0)

    def test_h4_extra_fields_are_rejected(self):
        for extra, value in (
            ("lead_id", "lead-1"),
            ("auth", True),
            ("tenant_id", "client-test"),
            ("classifier_result", {"kind": "QUESTION"}),
            ("event", {"raw": "envelope"}),
        ):
            with self.subTest(extra=extra):
                event = make_event()
                event[extra] = value
                self.assert_rejected("INVALID_EVENT", event)

    def test_h5_missing_required_field_is_rejected(self):
        for missing in ("schema_version", "client_id", "in_reply_to", "event_key"):
            with self.subTest(missing=missing):
                event = make_event()
                event.pop(missing)
                self.assert_rejected("INVALID_EVENT", event)

    def test_h6_invalid_binding(self):
        cases = (
            make_binding(campaign="not-a-uuid"),
            make_binding(campaign="AAAAAAAA-1111-4111-8111-AAAAAAAAAAAA"),
            make_binding(client_id=""),
            make_binding(client_id=" client-test"),
            make_binding(sender_email="seller@example.test "),
            {"schema_version": "WRONG", "client_id": "client-test",
             "runtime_campaign_id": DEFAULT_CAMPAIGN, "mailbox_ref": "mb-test",
             "sender_email": "seller@example.test"},
            {"client_id": "client-test"},
            "not-a-dict",
            None,
        )
        for index, binding in enumerate(cases):
            with self.subTest(binding=index):
                self.assert_rejected("INVALID_BINDING", make_event(), binding=binding)

    def test_h7_naive_and_non_utc_timestamps_are_rejected(self):
        for received_at in (
            "2026-10-11T12:00:00",
            "2026-10-11T15:00:00+03:00",
            "2026-10-11T12:00:00-00:30",
            "2026-10-11",
            "not-a-timestamp",
            "",
            1234,
            None,
        ):
            with self.subTest(received_at=repr(received_at)):
                self.assert_rejected("INVALID_EVENT", make_event(received_at=received_at))

    def test_h8_bool_and_non_string_identifiers_are_rejected(self):
        for key in (
            "client_id",
            "runtime_campaign_id",
            "mailbox_ref",
            "provider",
            "message_id",
            "thread_id",
            "sender_email",
            "recipient_email",
            "body_text",
        ):
            with self.subTest(field=key):
                event = make_event()
                event[key] = True if key != "body_text" else False
                self.assert_rejected("INVALID_EVENT", event)
        for value in (7, 1.5, [], {"a": "b"}, None):
            with self.subTest(value=repr(value)):
                event = make_event()
                event["message_id"] = value
                self.assert_rejected("INVALID_EVENT", event)

    def test_h9_non_object_input_is_rejected(self):
        for value in ([], "event", 42, None, True, (1, 2)):
            with self.subTest(value=repr(value)):
                self.assertCode(
                    "INVALID_EVENT",
                    self.store.append,
                    value,
                    binding=fixture_binding(),
                )
                self.assertEqual(self.raw_count(), 0)

    def test_h10_body_bounds_are_counted_in_utf8_bytes(self):
        # exactly 65536 bytes is accepted, one byte more is rejected
        body_max = "a" * 65536
        self.assertEqual(len(body_max.encode("utf-8")), 65536)
        accepted = self.store.append(
            make_event(message_id="Msg-Max", body_text=body_max), binding=fixture_binding()
        )
        self.assertEqual(accepted["status"], "APPENDED")
        self.assertEqual(
            self.raw_row(make_event(message_id="Msg-Max")["event_key"])["normalized_event"][
                "body_text"
            ],
            body_max,
        )

        too_big = "a" * 65537
        self.assert_code_only("INVALID_EVENT", make_event(body_text=too_big))

        # unicode: characters are not bytes
        unicode_max = "\u0451" * 32768
        self.assertEqual(len(unicode_max.encode("utf-8")), 65536)
        self.assertEqual(len(unicode_max), 32768)
        result = self.store.append(
            make_event(message_id="Msg-Uni", body_text=unicode_max), binding=fixture_binding()
        )
        self.assertEqual(result["status"], "APPENDED")
        self.assertEqual(
            self.raw_row(make_event(message_id="Msg-Uni")["event_key"])["normalized_event"][
                "body_text"
            ],
            unicode_max,
        )
        unicode_too_big = "\u0451" * 32769
        self.assertEqual(len(unicode_too_big.encode("utf-8")), 65538)
        self.assert_code_only("INVALID_EVENT", make_event(body_text=unicode_too_big))
        self.assertEqual(self.raw_count(), 2)

    def test_h11_non_ascii_bodies_are_stored_verbatim(self):
        binding = fixture_binding()
        for index, body in enumerate(
            ("Пожалуйста, объясните", "Please explain", "\u4f60\u597d\uff0c\u8bf7\u8bf4\u660e")
        ):
            event = make_event(message_id="Msg-U%d" % index, body_text=body)
            self.assertEqual(self.store.append(event, binding=binding)["status"], "APPENDED")
            stored = self.raw_row(event["event_key"])["normalized_event"]["body_text"]
            self.assertEqual(stored, body)
        self.assertEqual(self.raw_count(), 3)

    def test_h12_whitespace_only_body_and_identifiers_are_rejected(self):
        for body in ("", "   ", "\t\n "):
            with self.subTest(body=repr(body)):
                self.assert_rejected("INVALID_EVENT", make_event(body_text=body))
        for key in ("provider", "message_id", "thread_id"):
            for value in ("", " x", "x ", "\t"):
                with self.subTest(key=key, value=repr(value)):
                    event = make_event()
                    event[key] = value
                    self.assert_rejected("INVALID_EVENT", event)

    def test_h13_identifier_byte_bounds(self):
        ok = "x" * 512
        event = make_event(provider=ok)
        self.assertEqual(len(ok.encode("utf-8")), 512)
        self.assertEqual(
            self.store.append(event, binding=fixture_binding())["status"], "APPENDED"
        )
        too_long = "x" * 513
        event = make_event(provider=too_long)
        self.assert_code_only("INVALID_EVENT", event)
        # client_id / mailbox_ref carry the same 512 byte bound as the other
        # new DB fields, so the over-long value is an input error, not a scope
        # mismatch (the bound is checked before the binding comparison).
        event = make_event(client_id="x" * 513)
        self.assert_code_only("INVALID_EVENT", event)
        event = make_event(mailbox_ref="x" * 513)
        self.assert_code_only("INVALID_EVENT", event)
        event = make_event(thread_id="x" * 513)
        self.assert_code_only("INVALID_EVENT", event)
        event = make_event(in_reply_to="x" * 513)
        self.assert_code_only("INVALID_EVENT", event)
        event = make_event(sender_email="x" * 321)
        self.assert_code_only("INVALID_EVENT", event)
        event = make_event(recipient_email="x" * 321)
        self.assert_code_only("INVALID_EVENT", event)

        # The ACCEPTING side of the email bound is boundary-tested too: exactly
        # 320 bytes must be stored, otherwise a stricter limit (320 -> 319) would
        # pass unnoticed while only the over-long case is exercised.
        longest_email = "x" * 320
        self.assertEqual(len(longest_email.encode("utf-8")), 320)
        binding = make_binding(sender_email=longest_email)
        accepted = make_event(
            message_id="Msg-Email320",
            sender_email=longest_email,
            recipient_email=longest_email,
        )
        self.assertEqual(
            self.store.append(accepted, binding=binding)["status"], "APPENDED"
        )
        self.assertEqual(
            self.raw_row(accepted["event_key"])["normalized_event"]["sender_email"],
            longest_email,
        )
        self.assertEqual(self.raw_count(), 2)

    def test_h14_headers_contract(self):
        cases = (
            ("too_many", {("k%d" % i): "v" for i in range(65)}),
            ("not_a_dict", ["x"]),
            ("bool_value", {"X-A": True}),
            ("int_value", {"X-A": 1}),
            ("none_value", {"X-A": None}),
            ("big_total", {"X-A": "y" * 16385}),
            ("nested", {"X-A": {"b": "c"}}),
        )
        for name, headers in cases:
            with self.subTest(case=name):
                self.assert_rejected("INVALID_EVENT", make_event(headers=headers))
        allowed = {"X-A": "x" * 8189, "X-B": "y" * 8189}
        self.assertEqual(sum(len(k) + len(v) for k, v in allowed.items()), 16384)
        self.assertEqual(
            self.store.append(make_event(headers=allowed), binding=fixture_binding())["status"],
            "APPENDED",
        )
        self.assertEqual(self.raw_count(), 1)
        self.assertEqual(
            self.raw_row(make_event()["event_key"])["normalized_event"]["auto_reply_headers"],
            allowed,
        )
        over = {"X-A": "x" * 8189, "X-B": "y" * 8190}
        self.assertEqual(sum(len(k) + len(v) for k, v in over.items()), 16385)
        self.assert_code_only("INVALID_EVENT", make_event(headers=over))

        # Exactly the allowed number of pairs is accepted as well: only the
        # rejecting side (65 pairs) used to be exercised, so lowering the limit
        # to 63 would have gone unnoticed.
        exactly_64 = {"k%d" % i: "v" for i in range(64)}
        self.assertEqual(len(exactly_64), 64)  # the limit stated by the task
        self.assertLess(
            sum(len(k) + len(v) for k, v in exactly_64.items()), 16384
        )
        accepted = make_event(message_id="Msg-Pairs64", headers=exactly_64)
        self.assertEqual(
            self.store.append(accepted, binding=fixture_binding())["status"], "APPENDED"
        )
        self.assertEqual(
            self.raw_row(accepted["event_key"])["normalized_event"]["auto_reply_headers"],
            exactly_64,
        )
        self.assertEqual(self.raw_count(), 2)

    def test_h15_caller_dicts_are_not_mutated(self):
        event = make_event(headers={"X-A": "1"})
        binding = fixture_binding()
        event_before = json.dumps(event, sort_keys=True)
        binding_before = json.dumps(binding, sort_keys=True)
        headers_before = dict(event["auto_reply_headers"])
        self.store.append(event, binding=binding)
        self.assertEqual(json.dumps(event, sort_keys=True), event_before)
        self.assertEqual(json.dumps(binding, sort_keys=True), binding_before)
        self.assertEqual(event["auto_reply_headers"], headers_before)

    def test_h16_get_bad_arguments_never_open_a_connection(self):
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)
        self.assertEqual(self.raw_count(), 0)
        self.assertCode("INVALID_EVENT_KEY", store.get, client_id="client-test",
                        mailbox_ref="mb-test", event_key="0" * 63)
        self.assertCode("INVALID_EVENT_KEY", store.get, client_id="client-test",
                        mailbox_ref="mb-test", event_key="A" * 64)
        self.assertCode("INVALID_EVENT_KEY", store.get, client_id="client-test",
                        mailbox_ref="mb-test", event_key=None)
        self.assertCode("INVALID_EVENT", store.get, client_id="", mailbox_ref="mb-test",
                        event_key="0" * 64)
        self.assertCode("INVALID_EVENT", store.get, client_id=True, mailbox_ref="mb-test",
                        event_key="0" * 64)
        self.assertCode("INVALID_EVENT", store.get, client_id="x" * 513,
                        mailbox_ref="mb-test", event_key="0" * 64)
        self.assertCode("INVALID_EVENT", store.get, client_id="client-test",
                        mailbox_ref=" mb-test", event_key="0" * 64)
        self.assertEqual(recording.calls, 0)
        self.assertIsNone(
            store.get(client_id="client-test", mailbox_ref="mb-test", event_key="0" * 64)
        )
        self.assertEqual(recording.calls, 1)

    def test_h17_unsupported_event_kind_is_rejected(self):
        # The kind is a closed enum: anything else (including a different case,
        # a number or a missing-looking value) must be refused, not stored.
        for kind in ("OTHER_KIND", "inbound_message", "INBOUND", "", " ", 42, None, False, ["INBOUND_MESSAGE"]):
            with self.subTest(kind=repr(kind)):
                self.assert_rejected("INVALID_EVENT", make_event(event_kind=kind))
        for kind in ("INBOUND_MESSAGE", "DELIVERY_FAILURE"):
            with self.subTest(accepted=kind):
                event = make_event(message_id="Msg-Kind-%s" % kind, event_kind=kind)
                self.assertEqual(
                    self.store.append(event, binding=fixture_binding())["status"], "APPENDED"
                )
                self.assertEqual(
                    self.raw_row(event["event_key"])["normalized_event"]["event_kind"], kind
                )
        self.assertEqual(self.raw_count(), 2)


    def test_h18_event_schema_version_must_be_exactly_the_inbound_version(self):
        # The version tag is part of the contract: a correct-looking tag with a
        # different value is not "close enough" and must be refused.
        for version in (
            "FIRST_INBOUND_V2",
            "first_inbound_v1",
            "FIRST_INBOUND_V1 ",
            " FIRST_INBOUND_V1",
            "FIRST_INBOUND_V1\n",
            "FIRST_INBOX_BINDING_V1",
            "",
            42,
            True,
            None,
            ["FIRST_INBOUND_V1"],
        ):
            with self.subTest(event_schema_version=repr(version)):
                event = make_event()
                event["schema_version"] = version
                self.assert_rejected("INVALID_EVENT", event)

        # ...and the binding tag is checked the same way.
        for version in ("FIRST_INBOX_BINDING_V2", "first_inbox_binding_v1", "", 42, None):
            with self.subTest(binding_schema_version=repr(version)):
                binding = fixture_binding()
                binding["schema_version"] = version
                self.assert_rejected(
                    "INVALID_BINDING",
                    make_event(message_id="Msg-Ver-%r" % (version,)),
                    binding=binding,
                )

        # The exact tags still work (the checks above are not a blanket refusal).
        self.assertEqual(
            self.store.append(fixture_event(), binding=fixture_binding())["status"],
            "APPENDED",
        )
        self.assertEqual(self.raw_count(), 1)

    def test_h19_explicit_null_headers_are_rejected_while_absent_equals_empty(self):
        # Task section 5 makes "absent auto_reply_headers" and "{}" one and the
        # same content.  An explicitly present null is neither, and section 3
        # declares the field as a mapping of strings, so it is refused rather
        # than silently read as an empty dictionary.
        self.assert_rejected("INVALID_EVENT", make_event(headers=None))
        # ... while absent and {} really are the same content (section 5).
        with_empty = make_event(message_id="Msg-Null-A", headers={})
        without = make_event(message_id="Msg-Null-A")
        self.assertNotIn("auto_reply_headers", without)
        self.assertEqual(
            self.store.append(with_empty, binding=fixture_binding())["status"],
            "APPENDED",
        )
        self.assertEqual(
            self.store.append(without, binding=fixture_binding())["status"],
            "DUPLICATE",
        )
        self.assertEqual(self.raw_count(), 1)

    def test_h20_identifiers_are_kept_byte_for_byte_without_normalisation(self):
        # The task asks for no Unicode normalisation: an identifier is kept
        # exactly as supplied and the event_key is computed over those same
        # bytes, so two canonically equivalent forms are two different
        # identifiers - not one (the normaliser belongs to the upstream adapter).
        composed = "caf\u00e9"          # café, 5 bytes
        decomposed = "cafe\u0301"       # café, 6 bytes
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(len(composed.encode("utf-8")), 5)
        self.assertEqual(len(decomposed.encode("utf-8")), 6)

        first = make_event(message_id=composed)
        second = make_event(message_id=decomposed)
        self.assertEqual(
            first["event_key"], expected_event_key("fixture", "mb-test", composed)
        )
        self.assertNotEqual(first["event_key"], second["event_key"])
        for event in (first, second):
            with self.subTest(message_id=event["message_id"]):
                self.assertEqual(
                    self.store.append(event, binding=fixture_binding())["status"],
                    "APPENDED",
                )
        self.assertEqual(self.raw_count(), 2)
        for event in (first, second):
            stored = self.raw_row(event["event_key"])["normalized_event"]["message_id"]
            self.assertEqual(
                stored.encode("utf-8"), event["message_id"].encode("utf-8")
            )

        # The same rule holds for the scope identifiers: a decomposed client_id
        # does not match a composed one in the binding.
        decomposed_client = "clie\u0301nt-test"
        composed_client = "cli\u00e9nt-test"
        self.assertNotEqual(decomposed_client, composed_client)
        mismatched = make_event(
            message_id="Msg-Unicode-Scope", client_id=decomposed_client
        )
        self.assert_code_only(
            "SCOPE_MISMATCH",
            mismatched,
            binding=make_binding(client_id=composed_client),
        )
        # ... and nothing was written for it (the two real rows are untouched).
        self.assertEqual(self.raw_count(), 2)

    # -- values no UTF-8 encoder can represent (lone surrogates) --------------

    LONE_SURROGATE = "\ud800"

    def test_h21_lone_surrogates_in_event_fields_are_rejected_before_any_connection(self):
        # A Python string can hold a lone surrogate (U+D800), which
        # str.encode("utf-8") refuses.  Section 7 of the task returns validation
        # failures as InboxStoreError, so the module must answer with its own
        # INVALID_EVENT instead of letting UnicodeEncodeError escape - and it must
        # not "repair" the value either: no silent deletion, no replacement.
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)
        polluted = "x" + self.LONE_SURROGATE
        for field in (
            "client_id",
            "runtime_campaign_id",
            "mailbox_ref",
            "provider",
            "message_id",
            "thread_id",
            "sender_email",
            "recipient_email",
            "body_text",
            "in_reply_to",
        ):
            with self.subTest(field=field):
                event = make_event()
                event[field] = polluted
                self.assertCode(
                    "INVALID_EVENT", store.append, event, binding=fixture_binding()
                )
        self.assertEqual(recording.calls, 0, "the refusal precedes the connection")
        self.assertEqual(self.raw_count(), 0)

    def test_h22_lone_surrogates_in_headers_are_rejected_before_any_connection(self):
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)
        polluted = "x" + self.LONE_SURROGATE
        for name, headers in (
            ("value", {"X-A": polluted}),
            ("key", {polluted: "value"}),
            ("key and value", {polluted: polluted}),
            ("among valid headers", {"X-A": "ok", "X-B": polluted}),
            ("valid key, polluting value", {"X-A": "ok" + self.LONE_SURROGATE}),
        ):
            with self.subTest(case=name):
                self.assertCode(
                    "INVALID_EVENT",
                    store.append,
                    make_event(headers=headers),
                    binding=fixture_binding(),
                )
        self.assertEqual(recording.calls, 0, "the refusal precedes the connection")
        self.assertEqual(self.raw_count(), 0)

    def test_h23_lone_surrogates_in_binding_fields_are_invalid_binding(self):
        # The code depends on which input is wrong: a binding field is
        # INVALID_BINDING, an event field is INVALID_EVENT (task section 7 lists
        # both codes), and neither may reach the database.
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)
        polluted = "x" + self.LONE_SURROGATE
        for field in ("client_id", "mailbox_ref", "sender_email"):
            with self.subTest(field=field):
                self.assertCode(
                    "INVALID_BINDING",
                    store.append,
                    make_event(),
                    binding=make_binding(**{field: polluted}),
                )
        self.assertEqual(recording.calls, 0, "the refusal precedes the connection")
        self.assertEqual(self.raw_count(), 0)

    def test_h24_lone_surrogates_in_get_arguments_are_rejected_before_any_connection(self):
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)
        polluted = "x" + self.LONE_SURROGATE
        hex_key = "0" * 64
        for code, kwargs in (
            ("INVALID_EVENT",
             {"client_id": polluted, "mailbox_ref": "mb-test", "event_key": hex_key}),
            ("INVALID_EVENT",
             {"client_id": "client-test", "mailbox_ref": polluted, "event_key": hex_key}),
            # the shape of event_key has its own code, and a surrogate makes the
            # key something other than 64 lowercase hex characters
            ("INVALID_EVENT_KEY",
             {"client_id": "client-test", "mailbox_ref": "mb-test",
              "event_key": polluted}),
        ):
            with self.subTest(code=code, kwargs=sorted(kwargs)):
                self.assertCode(code, store.get, **kwargs)
        self.assertEqual(recording.calls, 0, "the refusal precedes the connection")
        self.assertIsNone(
            store.get(client_id="client-test", mailbox_ref="mb-test", event_key=hex_key)
        )
        self.assertEqual(recording.calls, 1)

    def test_h25_non_bmp_characters_are_accepted_and_stored_verbatim(self):
        # The refusal above is about code units that cannot be *encoded*, not
        # about non-ASCII text.  A real non-BMP character (one astral code point,
        # four UTF-8 bytes) is perfectly encodable and must still be stored byte
        # for byte - otherwise the guard would have been an over-broad
        # "reject non-ASCII" and the task's Unicode bodies would break.
        emoji = "\U0001F600"  # 😀
        self.assertEqual(len(emoji.encode("utf-8")), 4)
        body = "hello " + emoji + " \u0451"
        event = make_event(
            message_id="Msg-" + emoji, body_text=body, headers={"X-Emoji": emoji}
        )
        self.assertEqual(
            self.store.append(event, binding=fixture_binding())["status"], "APPENDED"
        )
        stored = self.raw_row(event["event_key"])["normalized_event"]
        self.assertEqual(stored["message_id"], "Msg-" + emoji)
        self.assertEqual(stored["body_text"], body)
        self.assertEqual(stored["auto_reply_headers"], {"X-Emoji": emoji})
        self.assertEqual(self.raw_count(), 1)



    def test_h26_values_postgresql_cannot_store_are_rejected_before_any_connection(self):
        # The second kind of value this component cannot hand to the database:
        # U+0000 is refused by PostgreSQL in text and in jsonb, and the driver
        # raises DataError / UntranslatableCharacter - a foreign exception after
        # the connection is already open, where section 7 wants a domain error.
        # The value is refused, not repaired: no stripping, no escaping, no
        # truncation at the byte that cannot be stored.
        #
        # Almost every case is chosen so that the storability guard is the only check
        # that can reject it.  A binding field is mirrored into the event (the
        # event key is then recomputed over the polluted values) because
        # otherwise the scope comparison or the key check refuses it first and
        # this test would pass on a build without the guard - exactly the
        # masking trap that hid a defect in the previous round.  Two of the fields
        # below are the exception and are kept as behavioural pins rather than as
        # proofs of the guard: event_kind and schema_version are refused by their own
        # value checks as well, so they stay INVALID_EVENT on a build without the
        # guard (measured by an independent review; the test as a whole is still
        # caught - 5 failures and 13 errors with the guard removed).
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)
        nul = "x\x00y"
        self.assertEqual(nul, "x" + chr(0x00) + "y", "the value must be a real NUL byte")
        fields = (
            "body_text", "thread_id", "sender_email", "recipient_email",
            "in_reply_to", "message_id", "mailbox_ref", "client_id",
            "runtime_campaign_id", "provider", "event_kind", "schema_version",
        )
        for field in fields:
            with self.subTest(where="event", field=field):
                if field in ("message_id", "provider", "mailbox_ref"):
                    # the key is derived from these, so it must be recomputed over
                    # the polluted value - otherwise the key check rejects first and
                    # the case would pass on a build without the guard
                    event = make_event(**{field: nul})
                else:
                    event = make_event()
                    event[field] = nul
                self.assertCode(
                    "INVALID_EVENT", store.append, event, binding=fixture_binding()
                )
        # event_key has its own code: either the storability guard or the shape check
        # fires first, and section 7 fixes INVALID_EVENT_KEY for this field - so the
        # domain code is pinned here, not the guard.
        key_event = make_event()
        key_event["event_key"] = nul
        self.assertCode(
            "INVALID_EVENT_KEY", store.append, key_event, binding=fixture_binding()
        )
        for name, headers in (("value", {"X-A": nul}), ("key", {nul: "value"})):
            with self.subTest(where="headers", case=name):
                self.assertCode(
                    "INVALID_EVENT",
                    store.append,
                    make_event(headers=headers),
                    binding=fixture_binding(),
                )
        mirror = {
            "client_id": "client_id",
            "mailbox_ref": "mailbox_ref",
            "sender_email": "recipient_email",
        }
        for field, twin in mirror.items():
            with self.subTest(where="binding", field=field):
                event = make_event(**{twin: nul})
                self.assertCode(
                    "INVALID_BINDING",
                    store.append,
                    event,
                    binding=make_binding(**{field: nul}),
                )
        for arg in ("client_id", "mailbox_ref"):
            with self.subTest(where="get", arg=arg):
                kwargs = {
                    "client_id": "client-test",
                    "mailbox_ref": "mb-test",
                    "event_key": "0" * 64,
                }
                kwargs[arg] = nul
                self.assertCode("INVALID_EVENT", store.get, **kwargs)
        self.assertEqual(recording.calls, 0, "the refusal precedes the connection")
        self.assertEqual(self.raw_count(), 0)


    def test_h27_received_at_with_a_trailing_nul_is_refused_and_not_repaired(self):
        # CPython's fromisoformat tolerates exactly one trailing U+0000 and returns
        # an ordinary datetime, so a timestamp carrying a byte the database cannot
        # store used to be accepted with that byte silently dropped: the stored value
        # no longer equalled what the caller sent.  Section 7 wants a rejected input,
        # not a repaired one.  Found by an independent review of the previous build -
        # the same defect class as the surrogate finding.  The refusal must precede
        # the connection and leave no row behind.
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)
        bad = "2026-10-11T12:00:00+00:00" + chr(0)
        self.assertEqual(bad[-1], chr(0), "the value must really end with U+0000")
        self.assertCode(
            "INVALID_EVENT", store.append, make_event(received_at=bad), binding=fixture_binding()
        )
        self.assertEqual(recording.calls, 0, "the refusal precedes the connection")
        self.assertEqual(self.raw_count(), 0)
        # positive control: the same instant without the byte is accepted, and the
        # canonical spelling is what gets stored (normalisation, not repair)
        plain = InboxStore(connect_factory=self.connect_factory, schema=self.schema)
        for index, spelling in enumerate(("2026-10-11T12:00:00+00:00", "2026-10-11T12:00:00Z")):
            message_id = "Msg-H27-%d" % index
            result = plain.append(
                make_event(message_id=message_id, received_at=spelling),
                binding=fixture_binding(),
            )
            self.assertEqual(result["status"], "APPENDED")
            stored = self.raw_row(expected_event_key("fixture", "mb-test", message_id))
            self.assertEqual(
                stored["normalized_event"]["received_at"], "2026-10-11T12:00:00+00:00"
            )

    def test_h28_an_autocommit_factory_still_gives_one_transaction(self):
        # Task section 4 needs the isolation statement, the INSERT and the following
        # SELECT inside ONE transaction.  A caller may well hand over a pool whose
        # connections are in autocommit mode; then each statement commits on its own
        # and commit() is a no-op.  The component takes the connection out of
        # autocommit itself, and the test checks the connection it was actually
        # given, not only the happy path (found by an independent review).
        handed = []

        def factory():
            conn = self.connect_factory()
            conn.autocommit = True
            handed.append(conn)
            return conn

        store = InboxStore(connect_factory=factory, schema=self.schema)
        event = make_event(message_id="Msg-H28")
        first = store.append(event, binding=fixture_binding())
        self.assertEqual(first["status"], "APPENDED")
        self.assertEqual(len(handed), 1)
        self.assertFalse(
            handed[0].autocommit,
            "the connection must not stay in autocommit: every statement of the append"
            " has to belong to the same transaction",
        )
        second = store.append(event, binding=fixture_binding())
        self.assertEqual(second["status"], "DUPLICATE")
        self.assertEqual(second["inbox_id"], first["inbox_id"])
        self.assertEqual(self.raw_count(), 1)


    def test_h29_body_text_with_surrounding_whitespace_is_stored_byte_for_byte(self):
        # Task section 3: the register of the ids and of body_text is preserved.  A
        # silent repair (strip, trim, collapsing whitespace) is the same class of defect
        # as the trailing-NUL blocker: what reaches the database stops matching what was
        # sent.  It is pinned as a positive control - the body comes back with exactly the
        # bytes that went in, and the payload hash is taken over those bytes - because an
        # independent review showed that a mutation trimming the body survived the suite.
        bodies = (
            "  Please explain  ",
            "\n\nPricing?\n\n",
            "\tTabbed\t",
            "\r\nCRLF\r\n",
            "  \u00a0 x \u00a0 ",
            "trailing\u3000  ",
            "\u200bzero-width\u200b",
        )
        for index, body in enumerate(bodies, start=1):
            with self.subTest(body=body):
                event = make_event(message_id="Msg-H29-%d" % index, body_text=body)
                self.assertEqual(
                    self.store.append(event, binding=fixture_binding())["status"],
                    "APPENDED",
                )
                row = self.raw_row(event["event_key"])
                stored = row["normalized_event"]["body_text"]
                self.assertEqual(stored, body)
                self.assertEqual(
                    stored.encode("utf-8"),
                    body.encode("utf-8"),
                    "the body must be stored byte for byte, without trimming",
                )
                self.assertEqual(row["payload_sha256"], expected_payload_sha256(event))
        self.assertEqual(self.raw_count(), len(bodies))


# --- I -----------------------------------------------------------------------


class TestISchemaAndSqlSafety(PostgresTestCase):
    def test_i1_bad_schema_is_rejected_before_the_factory_is_called(self):
        recording = RecordingFactory(self.connect_factory)
        bad_schemas = (
            "public",
            "pg_catalog",
            "runtime_next",
            "f1_07",
            "f1_07_",
            "f1_07_local;DROP TABLE x",
            'f1_07_local";DROP TABLE x;--',
            "f1_07_local'",
            "f1_07_Local",
            "f1_07_\u043b\u043e\u043a\u0430\u043b",
            "f1_07_local.other",
            "f1_07_" + "a" * 60,
            "public_f1_07_x",
            "",
            42,
            None,
            True,
            ["f1_07_local"],
        )
        for schema in bad_schemas:
            with self.subTest(schema=repr(schema)):
                self.assertCode(
                    "INVALID_SCHEMA", InboxStore, connect_factory=recording, schema=schema
                )
                self.assertCode(
                    "INVALID_SCHEMA",
                    apply_migration,
                    connect_factory=recording,
                    schema=schema,
                )
                self.assertEqual(recording.calls, 0)

    def test_i2_sql_like_body_is_stored_literally_and_never_executed(self):
        hostile = "'); DROP TABLE %s.inbox_events; -- \u0442\u0435\u043a\u0441\u0442" % self.schema
        event = make_event(message_id="Msg-SQL", body_text=hostile)
        result = self.store.append(event, binding=fixture_binding())
        self.assertEqual(result["status"], "APPENDED")
        row = self.raw_row(event["event_key"])
        self.assertEqual(row["normalized_event"]["body_text"], hostile)
        self.assertEqual(self.raw_count(), 1)
        hostile_id = "Msg-A'; DELETE FROM %s.inbox_events; --" % self.schema
        event = make_event(message_id=hostile_id)
        self.assertEqual(
            self.store.append(event, binding=fixture_binding())["status"], "APPENDED"
        )
        self.assertEqual(self.raw_count(), 2)
        self.assertEqual(
            self.raw_row(event["event_key"])["normalized_event"]["message_id"], hostile_id
        )

    def test_i3_migration_sql_file_uses_identifier_not_string_substitution(self):
        sql_text = (
            Path(__file__).resolve().parents[1] / "migrations" / "001_inbox_events.sql"
        ).read_text(encoding="utf-8")
        self.assertEqual(sql_text.count("{{schema}}"), 2)
        self.assertEqual(sql_text.count(";"), 2)
        self.assertNotIn("f1_07_local", sql_text)
        # a schema with a quote is refused, never concatenated into the DDL
        self.assertCode(
            "INVALID_SCHEMA",
            InboxStore,
            connect_factory=self.connect_factory,
            schema='f1_07_x" ; DROP SCHEMA public; --',
        )


    def test_i4_schema_length_boundary_is_63_bytes(self):
        # The name limit is a byte limit on the schema name: exactly 63 is fine,
        # 64 is refused before the connect factory is ever called.
        longest = SCHEMA_PREFIX + "a" * (63 - len(SCHEMA_PREFIX))
        self.assertEqual(len(longest.encode("utf-8")), 63)
        self.assertTrue(SCHEMA_RE.fullmatch(longest))
        self.addCleanup(self.drop_own_schema, longest)

        recording = RecordingFactory(self.connect_factory)
        apply_migration(connect_factory=recording, schema=longest)
        store = InboxStore(connect_factory=recording, schema=longest)
        event = make_event(message_id="Msg-Schema63")
        self.assertEqual(
            store.append(event, binding=fixture_binding())["status"], "APPENDED"
        )
        self.assertEqual(self.raw_count(longest), 1)
        self.assertGreater(recording.calls, 0)

        too_long = longest + "a"
        self.assertEqual(len(too_long.encode("utf-8")), 64)
        calls_before = recording.calls
        self.assertCode(
            "INVALID_SCHEMA", InboxStore, connect_factory=recording, schema=too_long
        )
        self.assertCode(
            "INVALID_SCHEMA",
            apply_migration,
            connect_factory=recording,
            schema=too_long,
        )
        self.assertEqual(recording.calls, calls_before)


    def test_i5_migration_sql_pins_the_exact_check_constraints(self):
        # Task section 6 asks for a precise DDL, so every rule the deliverable file
        # states has to be pinned: otherwise the file can be weakened in the very
        # direction the task forbids and the suite stays green (found by an
        # independent review - the catalogue check compares columns, the primary key,
        # both UNIQUEs and the stored_at DEFAULT, but not the CHECK constraints of an
        # existing table).  Only whitespace and case are ignored here; the constraint
        # text is asserted as the file spells it.
        sql_text = (
            Path(__file__).resolve().parents[1] / "migrations" / "001_inbox_events.sql"
        ).read_text(encoding="utf-8")
        expected = {
            "client_id": "CHECK (octet_length(client_id) BETWEEN 1 AND 512)",
            "mailbox_ref": "CHECK (octet_length(mailbox_ref) BETWEEN 1 AND 512)",
            "provider": "CHECK (octet_length(provider) BETWEEN 1 AND 512)",
            "message_id": "CHECK (octet_length(message_id) BETWEEN 1 AND 512)",
            "event_key": "CHECK (event_key ~ '^[0-9a-f]{64}$')",
            "payload_sha256": "CHECK (payload_sha256 ~ '^[0-9a-f]{64}$')",
            "normalized_event": "CHECK (jsonb_typeof(normalized_event) = 'object')",
        }
        for column, clause in expected.items():
            with self.subTest(column=column):
                self.assertEqual(sql_text.count(clause), 1, clause)
        self.assertEqual(sql_text.count("CHECK ("), len(expected))
        self.assertNotIn("CHECK (true)", sql_text)
        # the catalogue renders the same rules differently, so the two spellings are
        # kept side by side here: what the file says must stay the stricter one
        self.assertNotIn("char_length", sql_text)

# --- J -----------------------------------------------------------------------


class TestJConcurrentSameEvent(PostgresTestCase):
    def test_j_two_physical_connections_appended_then_duplicate(self):
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)
        binding = fixture_binding()

        for attempt in range(3):
            event = make_event(message_id="Msg-J%d" % attempt)
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def worker(payload):
                try:
                    barrier.wait(timeout=30)
                    results.append(store.append(payload, binding=copy.deepcopy(binding)))
                except BaseException as exc:  # never suppress thread exceptions
                    errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=(copy.deepcopy(event),))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            for thread in threads:
                self.assertFalse(thread.is_alive(), "worker thread did not finish")
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(sorted(item["status"] for item in results), ["APPENDED", "DUPLICATE"])
            self.assertEqual(len({item["inbox_id"] for item in results}), 1)
            self.assertEqual(len({item["stored_at"] for item in results}), 1)
            self.assertEqual(self.raw_count(), attempt + 1)

        self.assertEqual(recording.calls, 6, "each append must open its own connection")
        self.assertEqual(len({id(conn) for conn in recording.connections}), 6)
        for conn in recording.connections:
            self.assertTrue(conn.closed, "store must close its own connection")

    def test_j2_read_committed_is_enforced_over_a_repeatable_read_session(self):
        # A hostile session default must not survive into append's transaction:
        # the second statement has to see a row committed by a concurrent
        # insert, which only READ COMMITTED guarantees here.
        def repeatable_read_factory():
            conn = self.connect_factory()
            with conn.cursor() as cur:
                cur.execute(
                    "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL"
                    " REPEATABLE READ"
                )
            conn.commit()
            return conn

        probe = repeatable_read_factory()
        try:
            with probe.cursor() as cur:
                cur.execute("SHOW default_transaction_isolation")
                self.assertEqual(cur.fetchone()[0], "repeatable read")
                cur.execute("SHOW transaction_isolation")
                self.assertEqual(cur.fetchone()[0], "repeatable read")
            probe.rollback()
        finally:
            probe.close()

        store = InboxStore(connect_factory=repeatable_read_factory, schema=self.schema)
        event = make_event(message_id="Msg-J2")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker(payload):
            try:
                barrier.wait(timeout=30)
                results.append(store.append(payload, binding=fixture_binding()))
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(copy.deepcopy(event),))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(item["status"] for item in results), ["APPENDED", "DUPLICATE"])
        self.assertEqual(self.raw_count(), 1)


# --- K -----------------------------------------------------------------------


class TestKConcurrentConflict(PostgresTestCase):
    def test_k_two_connections_one_key_two_bodies_give_one_row(self):
        event_a = make_event(message_id="Msg-K")
        event_b = copy.deepcopy(event_a)
        event_b["body_text"] = "Different body"
        self.assertEqual(event_a["event_key"], event_b["event_key"])
        self.assertNotEqual(
            expected_payload_sha256(event_a), expected_payload_sha256(event_b)
        )

        barrier = threading.Barrier(2)
        results = []
        errors = []
        binding = fixture_binding()

        def worker(payload):
            try:
                barrier.wait(timeout=30)
                results.append(self.store.append(payload, binding=binding))
            except InboxStoreError as exc:
                results.append({"status": exc.code})
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(payload,))
            for payload in (event_a, event_b)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for thread in threads:
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(item["status"] for item in results),
            ["APPENDED", "INBOX_EVENT_CONFLICT"],
        )
        self.assertEqual(self.raw_count(), 1)
        winner = [item for item in results if item["status"] == "APPENDED"][0]
        row = self.raw_row(event_a["event_key"])
        self.assertEqual(str(row["inbox_id"]), winner["inbox_id"])
        self.assertIn(
            row["normalized_event"]["body_text"], ("Please explain", "Different body")
        )


# --- L -----------------------------------------------------------------------


class TestLCommitFailure(PostgresTestCase):
    def test_l_deferred_constraint_trigger_fails_the_commit(self):
        schema = sql.Identifier(self.schema)
        setup = (
            sql.SQL(
                "CREATE FUNCTION {}.f107_l_fail() RETURNS trigger LANGUAGE plpgsql AS"
                " $f$ BEGIN RAISE EXCEPTION 'F107_DEFERRED_FAILPOINT'; END $f$"
            ).format(schema),
            sql.SQL(
                "CREATE CONSTRAINT TRIGGER f107_l_deferred AFTER INSERT ON"
                " {}.inbox_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW"
                " EXECUTE FUNCTION {}.f107_l_fail()"
            ).format(schema, schema),
        )
        conn = self.connect_factory()
        try:
            with conn.cursor() as cur:
                for statement in setup:
                    cur.execute(statement)
            conn.commit()
        finally:
            conn.close()

        trigged = self.fetch(
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND NOT t.tgisinternal AND t.tgname = 'f107_l_deferred'",
            (self.schema,),
        )
        self.assertEqual(len(trigged), 1, "deferred constraint trigger was not created")

        event = fixture_event()
        with self.assertRaises(psycopg.Error) as ctx:
            self.store.append(event, binding=fixture_binding())
        self.assertNotIsInstance(ctx.exception, InboxStoreError)
        self.assertIn("F107_DEFERRED_FAILPOINT", str(ctx.exception))
        self.assertEqual(self.raw_count(), 0)

        drop = (
            sql.SQL("DROP TRIGGER f107_l_deferred ON {}.inbox_events").format(schema),
            sql.SQL("DROP FUNCTION {}.f107_l_fail()").format(schema),
        )
        conn = self.connect_factory()
        try:
            with conn.cursor() as cur:
                for statement in drop:
                    cur.execute(statement)
            conn.commit()
        finally:
            conn.close()

        result = self.store.append(event, binding=fixture_binding())
        self.assertEqual(result["status"], "APPENDED")
        self.assertEqual(self.raw_count(), 1)


# --- M -----------------------------------------------------------------------


CHILD_SCRIPT = textwrap.dedent(
    """
    import json, os, sys
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.environ["F107_CHILD_SOURCE"])
    from f1_07_inbox.migrate import local_connect_factory
    from f1_07_inbox.store import InboxStore

    fixtures = os.path.join(os.environ["F107_CHILD_SOURCE"], "f1_07_inbox", "tests", "fixtures")
    with open(os.path.join(fixtures, "normalized_normal.json"), encoding="utf-8") as handle:
        event = json.load(handle)
    with open(os.path.join(fixtures, "binding_normal.json"), encoding="utf-8") as handle:
        binding = json.load(handle)

    factory = local_connect_factory(os.environ["F107_TEST_DSN"], connect_timeout=5)
    store = InboxStore(connect_factory=factory, schema=os.environ["F107_SCHEMA"])
    found = store.get(
        client_id=binding["client_id"],
        mailbox_ref=binding["mailbox_ref"],
        event_key=event["event_key"],
    )
    replay = store.append(event, binding=binding)
    print(json.dumps({
        "found": found is not None,
        "inbox_id": found["inbox_id"] if found else None,
        "stored_at": found["stored_at"] if found else None,
        "event": found["event"] if found else None,
        "replay": replay["status"],
        "replay_inbox_id": replay["inbox_id"],
        "replay_stored_at": replay["stored_at"],
    }, ensure_ascii=False))
    """
)


class TestMRestart(PostgresTestCase):
    def test_m_new_process_and_connection_see_the_row_and_replay_duplicate(self):
        event = fixture_event()
        first = self.store.append(event, binding=fixture_binding())
        self.assertEqual(first["status"], "APPENDED")

        env = dict(os.environ)
        env["F107_TEST_DSN"] = LOCAL_DSN
        env["F107_SCHEMA"] = self.schema
        env["F107_CHILD_SOURCE"] = str(SOURCE_ROOT)
        argv = [sys.executable, "-B", "-c", CHILD_SCRIPT]
        self.assertNotIn(LOCAL_DSN, " ".join(argv), "the DSN must not travel in argv")

        completed = subprocess.run(
            argv, cwd=str(SOURCE_ROOT), env=env, capture_output=True, timeout=180
        )
        stdout = completed.stdout.decode("utf-8", "replace")
        stderr = completed.stderr.decode("utf-8", "replace")
        self.assertEqual(completed.returncode, 0, stdout + stderr)
        lines = [line for line in stdout.splitlines() if line.startswith("{")]
        self.assertEqual(len(lines), 1, stdout)
        child = json.loads(lines[0])
        self.assertTrue(child["found"], "the new process must see the stored row")
        self.assertEqual(child["inbox_id"], first["inbox_id"])
        self.assertEqual(child["stored_at"], first["stored_at"])
        self.assertEqual(child["event"], expected_canonical(event))
        self.assertEqual(child["replay"], "DUPLICATE")
        self.assertEqual(child["replay_inbox_id"], first["inbox_id"])
        self.assertEqual(child["replay_stored_at"], first["stored_at"])
        self.assertEqual(self.raw_count(), 1)


# --- N -----------------------------------------------------------------------


class TestNMigration(PostgresTestCase):
    def test_n1_repeated_migration_keeps_the_row(self):
        event = fixture_event()
        self.assertEqual(
            self.store.append(event, binding=fixture_binding())["status"], "APPENDED"
        )
        for _ in range(2):
            apply_migration(connect_factory=self.connect_factory, schema=self.schema)
        self.assertEqual(self.raw_count(), 1)
        self.assertIsNotNone(
            self.store.get(
                client_id="client-test", mailbox_ref="mb-test", event_key=event["event_key"]
            )
        )
        self.assertEqual(
            self.store.append(event, binding=fixture_binding())["status"], "DUPLICATE"
        )

    def test_n2_incompatible_existing_table_is_not_accepted(self):
        other = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, other)
        self.create_schema_with_table(other, "bogus integer, other text")
        self.assertCode(
            "INVALID_SCHEMA",
            apply_migration,
            connect_factory=self.connect_factory,
            schema=other,
        )
        self.assertEqual(self.column_names(other), ["bogus", "other"])
        self.assertEqual(len(self.column_names(other)), 2)

    def test_n3_missing_unique_constraints_are_not_accepted(self):
        other = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, other)
        self.create_schema_with_table(
            other,
            with_checks(
                "inbox_id uuid PRIMARY KEY,"
                " client_id text NOT NULL,"
                " mailbox_ref text NOT NULL,"
                " provider text NOT NULL,"
                " message_id text NOT NULL,"
                " event_key text NOT NULL,"
                " payload_sha256 text NOT NULL,"
                " normalized_event jsonb NOT NULL,"
                " stored_at timestamptz NOT NULL DEFAULT clock_timestamp()"
            ),
        )
        self.assertCode(
            "INVALID_SCHEMA",
            apply_migration,
            connect_factory=self.connect_factory,
            schema=other,
        )
        self.assertEqual(len(self.column_names(other)), 9)

    def test_n4_extra_column_is_not_accepted(self):
        other = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, other)
        self.create_schema_with_table(
            other,
            with_checks(
                "inbox_id uuid PRIMARY KEY,"
                " client_id text NOT NULL,"
                " mailbox_ref text NOT NULL,"
                " provider text NOT NULL,"
                " message_id text NOT NULL,"
                " event_key text NOT NULL,"
                " payload_sha256 text NOT NULL,"
                " normalized_event jsonb NOT NULL,"
                " stored_at timestamptz NOT NULL DEFAULT clock_timestamp(),"
                " lead_id text,"
                " UNIQUE (client_id, mailbox_ref, provider, message_id),"
                " UNIQUE (client_id, mailbox_ref, event_key)"
            ),
        )
        self.assertCode(
            "INVALID_SCHEMA",
            apply_migration,
            connect_factory=self.connect_factory,
            schema=other,
        )
        self.assertIn("lead_id", self.column_names(other))

    def test_n5_wrong_nullability_and_type_are_not_accepted(self):
        # Section 6 names the column types and NOT NULL explicitly.  Every table here
        # carries the seven CHECK clauses of the DDL, so the CHECK comparison agrees
        # with them and the type/nullability comparison is what has to refuse.
        #
        # The varchar(512) case is refused by TWO checks, and that is measured rather
        # than assumed: for a varchar column the server rewrites the constraint into
        # ``octet_length((client_id)::text)``, so its catalogue text no longer matches
        # the DDL's (probe_r9_m32.py, in the owner's work log).  That is exactly why
        # the wrong type and the missing NOT NULL each get a table of their own below:
        # otherwise a mutation that disables only those two comparisons survives the
        # whole suite - measured on this build, m32 of the 40-mutation sweep.
        varchar_case = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, varchar_case)
        self.create_schema_with_table(
            varchar_case,
            with_checks(
                "inbox_id uuid PRIMARY KEY,"
                " client_id varchar(512),"
                " mailbox_ref text NOT NULL,"
                " provider text NOT NULL,"
                " message_id text NOT NULL,"
                " event_key text NOT NULL,"
                " payload_sha256 text NOT NULL,"
                " normalized_event jsonb NOT NULL,"
                " stored_at timestamptz NOT NULL DEFAULT clock_timestamp(),"
                " UNIQUE (client_id, mailbox_ref, provider, message_id),"
                " UNIQUE (client_id, mailbox_ref, event_key)"
            ),
        )
        self.assertCode(
            "INVALID_SCHEMA",
            apply_migration,
            connect_factory=self.connect_factory,
            schema=varchar_case,
        )

        # wrong type only: inbox_id is not referenced by any CHECK, so the seven
        # clauses keep their canonical text and stay quiet here
        wrong_type = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, wrong_type)
        self.create_schema_with_table(
            wrong_type,
            with_checks(
                "inbox_id text PRIMARY KEY,"
                " client_id text NOT NULL,"
                " mailbox_ref text NOT NULL,"
                " provider text NOT NULL,"
                " message_id text NOT NULL,"
                " event_key text NOT NULL,"
                " payload_sha256 text NOT NULL,"
                " normalized_event jsonb NOT NULL,"
                " stored_at timestamptz NOT NULL DEFAULT clock_timestamp(),"
                " UNIQUE (client_id, mailbox_ref, provider, message_id),"
                " UNIQUE (client_id, mailbox_ref, event_key)"
            ),
        )
        self.assertCode(
            "INVALID_SCHEMA",
            apply_migration,
            connect_factory=self.connect_factory,
            schema=wrong_type,
        )

        # missing NOT NULL only: the primary key keeps inbox_id NOT NULL, and no CHECK
        # references stored_at, so the nullable stamp is the single difference
        nullable_stamp = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, nullable_stamp)
        self.create_schema_with_table(
            nullable_stamp,
            with_checks(
                "inbox_id uuid PRIMARY KEY,"
                " client_id text NOT NULL,"
                " mailbox_ref text NOT NULL,"
                " provider text NOT NULL,"
                " message_id text NOT NULL,"
                " event_key text NOT NULL,"
                " payload_sha256 text NOT NULL,"
                " normalized_event jsonb NOT NULL,"
                " stored_at timestamptz DEFAULT clock_timestamp(),"
                " UNIQUE (client_id, mailbox_ref, provider, message_id),"
                " UNIQUE (client_id, mailbox_ref, event_key)"
            ),
        )
        self.assertCode(
            "INVALID_SCHEMA",
            apply_migration,
            connect_factory=self.connect_factory,
            schema=nullable_stamp,
        )

        # positive control: the same hand-written block with the DDL's types and NOT
        # NULL is accepted, so this test cannot pass by refusing everything
        control = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, control)
        self.create_schema_with_table(
            control,
            with_checks(
                "inbox_id uuid PRIMARY KEY,"
                " client_id text NOT NULL,"
                " mailbox_ref text NOT NULL,"
                " provider text NOT NULL,"
                " message_id text NOT NULL,"
                " event_key text NOT NULL,"
                " payload_sha256 text NOT NULL,"
                " normalized_event jsonb NOT NULL,"
                " stored_at timestamptz NOT NULL DEFAULT clock_timestamp(),"
                " UNIQUE (client_id, mailbox_ref, provider, message_id),"
                " UNIQUE (client_id, mailbox_ref, event_key)"
            ),
        )
        apply_migration(connect_factory=self.connect_factory, schema=control)

    def test_n6_missing_or_misplaced_primary_key_is_not_accepted(self):
        # Section 6 names the primary key explicitly: a table that matches on
        # columns, types, NOT NULL and both scoped UNIQUEs but has no primary key
        # (or has it on another column) is still an incompatible structure.
        # inbox_id is declared NOT NULL here on purpose, so the ONLY difference
        # from the expected structure is the primary key itself - otherwise the
        # NOT NULL check would mask the primary-key check and this test would
        # prove nothing.
        without_pk = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, without_pk)
        self.create_schema_with_table(
            without_pk,
            with_checks(
                "inbox_id uuid NOT NULL,"
                " client_id text NOT NULL,"
                " mailbox_ref text NOT NULL,"
                " provider text NOT NULL,"
                " message_id text NOT NULL,"
                " event_key text NOT NULL,"
                " payload_sha256 text NOT NULL,"
                " normalized_event jsonb NOT NULL,"
                " stored_at timestamptz NOT NULL DEFAULT clock_timestamp(),"
                " UNIQUE (client_id, mailbox_ref, provider, message_id),"
                " UNIQUE (client_id, mailbox_ref, event_key)"
            ),
        )
        misplaced_pk = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, misplaced_pk)
        self.create_schema_with_table(
            misplaced_pk,
            with_checks(
                "inbox_id uuid NOT NULL,"
                " client_id text NOT NULL,"
                " mailbox_ref text NOT NULL,"
                " provider text NOT NULL,"
                " message_id text NOT NULL,"
                " event_key text NOT NULL,"
                " payload_sha256 text NOT NULL,"
                " normalized_event jsonb NOT NULL,"
                " stored_at timestamptz NOT NULL DEFAULT clock_timestamp() PRIMARY KEY,"
                " UNIQUE (client_id, mailbox_ref, provider, message_id),"
                " UNIQUE (client_id, mailbox_ref, event_key)"
            ),
        )
        for other in (without_pk, misplaced_pk):
            with self.subTest(schema=other):
                self.assertCode(
                    "INVALID_SCHEMA",
                    apply_migration,
                    connect_factory=self.connect_factory,
                    schema=other,
                )

    # -- the two checks the review round added: the DDL DEFAULT of stored_at
    # -- and constraints that ON CONFLICT cannot use as arbiters --------------

    GOOD_STORED_AT = "stored_at timestamptz NOT NULL DEFAULT clock_timestamp()"

    def test_n7_the_expected_ddl_default_is_accepted_on_a_second_migration(self):
        # The positive control for the two new checks: a table created by this
        # package's own DDL carries exactly the expected default and no deferrable
        # constraint, so re-running the migration over it must stay green.
        self.assertEqual(self.stored_at_default(self.schema), "clock_timestamp()")
        self.assertEqual(set(self.constraint_deferrability(self.schema).values()), {False})
        apply_migration(connect_factory=self.connect_factory, schema=self.schema)
        self.assertEqual(self.stored_at_default(self.schema), "clock_timestamp()")

    def test_n8_stored_at_without_a_default_is_not_accepted(self):
        # Section 6 fixes the DDL as stored_at timestamptz NOT NULL DEFAULT
        # clock_timestamp().  The component never writes stored_at itself, so a
        # table whose stamp column has no default at all cannot produce the
        # expected receipts: it is an incompatible structure, refused - not
        # patched by an ALTER.
        other = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, other)
        self.create_compatible_table(other, stored_at="stored_at timestamptz NOT NULL")
        self.assertIsNone(self.stored_at_default(other))
        self.assert_migration_refused(other)
        self.assertIsNone(self.stored_at_default(other), "no automatic ALTER")
        self.assertEqual(len(self.column_names(other)), 9)

    def test_n9_stored_at_with_a_wrong_default_is_not_accepted(self):
        for default, catalog_text in (
            ("now()", "now()"),
            ("'2020-01-01T00:00:00+00'::timestamptz",
             "'2020-01-01 00:00:00+00'::timestamp with time zone"),
        ):
            with self.subTest(default=default):
                other = self.new_schema_name()
                self.addCleanup(self.drop_own_schema, other)
                self.create_compatible_table(
                    other,
                    stored_at="stored_at timestamptz NOT NULL DEFAULT %s" % default,
                )
                # the neighbouring default really is there and really differs from
                # the one the task fixes, so this test cannot pass by accident
                self.assertEqual(self.stored_at_default(other), catalog_text)
                self.assertNotEqual(self.stored_at_default(other), "clock_timestamp()")
                self.assert_migration_refused(other)
                self.assertEqual(
                    self.stored_at_default(other), catalog_text, "no automatic ALTER"
                )

    def test_n10_deferrable_primary_key_is_not_accepted(self):
        # ON CONFLICT cannot use a deferrable PK as an arbiter, so a table with
        # one accepts no append at all - it must be refused as incompatible.
        other = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, other)
        self.create_compatible_table(
            other,
            stored_at=self.GOOD_STORED_AT,
            pk="PRIMARY KEY (inbox_id) DEFERRABLE INITIALLY DEFERRED",
        )
        before = self.constraint_deferrability(other)
        self.assertIn(True, before.values())
        self.assert_migration_refused(other)
        self.assertEqual(
            self.constraint_deferrability(other), before, "no automatic ALTER"
        )

    def test_n11_deferrable_unique_constraints_are_not_accepted(self):
        other = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, other)
        self.create_compatible_table(
            other,
            stored_at=self.GOOD_STORED_AT,
            unique_tail=" DEFERRABLE INITIALLY IMMEDIATE",
        )
        before = self.constraint_deferrability(other)
        self.assertEqual(sorted(before.values()), [False, True, True])
        self.assert_migration_refused(other)
        self.assertEqual(
            self.constraint_deferrability(other), before, "no automatic ALTER"
        )
        # ... and here is what the refusal protects against: on such a table the
        # store's own ON CONFLICT cannot run, so a migration that accepted the
        # structure would have shipped a table the component cannot write to.
        store = InboxStore(connect_factory=self.connect_factory, schema=other)
        with self.assertRaises(psycopg.Error) as ctx:
            store.append(fixture_event(), binding=fixture_binding())
        self.assertIn("deferrable", str(ctx.exception).lower())
        self.assertEqual(self.raw_count(other), 0)

    # -- the acceptance finding of this round: CREATE SCHEMA must not be committed
    # -- on its own, whatever mode the factory's connection is in ---------------

    def test_n12_an_autocommit_factory_migrates_in_one_transaction(self):
        # Task section 6: "миграция транзакционная".  A caller may well hand over a
        # pool whose connections are in autocommit mode; then CREATE SCHEMA, CREATE
        # TABLE and the catalogue verification would each commit on their own and
        # commit() would be a no-op, so a failure at the table would leave a
        # committed, table-less schema behind.  The runner takes the connection out
        # of autocommit itself, so the migration is one transaction no matter what
        # the factory returns.  Positive control for the refusal in N13.
        handed = []
        spies = []

        def factory():
            spy = TransactionSpy(self.connect_factory())
            spy.autocommit = True
            handed.append(spy._connection)
            spies.append(spy)
            return spy

        other = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, other)
        self.assertFalse(self.schema_exists(other), "the schema must be brand new")

        apply_migration(connect_factory=factory, schema=other)

        self.assertEqual(len(spies), 1, "the migration opens exactly one connection")
        self.assertEqual(spies[0].events, ["commit", "close"], "one commit, no rollback")
        self.assertFalse(
            handed[0].autocommit,
            "the connection must not stay in autocommit: CREATE SCHEMA and CREATE TABLE"
            " have to belong to the same transaction",
        )
        self.assertTrue(self.schema_exists(other))
        self.assertEqual(len(self.column_names(other)), 9)
        # the table really is the structure the migration verified: a repeat over it
        # (on an ordinary connection this time) stays green
        apply_migration(connect_factory=self.connect_factory, schema=other)
        self.assertEqual(len(self.column_names(other)), 9)

    def test_n13_a_table_error_rolls_the_new_schema_back_in_autocommit_mode(self):
        # The finding the coordinator returned this build for.  Measured on the
        # previous build: with an autocommit connection the CREATE SCHEMA statement
        # was committed on its own, so when the CREATE TABLE statement failed the
        # new schema survived a rollback that had nothing left to undo.
        # The table error is a real PostgreSQL error (a RAISE from a DDL event
        # trigger, SQLSTATE P0001), not a mocked cursor: the disposable local
        # container the task itself prescribes creates its role as cluster
        # superuser, which an event trigger requires.
        self.assertTrue(
            self.role_is_superuser(),
            "the DDL event trigger used to make CREATE TABLE fail needs the cluster"
            " superuser that the task's own POSTGRES_USER is created as",
        )
        probe = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, probe)
        self.create_blocking_event_trigger(probe)
        self.addCleanup(self.drop_blocking_event_trigger)

        target = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, target)
        self.assertFalse(self.schema_exists(target))

        handed = []
        with self.assertRaises(psycopg.Error) as ctx:
            apply_migration(
                connect_factory=self.factory_with_block(target, handed),
                schema=target,
            )
        # the failure really is the table statement's, not a domain code and not
        # something that happened before the schema was created
        self.assertIn("CREATE TABLE is blocked", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, InboxStoreError)
        self.assertEqual(len(handed), 1)
        self.assertTrue(handed[0].closed, "the migration closes the connection it opened")
        # the property under test: nothing of the failed migration is left behind,
        # the schema included - checked from a different connection
        self.assertFalse(
            self.schema_exists(target),
            "the schema created by the failed migration must be rolled back, not"
            " committed on its own",
        )
        self.assertEqual(self.column_names(target), [])

        # control 1: the very same factory shape migrates fine while the trigger's
        # guard names another schema, so the refusal above came from the table
        # statement and not from autocommit, from the factory or from the trigger
        # being broken in general
        control = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, control)
        apply_migration(
            connect_factory=self.factory_with_block(control + "_other", []),
            schema=control,
        )
        self.assertTrue(self.schema_exists(control))
        self.assertEqual(len(self.column_names(control)), 9)

        # control 2: the same table error on an ordinary (non-autocommit) connection
        # rolled the schema back before this round and must keep doing so
        plain = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, plain)
        plain_handed = []
        with self.assertRaises(psycopg.Error):
            apply_migration(
                connect_factory=self.factory_with_block(plain, plain_handed, autocommit=False),
                schema=plain,
            )
        self.assertFalse(self.schema_exists(plain))
        self.assertFalse(plain_handed[0].autocommit, "the plain connection is left as it was")

    # -- the acceptance finding of this round: an existing table without the CHECK
    # -- constraints of the fixed DDL was accepted as compatible -----------------

    WEAKENED_CHECKS = ("CHECK (true)",) + CHECK_CLAUSES[1:]
    # Same semantics, written the way the server itself folds together (>=/<=
    # instead of BETWEEN, mixed case, an explicit ::text cast on the regex columns).
    RESPELLED_CHECKS = (
        "CHECK (OCTET_LENGTH(client_id) >= 1 AND octet_length(client_id) <= 512)",
        "CHECK (OCTET_LENGTH(mailbox_ref) >= 1 AND octet_length(mailbox_ref) <= 512)",
        "CHECK (OCTET_LENGTH(provider) >= 1 AND octet_length(provider) <= 512)",
        "CHECK (OCTET_LENGTH(message_id) >= 1 AND octet_length(message_id) <= 512)",
        "CHECK (event_key::text ~ '^[0-9a-f]{64}$'::text)",
        "CHECK (payload_sha256::text ~ '^[0-9a-f]{64}$'::text)",
        "CHECK (JSONB_TYPEOF(normalized_event) = 'object')",
    )

    def test_n14_check_constraints_of_an_existing_table_are_checked(self):
        # Section 6 fixes the DDL of the table, CHECK constraints included ("6.
        # ИЗОЛИРОВАННАЯ СХЕМА И ТОЧНЫЙ DDL"), and asks apply_migration to verify the
        # structure it finds through the PostgreSQL catalogue: a pre-existing table
        # whose CHECK constraints are missing or weakened is as incompatible as one
        # with the wrong columns.  Measured on the returned build: such a table was
        # accepted, so the bounds the DDL declares were not enforced by the
        # structure the component was willing to write into.
        #
        # Every table here matches the expected structure in every other respect
        # (columns, types, NOT NULL, primary key, both scoped UNIQUEs, the
        # clock_timestamp() default), so the CHECK set is the only part that can
        # decide the outcome.
        absent = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, absent)
        self.create_compatible_table(
            absent, stored_at=self.GOOD_STORED_AT, checks=()
        )
        self.assertEqual(self.check_constraints(absent), [])
        self.assert_migration_refused(absent)
        self.assertEqual(self.check_constraints(absent), [], "no automatic ALTER")
        self.assertEqual(len(self.column_names(absent)), 9)

        weakened = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, weakened)
        self.create_compatible_table(
            weakened, stored_at=self.GOOD_STORED_AT, checks=self.WEAKENED_CHECKS
        )
        # the neighbouring seven constraints really are there and exactly one of
        # them is the weakened one, so this test cannot pass by accident
        self.assertEqual(len(self.check_constraints(weakened)), 7)
        self.assertIn("CHECK (true)", self.check_constraints(weakened))
        self.assert_migration_refused(weakened)
        self.assertIn("CHECK (true)", self.check_constraints(weakened))

        # one constraint out of seven missing: the set has to match, not just its size
        one_missing = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, one_missing)
        self.create_compatible_table(
            one_missing, stored_at=self.GOOD_STORED_AT, checks=CHECK_CLAUSES[1:]
        )
        self.assertEqual(len(self.check_constraints(one_missing)), 6)
        self.assert_migration_refused(one_missing)
        self.assertEqual(
            len(self.check_constraints(one_missing)), 6, "no automatic ALTER"
        )

        # positive control 1: the same builder with the seven clauses of the DDL is
        # a compatible structure and must stay accepted
        compatible = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, compatible)
        self.create_compatible_table(compatible, stored_at=self.GOOD_STORED_AT)
        self.assertEqual(len(self.check_constraints(compatible)), 7)
        apply_migration(connect_factory=self.connect_factory, schema=compatible)
        self.assertEqual(len(self.check_constraints(compatible)), 7)
        self.assertEqual(len(self.column_names(compatible)), 9)

        # positive control 2: the same seven constraints written the way the server
        # normalises them are the same structure - the check compares the catalogue
        # text, not the author's spelling, so it must not refuse this table
        respelled = self.new_schema_name()
        self.addCleanup(self.drop_own_schema, respelled)
        self.create_compatible_table(
            respelled, stored_at=self.GOOD_STORED_AT, checks=self.RESPELLED_CHECKS
        )
        self.assertEqual(
            self.check_constraints(respelled), self.check_constraints(compatible)
        )
        apply_migration(connect_factory=self.connect_factory, schema=respelled)
        self.assertEqual(len(self.column_names(respelled)), 9)

    def test_n15_the_expected_check_text_is_what_the_server_reports(self):
        # The migration compares the catalogue text of the seven CHECK constraints
        # with the constants in migrate.py, which were measured on PostgreSQL 16.15.
        # This test keeps that measurement honest: it reads the text a real server
        # produces for this package's own DDL (the table of this test class was
        # created by apply_migration) and compares it with the expected set, so a
        # server that reports a different normal form fails loudly here instead of
        # silently refusing every correct table.
        self.assertEqual(
            self.check_constraints(self.schema), sorted(EXPECTED_CHECK_CONSTRAINTS)
        )
        self.assertEqual(len(EXPECTED_CHECK_CONSTRAINTS), 7)
        self.assertEqual(len(set(EXPECTED_CHECK_CONSTRAINTS)), 7)


# --- O -----------------------------------------------------------------------


class TestOIsolationOfFailures(PostgresTestCase):
    def test_o1_bad_append_does_not_break_the_next_write(self):
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)

        bad = fixture_event()
        bad["event_key"] = "0" * 64
        self.assertCode(
            "INVALID_EVENT_KEY", store.append, bad, binding=fixture_binding()
        )
        self.assertEqual(recording.calls, 0, "validation must precede the connection")
        self.assertEqual(self.raw_count(), 0)

        broken_binding = make_binding(campaign="not-a-uuid")
        self.assertCode(
            "INVALID_BINDING", store.append, fixture_event(), binding=broken_binding
        )
        self.assertEqual(recording.calls, 0)

        good = fixture_event()
        result = store.append(good, binding=fixture_binding())
        self.assertEqual(result["status"], "APPENDED")
        self.assertEqual(self.raw_count(), 1)
        self.assertEqual(recording.calls, 1, "only the accepted append opened a connection")
        for conn in recording.connections:
            self.assertTrue(conn.closed)

    def test_o2_database_error_is_raised_as_psycopg_error_and_closes_connection(self):
        recording = RecordingFactory(self.connect_factory)
        store = InboxStore(connect_factory=recording, schema=self.schema)
        conn = self.connect_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP TABLE {}.inbox_events").format(sql.Identifier(self.schema))
                )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(psycopg.Error):
            store.append(fixture_event(), binding=fixture_binding())
        self.assertEqual(recording.calls, 1)
        self.assertTrue(recording.connections[0].closed)

    def test_o3_local_target_guard_rejects_foreign_dsn_before_connecting(self):
        good = parse_local_dsn(LOCAL_DSN)
        self.assertEqual(good["host"], "127.0.0.1")
        self.assertEqual(good["dbname"], LOCAL_DBNAME)
        self.assertEqual(good["user"], LOCAL_USER)
        self.assertIsInstance(good["port"], int)

        rejected = (
            "host=127.0.0.1 port=%d dbname=postgres user=f107_test" % good["port"],
            "host=127.0.0.1 port=%d dbname=%s user=postgres" % (good["port"], LOCAL_DBNAME),
            "host=localhost port=%d dbname=%s user=%s" % (good["port"], LOCAL_DBNAME, LOCAL_USER),
            "host=/var/run/postgresql dbname=%s user=%s" % (LOCAL_DBNAME, LOCAL_USER),
            "host=127.0.0.1,10.0.0.5 port=%d dbname=%s user=%s"
            % (good["port"], LOCAL_DBNAME, LOCAL_USER),
            "host=127.0.0.1 hostaddr=10.0.0.5 port=%d dbname=%s user=%s"
            % (good["port"], LOCAL_DBNAME, LOCAL_USER),
            "service=prod dbname=%s user=%s" % (LOCAL_DBNAME, LOCAL_USER),
            "host=127.0.0.1 port=not-a-port dbname=%s user=%s" % (LOCAL_DBNAME, LOCAL_USER),
            "host=127.0.0.1 port=0 dbname=%s user=%s" % (LOCAL_DBNAME, LOCAL_USER),
            "host=127.0.0.1 port=70000 dbname=%s user=%s" % (LOCAL_DBNAME, LOCAL_USER),
            "postgresql://%s@db.example.com:5432/%s" % (LOCAL_USER, LOCAL_DBNAME),
            "host=127.0.0.1 port=%d dbname=%s user=%s sslmode=require"
            % (good["port"], LOCAL_DBNAME, LOCAL_USER),
            "",
        )
        for dsn in rejected:
            with self.subTest(dsn=dsn[:40]):
                with self.assertRaises(LocalTargetError):
                    parse_local_dsn(dsn)
                # the rejected DSN must never reach a connection attempt: the
                # factory refuses to be built at all.
                with self.assertRaises(LocalTargetError):
                    local_connect_factory(dsn)
        with self.assertRaises(LocalTargetError):
            local_connect_factory(
                "host=127.0.0.1 port=%d dbname=postgres user=f107_test" % good["port"]
            )

    def test_o4_storage_conflict_is_not_reported_as_a_fake_duplicate(self):
        event = fixture_event()
        self.assertEqual(
            self.store.append(event, binding=fixture_binding())["status"], "APPENDED"
        )
        # A row created outside this store (or manipulated) can occupy the
        # (client, mailbox, provider, message_id) identity while carrying a
        # different event_key: the insert then conflicts, no row is found by
        # key and a fake DUPLICATE must never be produced.
        foreign_key = "f" * 64
        conn = self.connect_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "UPDATE {}.inbox_events SET event_key = %s WHERE event_key = %s"
                    ).format(sql.Identifier(self.schema)),
                    (foreign_key, event["event_key"]),
                )
            conn.commit()
        finally:
            conn.close()

        self.assertCode(
            "STORAGE_CONFLICT", self.store.append, event, binding=fixture_binding()
        )
        self.assertEqual(self.raw_count(), 1)
        row = self.raw_row(foreign_key)
        self.assertEqual(row["payload_sha256"], expected_payload_sha256(event))
        self.assertEqual(row["normalized_event"], expected_canonical(event))
        self.assertIsNone(
            self.store.get(
                client_id="client-test", mailbox_ref="mb-test", event_key=event["event_key"]
            )
        )


class TestPTargetIdentity(PostgresTestCase):
    def test_p1_foreign_database_identity_is_refused_after_connecting(self):
        import f1_07_inbox.migrate as migrate_module

        real_connect = migrate_module.psycopg.connect
        opened = []

        def redirecting_connect(**kwargs):
            kwargs["dbname"] = "postgres"
            connection = real_connect(**kwargs)
            opened.append(connection)
            return connection

        factory = local_connect_factory(LOCAL_DSN, connect_timeout=5)
        with mock.patch.object(migrate_module.psycopg, "connect", redirecting_connect):
            with self.assertRaises(LocalTargetError) as ctx:
                factory()
        self.assertEqual(ctx.exception.code, "TARGET_MISMATCH")
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed, "the mismatching connection is closed")
        self.assertEqual(self.raw_count(), 0)


class TestQModuleHygiene(PostgresTestCase):
    """The package must stay importable on every interpreter the task may meet."""

    def test_q1_no_module_docstring_carries_a_lone_surrogate(self):
        # Measured, not guessed: a module docstring holding a lone surrogate makes
        # CPython 3.13.11 refuse to import the module at all (UnicodeEncodeError at
        # import time), while 3.12.10 imports the very same file happily.  The trap
        # is easy to fall into when documenting the very rule this round adds: in a
        # non-raw docstring the text \ud800 is an *escape*, so the documented text
        # silently becomes a real surrogate.  Writing \\ud800 in the source keeps it
        # text.  This test inspects the compiled docstrings, so it catches the trap
        # on any interpreter - including the 3.12 run where nothing would fail.
        import f1_07_inbox
        import f1_07_inbox.migrate
        import f1_07_inbox.store

        for name, module in (
            ("f1_07_inbox", f1_07_inbox),
            ("f1_07_inbox.store", f1_07_inbox.store),
            ("f1_07_inbox.migrate", f1_07_inbox.migrate),
            ("test_store_postgres", sys.modules[__name__]),
        ):
            with self.subTest(module=name):
                doc = module.__doc__
                self.assertIsNotNone(doc, "%s must keep its docstring" % name)
                stray = [i for i, ch in enumerate(doc) if 0xD800 <= ord(ch) <= 0xDFFF]
                self.assertEqual(
                    stray, [], "%s.__doc__ carries a lone surrogate: %r" % (name, doc)
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
