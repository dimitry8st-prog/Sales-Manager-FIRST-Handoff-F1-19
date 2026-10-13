"""F1-07 isolated durable inbox store package (sales-manager-first).

New package for the F1-07 technical task.  It is a standalone component:
it persists an already normalized inbound event into PostgreSQL and reads it
back.  It does not read a real mailbox, does not send anything, does not
classify replies, does not authenticate providers and does not modify or import
the existing runtime modules (``postgres_store``, ``sales_store``, ``main``,
``migrate``, ``worker``, …).

Public API::

    from f1_07_inbox import InboxStore, InboxStoreError

    store = InboxStore(connect_factory=factory, schema="f1_07_local")
    store.append(event, binding=binding)   # -> {"status": "APPENDED" | "DUPLICATE", ...}
    store.get(client_id=..., mailbox_ref=..., event_key=...)  # -> dict | None

Migration entry point: ``f1_07_inbox.migrate.apply_migration`` and the CLI
``python -m f1_07_inbox.migrate`` (reads only F107_TEST_DSN / F107_SCHEMA).
"""

from f1_07_inbox.store import InboxStore, InboxStoreError

__all__ = ["InboxStore", "InboxStoreError"]
