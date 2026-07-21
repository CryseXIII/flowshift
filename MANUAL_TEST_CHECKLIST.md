# FlowShift — Manual Test Checklist

## Automated checks first (run on any machine before a manual pass)

```
python -m py_compile src/python/tray.py src/python/gui.py \
    src/python/clipboard_win.py src/python/clipboard_html.py \
    src/python/runtime_model.py src/python/e2e_test.py \
    src/python/test_service.py src/python/test_clipboard_html.py \
    src/python/reconnect_stress_test.py src/python/keymap.py src/python/input_events.py \
    src/python/platform_capabilities.py src/python/version.py \
    src/python/elevated_task.py src/python/live_network_test.py \
    src/python/flowshift_diagnostics.py src/python/flowshift_diagnose.py \
    src/python/test_diagnostics.py src/python/test_clipboard_live_test.py \
    src/python/input_backends/*.py
python src/python/test_service.py          # 166 pure-logic checks (any OS)
python src/python/test_clipboard.py        # 72 clipboard foundation checks (any OS)
python src/python/test_clipboard_html.py    # HTML clipboard checks (any OS; Windows APIs stubbed off-Win)
python src/python/test_clipboard_gif.py     # animated GIF preview checks (any OS; skips cleanly if Pillow missing)
python src/python/test_clipboard_sync.py   # 14 two-manager text-sync checks (any OS)
python src/python/test_diagnostics.py      # local diagnostics report checks (any OS)
python src/python/test_clipboard_live_test.py  # live-test helper checks (any OS)
python src/python/e2e_test.py              # runtime handshake + input (Windows; skip on non-Win)
python src/python/reconnect_stress_test.py 30  # 30 reconnect rounds (Windows; skips off-Win)
python src/python/worker_smoke_test.py     # workers + forwarding + flying switch + clipboard (Windows)
```

> The worker smoke test catches silent worker crashes (e.g. a missing import in
> `forward_loop`): it starts a real runtime, checks `forward_loop`/`inject_loop`
> are alive, and verifies a fake peer actually RECEIVES forwarded input.

> Installer / uninstaller manual tests: see `docs/install_test_checklist.md`.

## Edge Switching WebGUI

### Test 1: Display Settings speichern
- WebGUI oeffnen.
- Display Settings oeffnen.
- Edge Switching aktivieren.
- East edge -> Surface setzen.
- Threshold, Inset, Cooldown setzen.
- Speichern.
- Browser neu laden.
- Erwartung: Einstellungen bleiben sichtbar.

### Test 2: A -> B ueber rechte Kante
- Runtime A und B starten.
- A WebGUI zeigt B als Peer.
- Maus auf A ueber rechte Kante bewegen.
- Erwartung:
  - A loggt edge trigger east.
  - A verbindet zu B, falls noch nicht verbunden.
  - A sendet edge_enter.
  - B setzt Cursor links.
  - A aktiviert Forwarding erst nach edge_enter_ack.
  - Maus/Tastatur funktionieren auf B.

### Test 3: B -> A zurueck ueber linke Kante
- Auf B Maus ueber linke Kante zurueckfuehren.
- Erwartung:
  - B sendet edge_return.
  - A deaktiviert Forwarding.
  - A setzt Cursor nahe rechter Kante.
  - Beide Sessions sind beendet.

### Test 4: Wiederholung
- Test 2 und 3 zehnmal wiederholen.
- Kein Flackern.
- Keine haengenden Tasten.
- Kein Zombie-Prozess.
- Logs bleiben lesbar.

### Test 5: Peer nicht verbunden
- Runtime A starten.
- Runtime B starten, aber Verbindung vorher nicht manuell aktivieren.
- Maus A ueber Edge.
- Erwartung:
  - A verbindet automatisch zu B.
  - Edge-Session startet erst nach Verbindung und Ack.

### Test 6: Disconnect waehrend Edge-Session
- Waehrend A -> B aktiv ist, B Runtime stoppen oder Netzwerk trennen.
- Erwartung:
  - A deaktiviert Forwarding.
  - A behaelt lokale Kontrolle.
  - Keine Taste/Mausbutton haengt.
  - Session wird gecancelt.

