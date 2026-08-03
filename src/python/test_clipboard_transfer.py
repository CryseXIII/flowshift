"""FlowShift clipboard transfer hardening tests (pure + runtime helper).

Covers TransferJob state, disk-space checks, retry/resume helpers, queue
serialization and the unified progress shape exposed by ClipboardManager.
"""
from __future__ import annotations

import os
import json
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
zero_job = ct.make_transfer_job("zero", "p1", "zero-item", "send", cm.KIND_FILE,
                                "empty", 0)
check(zero_job.status == ct.TransferStatus.pending,
      "zero-byte job requires explicit transfer completion")
ct.mark_completed(zero_job)
check(zero_job.status == ct.TransferStatus.completed,
      "zero-byte job supports explicit completion")
full_progress_job = ct.make_transfer_job(
    "full-progress", "p1", "full-progress-item", "receive", cm.KIND_BINARY, "full", 1,
    status=ct.TransferStatus.running)
ct.update_progress(full_progress_job, received_bytes=1)
check(full_progress_job.status == ct.TransferStatus.running,
      "byte progress alone does not complete a transfer")
ct.mark_completed(full_progress_job)


# ── V2 transfer session foundation ──────────────────────────────────
session = ct.TransferSession(
    transfer_id="session-1",
    direction="send",
    item_id="item-session-1",
    item_revision=2,
    profile="device-peer-a",
    peer_identity="device:peer-a",
    provider="device:local",
    strategy=ct.STREAM_V2_STRATEGY,
    manifest_digest="a" * 64,
    logical_bytes=400,
    remaining_bytes=400,
    file_count=2,
    progress={"current_file": "one.bin"},
    resume_state={"durable_bytes": 0},
    preflight_state={"status": "pending"},
)
initial_session = session.snapshot()
check(initial_session["state"] == ct.TransferSessionState.created,
      "session starts in created state")
check(session.item_id == "item-session-1" and session.strategy == ct.STREAM_V2_STRATEGY
      and session.logical_bytes == 400 and session.file_count == 2,
      "session exposes required immutable fields")
check(initial_session["progress"]["percent"] == 0.0,
      "session snapshot has concrete initial progress")
initial_session["resume_state"]["durable_bytes"] = 999
check(session.snapshot()["resume_state"]["durable_bytes"] == 0,
      "session snapshots do not expose mutable state")

session.transition(ct.TransferSessionState.preflight)
session.update_progress(preflight_state={"status": "accepted"})
session.transition(ct.TransferSessionState.accepted)
session.transition(ct.TransferSessionState.sending_manifest)
session.transition(ct.TransferSessionState.transferring)
session.advance_progress(100, progress={"current_file": "one.bin"},
                         resume_state={"durable_bytes": 100})
progress_session = session.snapshot()
check(progress_session["remaining_bytes"] == 300
      and progress_session["progress"]["transferred_bytes"] == 100
      and progress_session["progress"]["percent"] == 25.0,
      "session progress updates remaining bytes atomically")
session.transition(ct.TransferSessionState.paused)
session.transition(ct.TransferSessionState.waiting_reconnect)
session.increment_retry(error={"code": "disconnect", "message": "peer disconnected",
                               "retryable": True})
session.transition(ct.TransferSessionState.transferring)
session.advance_progress(300)
session.transition(ct.TransferSessionState.verifying)
session.transition(ct.TransferSessionState.finalizing)
session.transition(ct.TransferSessionState.completed)
check(session.snapshot()["state"] == ct.TransferSessionState.completed
      and session.snapshot()["remaining_bytes"] == 0
      and session.snapshot()["retry_count"] == 1,
      "session reaches a concrete completed end state")
try:
    session.transition(ct.TransferSessionState.transferring)
    terminal_reactivation_rejected = False
except ValueError:
    terminal_reactivation_rejected = True
check(terminal_reactivation_rejected, "terminal session cannot reactivate")

