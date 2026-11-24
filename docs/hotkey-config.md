# Hotkey Configuration

This describes the hotkey model as **implemented** in the Python runtime
(`src/python/runtime_model.py`, used by both `tray.py` and `gui.py`).

## Triggers: a key or a mouse button (`input_trigger.py`)

Every configurable hotkey (command wheel, clipboard overlay, switching to a
peer, return to local) stores one **trigger**:

| Form | Stored as | Registered with |
|---|---|---|
| key | `{"mods": 5, "vk": 86}` (Ctrl+Alt+V) | Windows `RegisterHotKey` |
| mouse button | `{"mods": 1, "mouse": "right"}` (Ctrl+Right click) | the always-on low-level mouse hook of FlowShift |
| empty | `null` (peer rows: `key: 0` without `mouse`) | nothing; deliberately cleared, never reported as a problem |

`mouse` is one of `left`, `right`, `middle`, `x1`, `x2` (side buttons),
`wheel_up`, `wheel_down`. A left or right click and the mouse wheel need at
least one modifier (API error `mouse_needs_modifier`), otherwise the computer
could no longer be used; middle click and the side buttons may be used alone.
Modifiers must match exactly (Ctrl+Right click is not Ctrl+Shift+Right click).

The mouse hook (`tray.wheel_trigger_mouse_proc`, decision in
`input_trigger.MouseTriggerMatcher`) swallows only a matching press and its
release (or one wheel notch per scroll, further notches within 0.4 s are
swallowed silently); every other mouse event is passed on at once, injected
events (FlowShift's own `SendInput`) always. Wheel triggers have no release:
the command wheel then opens for clicking (no hold and swipe). While
forwarding to another device only "return to local" is matched (the
forwarding mouse hook asks the same matcher), all other mouse hotkeys belong
to the local screen. Win+click can open the Start menu when Win is released;
prefer Ctrl, Alt or Shift for mouse hotkeys.

## Actions and stable identity

A hotkey binds a key combination to an **action**:

| Action | Meaning |
|---|---|
| `return_local` | return input to the local machine |
| `forward_peer:<peer-identity>` | forward input to a specific peer |

The peer identity is **stable and never a list index**:

- `device:<device_id>` – preferred, uses the peer's `device_id`
- `endpoint:<host>:<port>` – fallback when no `device_id` is known yet

Because the binding stores the identity (not the position in the peer list),
deleting, inserting or renaming peers does **not** make a hotkey point at the
wrong device:

- **Rename** a peer (same `device_id`) → hotkey still targets it; label updates.
- **Delete** a peer → its hotkey becomes visibly invalid ("(ungültig)"), it is
  **not** silently reassigned to another peer.
- **Insert** a peer → existing hotkeys are unaffected.

## Stored form (`config.json`)

```json
{
  "device_name": "Laptop-Viktor",
  "device_id": "a1b2c3d4",
  "port": 45781,
  "peers": [
    { "name": "Surface", "host": "192.168.1.101", "port": 45781, "device_id": "b5c6d7e8" }
  ],
  "hotkeys": [
    { "action": "forward_peer:device:b5c6d7e8", "mods": 5, "key": 49, "label": "Forward to Surface" },
    { "action": "return_local",                 "mods": 5, "key": 48, "label": "Return to local" }
  ]
}
```

- `mods` is a bitfield: `1=Ctrl, 2=Shift, 4=Alt, 8=Win` (so `5 = Ctrl+Alt`).
- `key` is a Windows virtual-key code (`0x31='1'`, `0x30='0'`, ...); `0`
  means no key (empty, nothing is registered).
- A mouse row stores `"key": 0, "mouse": "x1"` (older readers still find
  `key`), e.g. `{"action": "return_local", "mods": 1, "key": 0, "mouse": "x2"}`
  = Ctrl+Side button 2 returns to the local PC while forwarding.
- `GET /api/status` -> `hotkeys[]` carries per row `active` (`true`, `false`,
  `null` when empty) and `error` (plain text, only when a set hotkey could
  not be registered); the WebGUI shows only that error in red.

## Defaults

- Each of the first 9 peers gets `Ctrl+Alt+1..9` (in peer order at creation time).
- `Ctrl+Alt+0` = `return_local`.
- New peers added later receive a free `Ctrl+Alt+<n>` automatically.

## Migration from the old format

Older configs used index-based actions (`forward_0`, `forward_1`, ...). On load
these are migrated **once** to `forward_peer:<identity>` based on the current
peer list, labels are refreshed, and the config is saved. Indexes that no longer
resolve become an explicit invalid marker (they never crash and never jump to a
different peer).

## OS-level registration

The runtime registers each key hotkey with Windows `RegisterHotKey` and hands
every mouse hotkey to the mouse hook. When the config changes, the runtime
**unregisters and re-registers** all hotkeys on the window thread (and
replaces the mouse hook bindings), so a change made in the WebGUI takes
effect without leaving stale bindings behind. Failed registrations are logged.

## Overlay hotkeys (clipboard overlay, command wheel)

| Slot (`overlay_hotkeys.entries[].slot`) | Hotkey | Configured in |
|---|---|---|
| `clipboard_overlay` | `clipboard.overlay_hotkey`, default `{"mods": 5, "vk": 86}` = Ctrl+Alt+V; `null` = cleared | WebGUI Settings > Clipboard > Overlay hotkey (`POST /api/settings`) |
| `clipboard_ctrl_win_v` | Ctrl+Win+V (fixed, skipped when it equals the overlay hotkey) | always on while clipboard is enabled |
| `clipboard_win_v` | Win+V (only with `clipboard.intercept_win_v`) | Settings > Clipboard > Intercept Win+V |
| `command_wheel` | `command_wheel.hotkey`, default `{"mods": 1, "mouse": "right"}` = Ctrl+Right click; `null` = cleared | Settings > Command Wheel > Open hotkey (`POST /api/actions/wheel`) |
| `command_wheel_extra_1..3` | `command_wheel.extra_hotkeys[]`, up to three more triggers, default none | Settings > Command Wheel > Add another hotkey |

All of them use the trigger forms above; the WebGUI uses the identical bits
and names (`webgui/src/hotkeyFormat.js`). Key hotkeys of the wheel are
registered with `MOD_NOREPEAT`. Cleared hotkeys have no entry at all, so they
never show a warning.

### Recording a hotkey in the WebGUI

Every hotkey field (`TriggerPicker` in `HotkeyStatus.jsx`, used for the
command wheel, the clipboard overlay and every peer hotkey row): click the
field, then press the combination. Keys, left/right/middle click, the side
buttons and the mouse wheel are recorded together with the Ctrl/Alt/Shift/Win
keys held at that moment (a key pressed without modifiers keeps the ticked
ones). A plain left or right click or wheel is refused with a hint and the
field keeps recording; Escape stops. The list next to the field picks a mouse
button without pressing it (needed when FlowShift itself already catches the
combination, e.g. the current wheel hotkey), `Clear` empties the hotkey. The
field never opens the browser context menu or goes back/forward on a side
button.

### Conflicts

The same trigger may be assigned only once across the command wheel (main
and extra hotkeys), the clipboard overlay hotkey, the peer hotkeys, the fixed
kill switch and (with the clipboard on) Ctrl+Win+V
(`input_trigger.find_conflicts`, `hotkey_status.collect_assignments`).
`POST /api/actions/wheel` and `POST /api/settings` refuse a duplicate with
`400 hotkey_conflict` and a `message` that names the hotkey and both uses
("Ctrl+Right click is assigned twice: Command wheel and Clipboard overlay.
..."); nothing is saved. The WebGUI shows the same text before saving and
keeps Save disabled. `POST /api/settings` ignores a posted `command_wheel`
block (the Settings page posts back what it loaded; the wheel belongs to
`POST /api/actions/wheel`).

`RegisterHotKey` fails with error **1409** (`ERROR_HOTKEY_ALREADY_REGISTERED`)
when Windows or another program already owns a combination; the key press
then never reaches FlowShift. Windows normally reserves Win+V and on many
systems Ctrl+Win+V and Ctrl+Alt+Space. Each registration result is collected by
`hotkey_status.py` and published as `GET /api/status` -> `overlay_hotkeys`
(`ok`, `conflicts`, `entries[]` with `hotkey`, `active`, `error_code`,
`alternatives`, plain-text `message`). A busy hotkey is shown

- once per new conflict set as a tray notification ("FlowShift: hotkey taken")
  plus a tooltip hint,
- as a banner and an "Overlay Hotkeys" list on the WebGUI Dashboard,
- next to the hotkey in Settings > Clipboard and Settings > Command Wheel.

The message names the busy combination and what works instead: another active
clipboard hotkey or the tray menu for the clipboard, another working wheel
hotkey or Ctrl+Right click on the FlowShift tray icon for the command wheel.
A mouse hotkey is reported as not working only when the mouse hook could not
be installed (`could not be set up: the mouse hook of FlowShift is not
running (Windows error N)`).

### Command wheel block (`command_wheel`, version 2)

```json
"command_wheel": {"version": 2,
                  "hotkey": {"mods": 1, "mouse": "right"},
                  "extra_hotkeys": [{"mods": 1, "mouse": "x1"}],
                  "trigger": {"tap_open": true, "hold_swipe": true, "hold_ms": 250}}
```

- `hotkey`: the one visible main trigger, default Ctrl+Right click (before
  0.7.0-dev.49 a fixed extra path next to the keyboard hotkey). Any key or
  mouse button with modifiers, or `null`.
- `extra_hotkeys`: up to three more triggers (key or mouse button), e.g. a
  side button alone; each opens the same wheel.
- `tap_open`: a press shorter than `hold_ms` opens the wheel for clicking.
- `hold_swipe`: hold any wheel hotkey (key or mouse button), move towards a
  field and release: the field runs; releasing in the middle closes without
  an action. Wheel-notch triggers have no hold and open for clicking.
- `hold_ms`: threshold between tap and hold, 100..2000 ms.

At least one of `tap_open` / `hold_swipe` stays on (`400 trigger_disabled`).
With no hotkey at all the wheel still opens with Ctrl+Right click on the
FlowShift tray icon. Saving in WebGUI Settings > Command Wheel re-registers
all wheel hotkeys immediately. While forwarding to another device none of the
triggers opens the wheel locally.

### Migration of older wheel settings

A `command_wheel` block without `version` (before 0.7.0-dev.49) is
converted once on start (`overlay_actions.migrate_wheel_block`, saved by
`tray.load_config`):

- main hotkey = Ctrl+Right click (that path always worked before);
- a self-chosen keyboard hotkey stays as extra hotkey;
- the old default Ctrl+Alt+Space is dropped (Windows usually owns it, which
  was the red warning, and Ctrl+Right click replaces it);
- `trigger.mouse_button` `x1`/`x2` becomes an extra hotkey with that side
  button alone (`{"mods": 0, "mouse": "x1"}`); `none` is dropped;
- pages and `tap_open`/`hold_swipe`/`hold_ms` are kept.

So every trigger that opened the wheel before still opens it. Peer hotkeys and
the clipboard overlay hotkey keep their stored form unchanged.

## Kill switch

**Ctrl+Alt+Shift+Win+F12** is a fixed emergency hotkey that stops forwarding and
quits the runtime.

## Not implemented

Profile-level display routing / video modes (Extended/Duplicated, per-monitor
source) shown in some older docs are part of the target design, not the current
hotkey system.