## Phase 5A — Windows Live Clipboard Tests

1. Start the runtime with `python src/python/tray.py --tray`. PASS: control socket answers; FAIL: no `status` reply; log: `src/python/flowshift.log`.
2. Run `python src/python/flowshift_diagnose.py`. PASS: readable report shows version, runtime, clipboard, storage, dependencies, worker and problems sections; FAIL: report missing or unreadable; logs: `src/python/flowshift.log`, `src/python/flowshift_runtime.out`.
3. Check the report fields. PASS: clipboard root/temp/object dirs, Pillow, free disk, limits and runtime health are present; FAIL: fields missing or obviously wrong; logs: `src/python/flowshift.log`.
4. Run `python src/python/clipboard_live_test.py --quick --yes`. PASS: `TEXT PASS`, `HTML PASS`, `IMAGE PASS`, `FILES PASS`, `FILE_BATCH PASS`; FAIL: any line prints `FAIL`; logs: `src/python/flowshift.log`.
5. Run `python src/python/clipboard_live_test.py --with-runtime --yes`. PASS: it prints `RUNTIME REACHABLE` or `RUNTIME NOT REACHABLE` and still completes the clipboard tests; FAIL: clipboard tests abort early; logs: `src/python/flowshift.log`, `src/python/flowshift_runtime.out`.
6. Run `python src/python/clipboard_live_test.py --large-files 150 --file-size-mb 1 --yes` only when explicitly testing the large path. PASS: `LARGE_FILE_BATCH PASS`; FAIL: file batch fails or hangs; logs: `src/python/flowshift.log`.
7. Run `python src/python/clipboard_live_test.py --out "%TEMP%\FlowShiftLiveTest" --keep-files --yes` when you want to inspect the generated files. PASS: files remain in the chosen output directory; FAIL: files disappear or the run errors; logs: `src/python/flowshift.log`.
8. In the GUI, open the Clipboard window with `Ctrl+Alt+V`. PASS: the window opens; FAIL: nothing opens; logs: `src/python/flowshift.log`.
9. Test `Win+V` only if `intercept_win_v=true`. PASS: FlowShift opens the clipboard window instead of the OS history; FAIL: the OS history opens or the shortcut does nothing; logs: `src/python/flowshift.log`.
10. Two-device check Laptop -> Surface. PASS: copied Text, HTML, Image, 10 Files and 150 small files appear on the target and can be pasted; FAIL: items missing, progress stuck, or paste fails; logs: `src/python/flowshift.log`, `src/python/flowshift_runtime.out`.
11. Review logs after the run. PASS: no worker crashes, no `disk_full`, no `hash_mismatch`; FAIL: any worker crash or transfer error appears; logs: `src/python/flowshift.log`, `src/python/flowshift_runtime.out`.
12. Verify temp cleanup. PASS: fresh materialized files survive long enough to paste and old temp files are removed later; FAIL: files vanish too early or stale temp files never clear; logs: `src/python/flowshift.log`.

---

## Runtime lifecycle

- [ ] `python src/python/tray.py --tray` — tray icon appears, no CMD popup.
- [ ] Control socket answers `status` on `127.0.0.1:45782`.
- [ ] Control socket answers `diagnostics` and returns a serializable report.
- [ ] `python src/python/flowshift_diagnose.py` prints a report even when the runtime is down.
- [ ] GUI status shows `Runtime: gesund (alle Worker aktiv)` (green) and
      `Session: <id> interaktiv` (green, NOT Session 0).
- [ ] GUI `Pipeline:` line updates while forwarding (queued/forwarded/injected).
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

## Mouse movement + smoothing (delta forwarding + coalescing sender)