invalid_transition_session = ct.TransferSession(
    transfer_id="session-invalid-transition", direction="send", item_id="invalid-transition-item",
    item_revision=0, profile="profile-a", peer_identity="device:peer-a",
    provider="device:local", strategy=ct.STREAM_V2_STRATEGY,
    manifest_digest="e" * 64, logical_bytes=1, file_count=1,
)
try:
    invalid_transition_session.transition(ct.TransferSessionState.transferring)
    invalid_transition_rejected = False
except ValueError:
    invalid_transition_rejected = True
check(invalid_transition_rejected, "session rejects skipped lifecycle transitions")

cancel_session = ct.TransferSession(
    transfer_id="session-cancel", direction="receive", item_id="cancel-item",
    item_revision=0, profile="device-peer-b", peer_identity="device:peer-b",
    provider="device:peer-b", strategy=ct.LEGACY_ZIP_V1_STRATEGY,
    manifest_digest="b" * 64, logical_bytes=10, file_count=1,
)
cancel_session.cancel(error={"code": "user_cancelled", "message": "cancelled by user",
                             "retryable": False})
check(cancel_session.snapshot()["state"] == ct.TransferSessionState.cancelled
      and cancel_session.snapshot()["error"]["code"] == "user_cancelled",
      "session cancellation works from any nonterminal state with structured error")

nonterminal_states = [state for state in ct.TRANSFER_SESSION_STATES
                      if state not in ct.TERMINAL_TRANSFER_SESSION_STATES]
cancelled_states = []
for index, state in enumerate(nonterminal_states):
    state_session = ct.TransferSession(
        transfer_id=f"cancel-state-{index}", direction="send", item_id=f"cancel-item-{index}",
        item_revision=0, profile="profile-a", peer_identity="device:peer-a",
        provider="device:local", strategy=ct.STREAM_V2_STRATEGY,
        manifest_digest="f" * 64, logical_bytes=1, file_count=1, state=state,
    )
    state_session.cancel()
    cancelled_states.append(state_session.state)
check(cancelled_states == [ct.TransferSessionState.cancelled] * len(nonterminal_states),
      "session cancellation is valid from every nonterminal state")

invalid_sessions = (
    {"transfer_id": "bad id"},
    {"direction": "sideways"},
    {"item_revision": True},
    {"strategy": "automatic"},
    {"manifest_digest": "short"},
    {"remaining_bytes": 401},
    {"file_count": -1},
    {"progress": "not-structured"},
    {"resume_state": {"durable_bytes": -1}},
)
valid_session_args = {
    "transfer_id": "valid-session", "direction": "send", "item_id": "valid-item",
    "item_revision": 0, "profile": "profile-a", "peer_identity": "device:peer-a",
    "provider": "device:local", "strategy": ct.STREAM_V2_STRATEGY,
    "manifest_digest": "c" * 64, "logical_bytes": 400, "remaining_bytes": 400,
    "file_count": 1,
}
for invalid_fields in invalid_sessions:
    try:
        ct.TransferSession(**dict(valid_session_args, **invalid_fields))
        invalid_session_rejected = False
    except ValueError:
        invalid_session_rejected = True
    check(invalid_session_rejected,
          f"session validates {next(iter(invalid_fields))}")

concurrent_session = ct.TransferSession(
    transfer_id="session-concurrent", direction="receive", item_id="concurrent-item",
    item_revision=1, profile="profile-c", peer_identity="device:peer-c",
    provider="device:peer-c", strategy=ct.STREAM_V2_STRATEGY,
    manifest_digest="d" * 64, logical_bytes=4000, file_count=4,
)
concurrent_session.transition(ct.TransferSessionState.preflight)
concurrent_session.transition(ct.TransferSessionState.accepted)
concurrent_session.transition(ct.TransferSessionState.sending_manifest)
concurrent_session.transition(ct.TransferSessionState.transferring)
concurrent_errors = []
concurrent_stop = threading.Event()


def advance_session_progress():
    try:
        for _ in range(1000):
            concurrent_session.advance_progress(1)
    except Exception as exc:
        concurrent_errors.append(exc)


