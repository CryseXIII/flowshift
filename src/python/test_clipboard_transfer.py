"""FlowShift clipboard transfer hardening tests (pure + runtime helper).

Covers TransferJob state, disk-space checks, retry/resume helpers, queue
serialization and the unified progress shape exposed by ClipboardManager.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_model as cm
import clipboard_transfer as ct
from clipboard_runtime import ClipboardManager
from clipboard_store import ClipboardStore
from update_safety import is_safe_to_install_update

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


def wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── job state machine ────────────────────────────────────────────────
job = ct.make_transfer_job("t1", "p1", "item1", "receive", cm.KIND_FILE, "demo.bin", 200)
check(job.status == ct.TransferStatus.pending, "job initial state pending")
check(ct.should_require_manual_start(job, 100) is True, "manual-required threshold")

job.started_at = time.monotonic() - 2.0
ct.update_progress(job, received_bytes=100, completed_chunks=[0], missing_chunks=[1],
                   status=ct.TransferStatus.running)
check(abs(job.to_progress()["percent"] - 50.0) < 1e-6, "progress 50%")
check(40.0 <= job.bytes_per_second <= 60.0, "rate computed from elapsed time")
check(1.5 <= (job.eta_seconds or 0.0) <= 2.5, "eta computed from remaining bytes")

ct.mark_retry(job, error="hash mismatch")
check(job.retry_count == 1 and job.status == ct.TransferStatus.retrying, "retry counter increments")

ct.mark_completed(job)
check(job.status == ct.TransferStatus.completed, "job completed state")
check(job.to_progress()["percent"] == 100.0, "progress 100%")

job_fail = ct.make_transfer_job("t2", "p1", "item2", "receive", cm.KIND_TEXT, "demo", 50,
                                 max_retries=1)
ct.mark_retry(job_fail, error="timeout")
ct.mark_retry(job_fail, error="timeout")
check(job_fail.status == ct.TransferStatus.failed, "failed after max retries")

check(ct.missing_chunk_indices(3, completed_chunks=[0, 2]) == [1], "missing chunk helper")
check(ct.missing_chunk_indices(3, missing_chunks=[2, 1, 1]) == [1, 2], "missing chunk helper dedupes")


# ── disk-space guard ────────────────────────────────────────────────
tmp = tempfile.mkdtemp(prefix="fs_clip_xfer_")
try:
    space = ct.check_disk_space(tmp, 1024)
    check(space["ok"] is True and space["free_bytes"] > 0, "disk check enough space")
    too_much = ct.check_disk_space(tmp, space["free_bytes"] + 1)
    check(too_much["ok"] is False and too_much["missing_bytes"] > 0, "disk check insufficient space")
    bad = ct.check_disk_space(os.path.join(tmp, "does", "not", "exist"), 1)
    check("missing_bytes" in bad and "free_bytes" in bad, "disk check handles invalid path")
finally:
    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass


# ── queue serialisation + cancel ────────────────────────────────────
queue = ct.TransferQueue(max_parallel=1, retry_delay_ms=20)
order = []
release = threading.Event()
job_a = ct.make_transfer_job("qa", "p1", "a", "send", cm.KIND_TEXT, "A", 10)
job_b = ct.make_transfer_job("qb", "p1", "b", "send", cm.KIND_TEXT, "B", 10)


def work_a(job):
    order.append("a-start")
    release.wait(2.0)
    order.append("a-end")


def work_b(job):
    order.append("b-run")


check(queue.submit(job_a, work_a) is True, "queue accepts first job")
check(queue.submit(job_b, work_b) is True, "queue accepts second job")
time.sleep(0.1)
check(order == ["a-start"], "queue runs sequentially")
busy_queue = queue.activity_snapshot()
check(busy_queue["active"] == 1 and busy_queue["queued"] == 1,
      "queue snapshot separates active and queued work")
check(set(busy_queue["blocking_job_statuses"]) == {
          ct.TransferStatus.pending, ct.TransferStatus.running},
      "queue snapshot reports blocking job statuses")
check(queue.cancel(job_b.transfer_id) is True, "queue cancels pending job")
release.set()
check(wait_until(lambda: job_a.status == ct.TransferStatus.completed, timeout=2.0),
      "queue completes running job")
check(job_b.status == ct.TransferStatus.cancelled, "cancelled job stays cancelled")
check(order == ["a-start", "a-end"], "cancelled pending job never runs")
idle_queue = queue.activity_snapshot()
check(idle_queue["active"] == 0 and idle_queue["retry_pending"] == 0,
      "queue activity clears after work")
check(queue.shutdown(timeout=1.0) is True, "queue shutdown joins worker")
check(queue.activity_snapshot()["workers_alive"] == 0, "queue worker stopped")
rejected = ct.make_transfer_job("qc", "p1", "c", "send", cm.KIND_TEXT, "C", 10)
check(queue.submit(rejected, lambda current: None) is False,
      "queue rejects submissions after shutdown")


# ── active queue is blocking and bounded shutdown is truthful ───────
active_queue = ct.TransferQueue(max_parallel=1, retry_delay_ms=20)
active_started = threading.Event()
active_release = threading.Event()
active_job = ct.make_transfer_job("active", "p1", "active", "send",
                                  cm.KIND_TEXT, "active", 10)


def active_work(current):
    active_started.set()
    active_release.wait(2.0)


check(active_queue.submit(active_job, active_work), "active queue accepts work")
check(active_started.wait(1.0), "active queue work starts")
active_snap = active_queue.activity_snapshot()
check(active_snap["active"] == 1 and active_snap["blocking"],
      "executing queue work blocks install")
safety = is_safe_to_install_update({"clipboard_activity": active_snap})
check(safety["reason"] == "clipboard_transfer_active",
      "safe-to-install query sees active queue")
check(active_job.status == ct.TransferStatus.running,
      "safe-to-install query does not abort transfer")
check(active_queue.shutdown(timeout=0.02) is False,
      "queue shutdown reports live worker at timeout")
check(active_job.status == ct.TransferStatus.running,
      "bounded shutdown does not abort executing transfer")
active_release.set()
check(active_queue.shutdown(timeout=1.0) is True, "second shutdown joins released worker")
check(active_job.status == ct.TransferStatus.completed,
      "executing transfer completes during shutdown")


# ── retry waiter is singular and cannot requeue after stop ──────────
retry_queue = ct.TransferQueue(max_parallel=1, retry_delay_ms=200)
retry_job = ct.make_transfer_job("retry", "p1", "retry", "send",
                                 cm.KIND_TEXT, "retry", 10, max_retries=3)


def fail_work(current):
    raise RuntimeError("retry me")


check(retry_queue.submit(retry_job, fail_work), "retry queue accepts work")
check(wait_until(lambda: retry_queue.activity_snapshot()["retry_pending"] == 1),
      "retry is represented as pending activity")
check(len(retry_queue._retry_timers) == 1, "one retry waiter per transfer")
check(retry_queue.shutdown(timeout=1.0), "retry queue shuts down")
time.sleep(0.25)
check(retry_queue.activity_snapshot()["retry_pending"] == 0,
      "retry does not requeue after shutdown")


# ── unified progress shape via runtime ──────────────────────────────
settings = cm.clipboard_settings({"clipboard": {
    "enabled": True,
    "sync_on_activate": True,
    "history_max_items": 999,
    "history_max_total_gb": 10.0,
    "max_auto_transfer_mb": 100,
    "clipboard_transfer_max_retries": 5,
    "clipboard_transfer_retry_delay_ms": 50,
    "clipboard_transfer_max_parallel": 1,
    "clipboard_max_transfer_kib_per_sec": 0,
}})

tmp = tempfile.mkdtemp(prefix="fs_clip_xfer_rt_")
try:
    mgr = ClipboardManager(tmp, "dev", lambda ident, msg: None, lambda: settings)
    st = mgr.store("device:A")

    text_item = cm.make_text_item("hello", seq=1)
    st.add_item(text_item, data=b"hello")

    waiting_item = dict(cm.make_binary_item("a" * 64, 101 * 1024 * 1024, seq=2,
                                            kind=cm.KIND_FILE, display_name="big.bin",
                                            available=False))
    waiting_item["metadata"] = {"transfer_status": ct.TransferStatus.waiting_manual}
    st.add_item(waiting_item, data=None)

    failed_item = dict(cm.make_binary_item("b" * 64, 10, seq=3,
                                          kind=cm.KIND_FILE, display_name="oops.bin",
                                          available=False))
    failed_item["metadata"] = {"transfer_status": ct.TransferStatus.failed,
                                "transfer_error": "Nicht genug Speicherplatz"}
    st.add_item(failed_item, data=None)

    running_item = cm.make_text_item("running", seq=4)
    st.add_item(running_item, data=b"running")
    running_job = ct.make_transfer_job("rt1", "device:A", running_item["item_id"], "send",
                                       running_item["kind"], running_item["display_name"],
                                       running_item["size"], status=ct.TransferStatus.running)
    running_job.started_at = time.monotonic() - 1.0
    ct.update_progress(running_job, received_bytes=3, sent_bytes=3, status=ct.TransferStatus.running)
    mgr._jobs[running_item["item_id"]] = running_job

    snap = mgr.progress_snapshot()
    check(snap[text_item["item_id"]]["status"] == ct.TransferStatus.completed,
          "text item progress is completed")
    check(snap[waiting_item["item_id"]]["status"] == ct.TransferStatus.waiting_manual,
          "waiting-manual item progress is waiting_manual")
    check(snap[failed_item["item_id"]]["status"] == ct.TransferStatus.failed,
          "failed item progress is failed")
    check(snap[running_item["item_id"]]["status"] == ct.TransferStatus.running,
          "running job progress is running")
    check("bytes_per_second" in snap[running_item["item_id"]], "progress shape includes rate")
    check("eta_seconds" in snap[running_item["item_id"]], "progress shape includes eta")

    activity = mgr.activity_snapshot()
    check(activity["blocking_job_statuses"] == {ct.TransferStatus.running: 1},
          "manager activity reads jobs directly")
    check(activity["blocking"] is True, "running manager job blocks install")
    ct.mark_completed(running_job)
    activity = mgr.activity_snapshot()
    check(activity["blocking_jobs"] == 0,
          "waiting_manual, completed and failed jobs do not block")
    check(mgr.set_update_maintenance(True) is True,
          "manager enters reversible update maintenance")
    check(mgr.activity_snapshot()["update_maintenance"] is True,
          "maintenance is exposed without hiding existing activity")
    check(mgr.activity_snapshot()["accepting"] is False,
          "maintenance truthfully reports new work admission closed")
    check(mgr.capture_text("device:A", "during maintenance") is None,
          "maintenance rejects new capture")
    check(mgr.handle("device:A", {"type": "clipboard_manifest"}) is False,
          "maintenance rejects incoming clipboard work")
    with mgr._lock:
        mgr._assemblers["existing-transfer"] = {}
    check(mgr._begin_incoming_operation({
        "type": "clipboard_transfer_chunk", "transfer_id": "existing-transfer"}) is True,
        "maintenance permits continuation of an admitted incoming transfer")
    mgr._end_local_operation()
    with mgr._lock:
        mgr._assemblers.pop("existing-transfer", None)
    check(mgr._queue_send_item("device:A", text_item["item_id"]) is None,
          "maintenance rejects new send work")
    check(mgr.set_update_maintenance(False) is False,
          "manager leaves reversible update maintenance")
    check(mgr.capture_text("device:A", "after maintenance") is not None,
          "clipboard admission resumes after maintenance")
    check(mgr.shutdown(timeout=1.0) is True, "manager shutdown joins transfer queue")
    check(mgr.set_update_maintenance(False) is True,
          "maintenance cannot reopen a shutting-down manager")
    check(mgr.capture_text("device:A", "after shutdown") is None,
          "manager rejects capture after shutdown")
    check(mgr.handle("device:A", {"type": "clipboard_manifest"}) is False,
          "manager rejects incoming work after shutdown")
    check(mgr._queue_send_item("device:A", text_item["item_id"]) is None,
          "manager rejects transfer submission after shutdown")

finally:
    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass


print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard transfer tests passed.")