- [ ] Activate profile Laptop → Surface. Move mouse on Laptop.
- [ ] Surface cursor moves proportionally (not frozen, not jumping back to start position).
- [ ] Cursor stays suppressed on Laptop (Laptop apps do not receive cursor moves).
- [ ] Move to screen edges: cursor stops at edge on Surface (clamped correctly).
- [ ] Deactivate: Laptop cursor unfreezes.
- [ ] Repeat after a Laptop runtime restart: anchor is re-primed on each activation.
- [ ] **Smoothness**: slow move, fast move, small precision move, diagonal, circle,
      window drag, mouse text selection. Compare to the previous jittery feel and
      note the result in the report (smoother / not / measured flush rate).
- [ ] Clicks (left/right/double/middle) remain reliable during heavy movement.
- [ ] Wheel remains reliable during heavy movement.
- [ ] Optional tuning in `config.json` `"mouse"` block: raise/lower
      `flush_interval_ms` and `sensitivity`, confirm effect.

---

## Keyboard forwarding + text selection (extended-key fix)

- [ ] Individual keys (letters, digits, function keys) forwarded and typed on Surface.
- [ ] Open Notepad/Notepad++ on Surface, type text, then from the Laptop:
  - [ ] **Shift+Left/Right** selects character-wise.
  - [ ] **Shift+Up/Down** selects line-wise.
  - [ ] **Shift+Home** selects to line start; **Shift+End** to line end.
  - [ ] **Ctrl+Shift+Left/Right** selects word-wise.
  - [ ] **Ctrl+Shift+Home/End** selects to document start/end.
- [ ] `type_text` via GUI Live Test: multi-line text with Enter and Tab lands correctly.
- [ ] No keys/modifiers stuck after deactivate/disconnect (synthetic key_up sent).
- [ ] Kill switch `Ctrl+Alt+Shift+Win+F12`: stops forwarding and quits runtime.

---

## Remote desktop-file creation (real remote input)

- [ ] Versions match on both devices (or use `--force`).
- [ ] Run `python src/python/remote_desktop_file_test.py --check` → reports match.
- [ ] Run `python src/python/remote_desktop_file_test.py` → creates
      `FlowShift_Remote_Test.txt` on the **Surface** desktop with the 4-line poem,
      purely via forwarded Win+R / notepad / typing / Ctrl+S.
- [ ] Repeat 3×, stopping+starting the Laptop runtime between runs (Surface runtime
      stays up); use `--repeat` or re-run. Files appear each time.

---

## Clipboard — text layer (two devices)

- [ ] GUI **Clipboard** tab: enable clipboard, save. Verify the store path note.
- [ ] On Laptop, copy several texts (Ctrl+C). The watcher captures them (GUI
      history list per peer profile shows them after "Aktualisieren").
- [ ] Activate Laptop → Surface. On the Surface GUI, the peer profile's history
      pulls exactly the missing text items (in order), status "verfügbar".
- [ ] Copy 2 more on Laptop, re-activate: only the 2 new items transfer.
- [ ] On Surface, select an item → "In Zwischenablage" → Ctrl+V pastes it.
- [ ] Pin/unpin, delete one, "Alle löschen" work.
- [ ] Bidirectional: copy on Surface, activate Surface → Laptop, Laptop pulls it.
- [ ] Runtime health stays green; mouse/keyboard stay responsive during sync.

## Clipboard — history window

- [ ] ClipboardWindow opens without a crash.
- [ ] Textitem can be dragged taller with the mouse and shows more text.
- [ ] Textitem can be dragged shorter with the mouse and shows less text.
- [ ] Preview/body height changes live while dragging.
- [ ] Progressbar and buttons stay visible and clickable.
- [ ] Scroll behavior still works after resizing cards.

## Clipboard — animated GIF

- [ ] Single `.gif` file shows an animated preview in the Clipboard window.
- [ ] GIF keeps its aspect ratio while animating.
- [ ] Switching thumbnail size reloads the GIF preview at the new size.
- [ ] Closing the Clipboard window stops the animation cleanly.

## Clipboard — HTML

- [ ] Copied HTML shows an `HTML` item in the Clipboard window.
- [ ] Preview text is readable and does not show raw markup.
- [ ] `clip_get` pastes HTML into an app that supports `HTML Format`.
- [ ] Plaintext fallback is also set on the clipboard.

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