def poll_session_progress():
    try:
        while not concurrent_stop.is_set():
            current = concurrent_session.snapshot()
            transferred = current["progress"]["transferred_bytes"]
            if current["remaining_bytes"] + transferred != current["logical_bytes"]:
                raise AssertionError("torn transfer session snapshot")
    except Exception as exc:
        concurrent_errors.append(exc)


progress_threads = [threading.Thread(target=advance_session_progress) for _ in range(4)]
snapshot_thread = threading.Thread(target=poll_session_progress)
snapshot_thread.start()
for progress_thread in progress_threads:
    progress_thread.start()
for progress_thread in progress_threads:
    progress_thread.join(3.0)
concurrent_stop.set()
snapshot_thread.join(3.0)
concurrent_snapshot = concurrent_session.snapshot()
check(not concurrent_errors and all(not thread.is_alive() for thread in progress_threads)
      and not snapshot_thread.is_alive()
      and concurrent_snapshot["remaining_bytes"] == 0
      and concurrent_snapshot["progress"]["transferred_bytes"] == 4000,
      "session progress and snapshots are synchronized across concurrent threads")


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
check(not queue._jobs and not queue._tasks,
      "queue retires completed and cancelled job references")
check(queue.shutdown(timeout=1.0) is True, "queue shutdown joins worker")
check(queue.activity_snapshot()["workers_alive"] == 0, "queue worker stopped")
rejected = ct.make_transfer_job("qc", "p1", "c", "send", cm.KIND_TEXT, "C", 10)
check(queue.submit(rejected, lambda current: None) is False,
      "queue rejects submissions after shutdown")

full_queue = ct.TransferQueue(max_parallel=1, maxsize=1, retry_delay_ms=20)
full_started = threading.Event()
full_release = threading.Event()
full_active = ct.make_transfer_job("full-active", "p1", "fa", "send",
                                   cm.KIND_TEXT, "FA", 1)
full_queued = ct.make_transfer_job("full-queued", "p1", "fq", "send",
                                   cm.KIND_TEXT, "FQ", 1)
full_rejected = ct.make_transfer_job("full-rejected", "p1", "fr", "send",
                                     cm.KIND_TEXT, "FR", 1)


def hold_full_queue(current):
    full_started.set()
    full_release.wait(2.0)


check(full_queue.submit(full_active, hold_full_queue), "bounded queue accepts active job")
check(full_started.wait(1.0), "bounded queue active job starts")
check(full_queue.submit(full_queued, lambda current: None),
      "bounded queue accepts one queued job")
check(not full_queue.submit(full_rejected, lambda current: None),
      "bounded queue rejects overflow")
check(full_rejected.transfer_id not in full_queue._jobs
      and full_rejected.transfer_id not in full_queue._tasks,
      "rejected queue job retains no references")
duplicate = ct.make_transfer_job("full-queued", "p1", "duplicate", "send",
                                 cm.KIND_TEXT, "duplicate", 1)
check(not full_queue.submit(duplicate, lambda current: None),
      "bounded queue rejects duplicate transfer IDs")
full_release.set()
check(wait_until(lambda: not full_queue._jobs),
      "bounded queue retires all terminal jobs")
check(full_queue.shutdown(timeout=1.0), "bounded queue shuts down")

cancel_wait_queue = ct.TransferQueue(max_parallel=1, maxsize=1, retry_delay_ms=20)
cancel_wait_started = threading.Event()
cancel_wait_release = threading.Event()
cancel_wait_active = ct.make_transfer_job("cancel-wait-active", "p1", "cwa", "send",
                                          cm.KIND_TEXT, "CWA", 1)
cancel_wait_queued = ct.make_transfer_job("cancel-wait-queued", "p1", "cwq", "send",
                                          cm.KIND_TEXT, "CWQ", 1)
cancel_wait_blocked = ct.make_transfer_job("cancel-wait-blocked", "p1", "cwb", "send",
                                           cm.KIND_TEXT, "CWB", 1)


