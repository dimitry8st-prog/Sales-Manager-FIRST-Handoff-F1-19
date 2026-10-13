import sales_manager
import sales_store
import task_store


def run_once():
    task = task_store.claim_next_task()
    if task is None:
        return {"status": "IDLE"}
    task_id = task["task_id"]
    try:
        if (task.get("payload") or {}).get("force_error"):
            raise RuntimeError("FORCED_WORKER_ERROR")
        if task["scope"].startswith("sales."):
            result_payload = sales_manager.process_task(task)
        else:
            result_payload = {"operation": "generic", "scope": task["scope"]}
        result = task_store.complete_task(task_id, result_payload)
        return {"status": "DONE", "task": result}
    except sales_store.ControlBlocked as exc:
        result = task_store.defer_task(task_id, str(exc), delay_seconds=300)
        return {"status": "DEFERRED", "task": result}
    except Exception as exc:
        result = task_store.fail_task(task_id, str(exc))
        alert_queued = False
        if (
            result is not None
            and result.get("status") == "FAILED"
            and task.get("scope") != "sales.notify_alert"
        ):
            try:
                task_store.create_task(
                    "sales.notify_alert",
                    {
                        "alert_type": "TASK_FAILED",
                        "task_id": str(task_id),
                        "scope": task.get("scope"),
                        "attempts": result.get("attempts"),
                        "error": result.get("last_error"),
                    },
                    max_attempts=8,
                    dedupe_key=f"sales-alert:task-failed:{task_id}",
                    priority=100,
                )
                alert_queued = True
            except Exception:
                alert_queued = False
        return {"status": "ERROR", "task": result, "alert_queued": alert_queued}


if __name__ == "__main__":
    print(run_once())
