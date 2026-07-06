# FlowShift — Manual Test Checklist

## Automated checks first (run on any machine before a manual pass)

```
python -m py_compile src/python/tray.py src/python/gui.py \
    src/python/runtime_model.py src/python/e2e_test.py \
    src/python/test_service.py src/python/reconnect_stress_test.py \
    src/python/keymap.py src/python/input_events.py \
    src/python/platform_capabilities.py src/python/version.py \
    src/python/elevated_task.py src/python/live_network_test.py \
    src/python/input_backends/*.py
python src/python/test_service.py          # 128 pure-logic checks (any OS)
python src/python/e2e_test.py              # runtime handshake + input (Windows; skip on non-Win)
python src/python/reconnect_stress_test.py 30  # 30 reconnect rounds + clean shutdown
```

---

## Runtime lifecycle

- [ ] `python src/python/tray.py --tray` — tray icon appears, no CMD popup.
- [ ] Control socket answers `status` on `127.0.0.1:45782`.
- [ ] Start from GUI twice: second start does NOT create a second runtime (singleton mutex).
- [ ] Stop from GUI: state `stopping → stopped` only after control socket is gone.
- [ ] Restart (stop → wait → start) comes back to `running`.
- [ ] Repeat Start/Stop 20×: no zombie processes (`Get-NetTCPConnection -LocalPort 45781,45782`).
- [ ] UAC: no prompt on normal start. Elevated path via "Elevated Runtime installieren" (one UAC, then none).

---

## Tray icon behaviour (commit e137af8)

- [ ] **Single left-click**: does nothing (no profile activation).
- [ ] **Double left-click**: opens settings GUI.
- [ ] **Right-click**: shows menu (Forwarding start/stop, Settings, Auto-start, Exit).
- [ ] Tooltip when **no profile active**: `FlowShift` (just the name, nothing else).
- [ ] Tooltip when **profile active** (e.g. Laptop → Surface): `FlowShift | Laptop → Surface`.

---

## Profile rows & direction labels (commit e137af8)

- [ ] With runtime running but **no profile active**: profile rows show peer names with
  **no direction label, no "verbunden", no "offline"** — columns are empty.
- [ ] After **activating Laptop → Surface**: Laptop's profile row shows `Laptop → Surface` / Quelle.
- [ ] After activation: **Surface's profile row** also shows `Laptop → Surface` / Ziel
  (via `fwd_state` protocol message). Verify by opening GUI on Surface.
- [ ] After **deactivating**: both profile rows clear back to empty.
- [ ] **Forwarding-Toggle-Button** text: inactive → `Forwarding starten → Surface-Viktor`;
  active → `Forwarding stoppen (Surface-Viktor)`.
- [ ] With profile A active: "Aktivieren" button for all other peers is **disabled**
  (prevents circular forwarding without first deactivating).
- [ ] Status panel shows separate lines: Netzwerk / Forwarding / Capture — never misleads.
- [ ] **"Status aktualisieren"** button updates immediately (polling loop runs every 1 s anyway).

---

## Mouse movement (commit e137af8 — new delta-based forwarding)

- [ ] Activate profile Laptop → Surface. Move mouse on Laptop.
- [ ] Surface cursor moves proportionally (not frozen, not jumping back to start position).
- [ ] Cursor stays suppressed on Laptop (Laptop apps do not receive cursor moves).
- [ ] Move to screen edges: cursor stops at edge on Surface (clamped correctly).
- [ ] Deactivate: Laptop cursor unfreezes.
- [ ] Repeat after a Laptop runtime restart: anchor is re-primed on each activation.

---

## Keyboard forwarding

- [ ] Individual keys (letters, digits, function keys) forwarded and typed on Surface.
- [ ] **Modifier combos**: hold Shift on Laptop, press arrow → text selected on Surface.
  Shift+Ctrl+Arrow selects a word. (Code analysis: modifier VKs forwarded correctly.)
- [ ] `type_text` via GUI Live Test: multi-line text with Enter and Tab lands correctly.
- [ ] No keys stuck after deactivate (synthetic key_up sent to Surface).
- [ ] Kill switch `Ctrl+Alt+Shift+Win+F12`: stops forwarding and quits runtime.

---

## Peer discovery & reconnect

- [ ] Peer auto-discovered via UDP broadcast (no manual IP needed if on same LAN).
- [ ] Ping Laptop → Surface: RTT shown in GUI log, Surface logs `ping received` / `pong sent`.
- [ ] Disconnect Surface: GUI shows no active connection (no stale "verbunden" labels).
- [ ] Reconnect Surface (without restarting Laptop runtime): peer returns, profile activatable.
- [ ] Edit peer host/port while runtime runs: connector restarts with new address
  (log: `address changed … restarting connector`).

---

## Circular forwarding (commit e137af8)

- [ ] Laptop activates → Surface. In Surface GUI, try to activate → Laptop:
  "Aktivieren" button is **disabled** as long as Surface's own profile is inactive.
  (If Surface has no active profile, its button is enabled.)
- [ ] Activate Surface → Laptop while Laptop → Surface is active: injected events on
  Surface carry `INJECTED_EXTRA_INFO` so the Surface hook does NOT forward them back.
  No forwarding loop occurs (verified by hook filter: `LLMHF_INJECTED`).

---

## Hotkeys

- [ ] Default hotkeys: Ctrl+Alt+1 forward, Ctrl+Alt+0 return. Both fire correctly.
- [ ] Delete peer A: peer B hotkey still works. Peer A hotkey shows "(ungültig)".
- [ ] Rename B: hotkey still targets B (identity-stable).
- [ ] Change hotkey in GUI while running: new combo works immediately (re-registered via `WM_RELOAD_HOTKEYS`).
- [ ] Invalid hotkey (`key == 0` or unresolved target): skipped at registration, log shows warning.

---

## Input cleanup (no stuck keys/buttons)

- [ ] Hold a key while forwarding active, press Return-to-local: no key stuck on Surface.
- [ ] Pull network cable while forwarding: keys released within 1 s (disconnect triggers cleanup).
- [ ] Hold mouse button, deactivate: mouseup sent to Surface.

---

## Live Test tab

- [ ] Both devices on same Git commit: button enabled. Different commits: button disabled.
- [ ] Override checkbox bypasses version gate.
- [ ] `Live Test starten` activates → mouse moves → click → text typed → deactivates.
  Verify on Surface that text appeared.

---

## Logging

- [ ] `flowshift.log` is written (file logging confirmed).
- [ ] Mouse-move and `status`-poll logs are rate-limited (no flooding).
- [ ] Shutdown logged once; all loops stop cleanly.
- [ ] GUI log shows `fwd_state` notifications when remote activates/deactivates.