def hold_cancel_wait_queue(current):
    cancel_wait_started.set()
    cancel_wait_release.wait(2.0)


cancel_wait_queue.submit(cancel_wait_active, hold_cancel_wait_queue)
check(cancel_wait_started.wait(1.0), "cancel-wait active job starts")
cancel_wait_queue.submit(cancel_wait_queued, lambda current: None)
cancel_wait_result = []
cancel_wait_thread = threading.Thread(target=lambda: cancel_wait_result.append(
    cancel_wait_queue.submit(cancel_wait_blocked, lambda current: None,
                             block=True, timeout=0.05)))
cancel_wait_thread.start()
cancel_wait_thread.join(1.0)
check(cancel_wait_result == [False]
      and cancel_wait_queue.get_job("cancel-wait-blocked") is None,
      "blocked submit timeout retains no job or closure")
cancel_wait_release.set()
check(wait_until(lambda: not cancel_wait_queue._jobs),
      "cancel-wait queue retires all jobs")
check(cancel_wait_queue.shutdown(timeout=1.0), "cancel-wait queue shuts down")


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

cancel_retry_queue = ct.TransferQueue(max_parallel=1, retry_delay_ms=200)
cancel_retry_job = ct.make_transfer_job(
    "cancel-retry", "p1", "cancel-retry", "send", cm.KIND_TEXT,
    "cancel-retry", 1, max_retries=2)
cancel_retry_calls = []


def cancel_retry_work(current):
    cancel_retry_calls.append(current.retry_count)
    raise RuntimeError("cancel this retry")


check(cancel_retry_queue.submit(cancel_retry_job, cancel_retry_work),
      "cancel-retry queue accepts work")
check(wait_until(lambda: cancel_retry_queue.activity_snapshot()["retry_pending"] == 1),
      "cancel-retry enters pending state")
check(cancel_retry_queue.cancel(cancel_retry_job.transfer_id),
      "retry-pending transfer can be cancelled")
time.sleep(0.25)
check(cancel_retry_calls == [0] and cancel_retry_queue.activity_snapshot()["queued"] == 0,
      "cancelled retry timer cannot enqueue stale work")
check(cancel_retry_queue.shutdown(timeout=1.0), "cancel-retry queue shuts down")

cancel_race_queue = ct.TransferQueue(max_parallel=1, retry_delay_ms=0)
cancel_race_job = ct.make_transfer_job(
    "cancel-race", "p1", "cancel-race", "send", cm.KIND_TEXT,
    "cancel-race", 1, max_retries=2)
cancel_race_started = threading.Event()
cancel_race_release = threading.Event()
cancel_race_calls = []


def cancel_race_work(current):
    cancel_race_calls.append(current.retry_count)
    cancel_race_started.set()
    cancel_race_release.wait(1)
    raise RuntimeError("cancelled during failure")


cancel_race_queue.submit(cancel_race_job, cancel_race_work)
check(cancel_race_started.wait(1), "cancel-race work starts")
check(cancel_race_queue.cancel(cancel_race_job.transfer_id),
      "active transfer can be cancelled before exception")
cancel_race_release.set()
time.sleep(0.1)
check(cancel_race_calls == [0] and cancel_race_job.status == ct.TransferStatus.cancelled,
      "active cancellation cannot be overwritten by retry")
check(cancel_race_queue.shutdown(timeout=1.0), "cancel-race queue shuts down")

resume_handoff_queue = ct.TransferQueue(max_parallel=1, maxsize=1, retry_delay_ms=0)
resume_old = ct.make_transfer_job(
    "resume-handoff", "p1", "resume-handoff", "send", cm.KIND_TEXT,
    "resume-old", 1)
resume_new = ct.make_transfer_job(
    "resume-handoff", "p1", "resume-handoff", "send", cm.KIND_TEXT,
    "resume-new", 1)
resume_old_started = threading.Event()
resume_old_release = threading.Event()
resume_new_ran = threading.Event()
resume_submit_result = []


