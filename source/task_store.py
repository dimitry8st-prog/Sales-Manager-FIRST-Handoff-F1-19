import json
import uuid

import postgres_store


def _validate_task(scope, payload, max_attempts, priority):
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("INVALID_SCOPE")
    if not isinstance(payload, dict):
        raise ValueError("INVALID_TASK_PAYLOAD")
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
        raise ValueError("INVALID_MAX_ATTEMPTS")
    if not isinstance(priority, int) or not -100 <= priority <= 100:
        raise ValueError("INVALID_PRIORITY")


def create_task(scope, payload, max_attempts=3, dedupe_key=None, priority=0):
    _validate_task(scope, payload, max_attempts, priority)
    task_id = str(uuid.uuid4())
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {postgres_store.SCHEMA}.tasks
                    (task_id, scope, payload, status, attempts, max_attempts,
                     dedupe_key, priority, available_at, created_at, updated_at)
                VALUES (%s, %s, %s::jsonb, 'PENDING', 0, %s, %s, %s, NOW(), NOW(), NOW())
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                RETURNING task_id, scope, status, attempts, max_attempts, dedupe_key, priority, available_at
                """,
                (task_id, scope, json.dumps(payload), max_attempts, dedupe_key, priority),
            )
            row = cur.fetchone()
            if row is None and dedupe_key is not None:
                cur.execute(
                    f"SELECT task_id, scope, status, attempts, max_attempts, dedupe_key, priority, available_at "
                    f"FROM {postgres_store.SCHEMA}.tasks WHERE dedupe_key=%s",
                    (dedupe_key,),
                )
                row = cur.fetchone()
            return row


def get_task(task_id):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT task_id, scope, payload, status, attempts, max_attempts, "
                f"last_error, result, dedupe_key, priority, available_at, created_at, updated_at "
                f"FROM {postgres_store.SCHEMA}.tasks WHERE task_id=%s",
                (task_id,),
            )
            return cur.fetchone()


def complete_task(task_id, result=None):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.tasks "
                "SET status='SUCCEEDED', result=%s::jsonb, last_error=NULL, updated_at=NOW() "
                "WHERE task_id=%s AND status='RUNNING' "
                "RETURNING task_id, status, attempts, result",
                (json.dumps(result or {}), task_id),
            )
            return cur.fetchone()


def fail_task(task_id, error):
    safe_error = str(error)[:2000]
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {postgres_store.SCHEMA}.tasks
                SET status=CASE WHEN attempts < max_attempts THEN 'PENDING' ELSE 'FAILED' END,
                    last_error=%s,
                    available_at=CASE
                        WHEN attempts < max_attempts THEN
                            NOW() + make_interval(secs => LEAST(300, (POWER(2, attempts) * 5)::int))
                        ELSE available_at
                    END,
                    updated_at=NOW()
                WHERE task_id=%s AND status='RUNNING'
                RETURNING task_id, status, attempts, max_attempts, last_error, available_at
                """,
                (safe_error, task_id),
            )
            return cur.fetchone()


def defer_task(task_id, reason, delay_seconds=300):
    safe_error = str(reason)[:2000]
    delay_seconds = max(1, min(int(delay_seconds), 86400))
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {postgres_store.SCHEMA}.tasks
                SET status='PENDING',
                    attempts=GREATEST(0, attempts-1),
                    last_error=%s,
                    available_at=NOW() + make_interval(secs => %s),
                    updated_at=NOW()
                WHERE task_id=%s AND status='RUNNING'
                RETURNING task_id, status, attempts, max_attempts, last_error, available_at
                """,
                (safe_error, delay_seconds, task_id),
            )
            return cur.fetchone()


def claim_next_task():
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT task_id FROM {postgres_store.SCHEMA}.tasks
                WHERE status='PENDING' AND attempts < max_attempts AND available_at <= NOW()
                ORDER BY priority DESC, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                f"""
                UPDATE {postgres_store.SCHEMA}.tasks
                SET status='RUNNING', attempts=attempts+1, updated_at=NOW()
                WHERE task_id=%s
                RETURNING task_id, scope, payload, status, attempts, max_attempts, dedupe_key, priority
                """,
                (row["task_id"],),
            )
            return cur.fetchone()


def recover_stale_tasks(stale_seconds=300):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {postgres_store.SCHEMA}.tasks
                SET status=CASE WHEN attempts < max_attempts THEN 'PENDING' ELSE 'FAILED' END,
                    last_error='WORKER_LEASE_EXPIRED', available_at=NOW(), updated_at=NOW()
                WHERE status='RUNNING'
                  AND updated_at < NOW() - (%s * INTERVAL '1 second')
                RETURNING task_id, status, attempts, max_attempts, last_error
                """,
                (stale_seconds,),
            )
            return cur.fetchall()