def resume_old_work(current):
    resume_old_started.set()
    resume_old_release.wait(1)


resume_handoff_queue.submit(resume_old, resume_old_work)
check(resume_old_started.wait(1), "resume handoff old attempt starts")
check(resume_handoff_queue.cancel(resume_old.transfer_id),
      "resume handoff cancels old active attempt")
resume_submit_thread = threading.Thread(target=lambda: resume_submit_result.append(
    resume_handoff_queue.submit(
        resume_new, lambda current: resume_new_ran.set(), block=True, timeout=1)))
resume_submit_thread.start()
time.sleep(0.03)
check(not resume_new_ran.is_set(), "resume replacement waits for old attempt release")
resume_old_release.set()
resume_submit_thread.join(1)
check(resume_submit_result == [True] and resume_new_ran.wait(1),
      "resume replacement with same transfer ID runs after handoff")
check(wait_until(lambda: resume_new.status == ct.TransferStatus.completed),
      "resume replacement reaches completed state")
check(resume_handoff_queue.shutdown(timeout=1.0), "resume handoff queue shuts down")

parallel_retry_queue = ct.TransferQueue(max_parallel=2, retry_delay_ms=0)
parallel_retry_job = ct.make_transfer_job(
    "parallel-retry", "p1", "parallel-retry", "send", cm.KIND_TEXT,
    "parallel-retry", 1, max_retries=2)
parallel_retry_calls = []
parallel_retry_lock = threading.Lock()
parallel_retry_registry = []


def fail_then_succeed_without_overlap(current):
    with parallel_retry_lock:
        parallel_retry_calls.append(current.retry_count)
        parallel_retry_registry.append(
            parallel_retry_queue.get_job(current.transfer_id) is current)
    if current.retry_count == 0:
        raise RuntimeError("retry once")


check(parallel_retry_queue.submit(parallel_retry_job, fail_then_succeed_without_overlap),
      "parallel retry queue accepts work")
check(wait_until(lambda: parallel_retry_job.status == ct.TransferStatus.completed),
      "parallel retry completes after one retry")
check(parallel_retry_calls == [0, 1] and parallel_retry_registry == [True, True],
      "retry keeps one valid registry entry across worker attempts")
check(wait_until(lambda: not parallel_retry_queue._jobs),
      "parallel retry retains no terminal registry entry")
check(parallel_retry_queue.shutdown(timeout=1.0), "parallel retry queue shuts down")


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
    mgr._register_job(running_job)

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
    running_progress = snap[running_item["item_id"]]
    check(running_progress["transfer_id"] == "rt1"
          and running_progress["strategy"] == ct.LEGACY_ZIP_V1_STRATEGY,
          "runtime progress exposes transfer id and selected strategy")

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


# ── productive session persistence / restore / terminal synchronization ─
tmp = tempfile.mkdtemp(prefix="fs_clip_session_rt_")
try:
    mgr = ClipboardManager(tmp, "dev", lambda ident, msg: None, lambda: settings)
    st = mgr.store("device:A")
    item = cm.make_text_item("persistent", seq=1)
    st.add_item(item, data=b"persistent")
    runtime_job = mgr._make_job_from_item("device:A", item, "send",
                                          status=ct.TransferStatus.running)
    ct.advance_job_session(runtime_job, ct.TransferSessionState.transferring)
    ct.update_progress(runtime_job, sent_bytes=4, status=ct.TransferStatus.running)
    persisted = st.transfer_sessions_snapshot()[runtime_job.transfer_id]
    check(runtime_job.session is not None
          and runtime_job.session.remaining_bytes == item["size"] - 4
          and persisted["state"] == ct.TransferSessionState.transferring,
          "runtime job keeps current progress while persistence is checkpointed")
    reopened = ClipboardManager(tmp, "dev", lambda ident, msg: None, lambda: settings)
    reopened.store("device:A")
    restored = reopened._jobs[runtime_job.transfer_id]
    check(restored.session.state == ct.TransferSessionState.failed
          and restored.status == ct.TransferStatus.failed
          and restored.session.error["code"] == "restart_without_resume_journal",
          "manager reconciles nonterminal snapshots to a restart failure")
    ct.mark_completed(runtime_job)
    check(st.transfer_sessions_snapshot()[runtime_job.transfer_id]["state"]
          == ct.TransferSessionState.completed,
          "legacy completion synchronizes the session terminal state")
    cancelled = mgr._make_job_from_item("device:A", item, "receive",
                                        status=ct.TransferStatus.running)
    ct.mark_cancelled(cancelled, "user cancelled")
    check(st.transfer_sessions_snapshot()[cancelled.transfer_id]["state"]
          == ct.TransferSessionState.cancelled,
          "legacy cancellation synchronizes the session terminal state")
    index_path = st.index_path
    document = json.loads(open(index_path, encoding="utf-8").read())
    document["transfer_sessions"]["malformed"] = {"transfer_id": "malformed"}
    with open(index_path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    safe = ClipboardStore(tmp, st.profile_id)
    check(len(safe.list_items()) == 1 and "malformed" not in safe.transfer_sessions_snapshot(),
          "malformed session snapshot cannot corrupt item history")
    for index in range(300):
        extra = mgr._make_job_from_item("device:A", item, "send",
                                        transfer_id=f"bounded-{index}")
        ct.mark_cancelled(extra)
    check(len(mgr._jobs) == 256
          and len(st.transfer_sessions_snapshot()) == 256,
          "runtime and persisted session registries are bounded")
    removable = mgr._make_job_from_item("device:A", item, "send",
                                        transfer_id="remove-with-item")
    check("remove-with-item" in st.transfer_sessions_snapshot(),
          "item transfer session exists before lifecycle deletion")
    check(mgr.delete_item("device:A", item["item_id"])
          and "remove-with-item" not in st.transfer_sessions_snapshot()
          and item["item_id"] not in mgr._jobs_by_item,
          "item deletion removes persisted and runtime transfer sessions")
    clear_item = cm.make_text_item("clear sessions", seq=2)
    st.add_item(clear_item, data=b"clear sessions")
    clear_job = mgr._make_job_from_item("device:A", clear_item, "send",
                                        transfer_id="remove-with-clear")
    mgr._make_job_from_item("device:A", clear_item, "send",
                            transfer_id="clear-lifecycle")
    check(mgr.clear("device:A") and not st.transfer_sessions_snapshot()
          and clear_item["item_id"] not in mgr._jobs_by_item,
          "clear removes persisted and runtime transfer sessions")
    reopened.shutdown(timeout=1.0)
    mgr.shutdown(timeout=1.0)
finally:
    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass


# ── bounded progress persistence and best-effort save failures ──────
persisted_snapshots = []
bounded_session = ct.TransferSession(
    transfer_id="bounded-progress", direction="send", item_id="bounded-progress-item",
    item_revision=0, profile="profile-a", peer_identity="device:peer-a",
    provider="device:local", strategy=ct.LEGACY_ZIP_V1_STRATEGY,
    manifest_digest="a" * 64, logical_bytes=2048, file_count=1,
)
bounded_job = ct.make_transfer_job(
    "bounded-progress", "profile-a", "bounded-progress-item", "send", cm.KIND_BINARY,
    "bounded.bin", 2048, chunk_count=2048, status=ct.TransferStatus.running)
bounded_job.session = bounded_session
bounded_job.session_persist = lambda snapshot: persisted_snapshots.append(snapshot)
ct.advance_job_session(bounded_job, ct.TransferSessionState.transferring)
bounded_job.session_last_persisted_at -= 1.0
ct.update_progress(bounded_job, sent_bytes=1500, completed_chunks=range(1500),
                   missing_chunks=range(1500, 2048), status=ct.TransferStatus.running)
bounded_progress = persisted_snapshots[-1]["progress"]
check("completed_chunks" not in bounded_progress
      and bounded_progress["completed_chunk_count"] == 1500
      and bounded_progress["next_chunk_index"] == 1500,
      "session persistence stores bounded cumulative progress above 1024 chunks")

failure_queue = ct.TransferQueue(max_parallel=1, retry_delay_ms=10)
failure_session = ct.TransferSession(
    transfer_id="save-failure", direction="send", item_id="save-failure-item",
    item_revision=0, profile="profile-a", peer_identity="device:peer-a",
    provider="device:local", strategy=ct.LEGACY_ZIP_V1_STRATEGY,
    manifest_digest="b" * 64, logical_bytes=1, file_count=1,
)
failure_job = ct.make_transfer_job(
    "save-failure", "profile-a", "save-failure-item", "send", cm.KIND_BINARY,
    "failure.bin", 1)
failure_job.session = failure_session
failure_job.session_persist = lambda _snapshot: (_ for _ in ()).throw(OSError("save failed"))


def successful_payload_despite_save_failure(current):
    ct.update_progress(current, sent_bytes=1)


check(failure_queue.submit(failure_job, successful_payload_despite_save_failure),
      "queue accepts payload with injected session save failure")
check(wait_until(lambda: failure_job.status == ct.TransferStatus.completed)
      and failure_job.session.state == ct.TransferSessionState.completed
      and failure_job.persistence_failures > 0
      and failure_queue.activity_snapshot()["active"] == 0,
      "session save failure does not kill worker or change successful result semantics")
check(failure_queue.shutdown(timeout=1.0), "save-failure worker remains joinable")

awaiting_queue = ct.TransferQueue(max_parallel=1, retry_delay_ms=10)
awaiting_job = ct.make_transfer_job(
    "awaiting-ack", "profile-a", "awaiting-item", "send", cm.KIND_BINARY,
    "awaiting.bin", 1)


def wait_for_receiver_ack(current):
    ct.update_progress(current, sent_bytes=1, status=ct.TransferStatus.awaiting_ack)


check(awaiting_queue.submit(awaiting_job, wait_for_receiver_ack),
      "queue accepts sender awaiting receiver finalization")
check(wait_until(lambda: awaiting_job.status == ct.TransferStatus.failed)
      and wait_until(lambda: awaiting_queue.activity_snapshot()["active"] == 0),
      "queue fails a worker that returns before receiver ACK")
check(awaiting_queue.shutdown(timeout=1.0), "awaiting-ACK queue remains joinable")

# Nonterminal chunk progress is checkpointed instead of fsyncing every chunk.
checkpoint_saves = []
checkpoint_session = ct.TransferSession(
    transfer_id="checkpointed", direction="receive", item_id="checkpointed-item",
    item_revision=0, profile="profile-a", peer_identity="device:peer-a",
    provider="device:peer-a", strategy=ct.LEGACY_ZIP_V1_STRATEGY,
    manifest_digest="c" * 64, logical_bytes=32 * 1024 * 1024, file_count=1)
checkpoint_job = ct.make_transfer_job(
    "checkpointed", "profile-a", "checkpointed-item", "receive", cm.KIND_BINARY,
    "checkpoint.bin", checkpoint_session.logical_bytes, chunk_count=512,
    status=ct.TransferStatus.running)
checkpoint_job.session = checkpoint_session
checkpoint_job.session_persist = lambda snapshot: checkpoint_saves.append(snapshot)
ct.advance_job_session(checkpoint_job, ct.TransferSessionState.transferring)
for index in range(512):
    ct.update_progress(
        checkpoint_job, received_bytes=(index + 1) * 64 * 1024,
        completed_chunk_count=index + 1, missing_chunk_count=511 - index,
        next_chunk_index=index + 1)
ct.mark_completed(checkpoint_job)
check(len(checkpoint_saves) < 20,
      "many chunks produce bounded session checkpoint saves")
check(checkpoint_saves[-1]["state"] == ct.TransferSessionState.completed
      and checkpoint_saves[-1]["remaining_bytes"] == 0,
      "terminal session state is always persisted")


print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard transfer tests passed.")
