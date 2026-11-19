# Hotkey Configuration

This describes the hotkey model as **implemented** in the Python runtime
(`src/python/runtime_model.py`, used by both `tray.py` and `gui.py`).

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
- `key` is a Windows virtual-key code (`0x31='1'`, `0x30='0'`, ...).

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

The runtime registers each hotkey with Windows `RegisterHotKey`. When the config
changes, the runtime **unregisters and re-registers** all hotkeys on the window
thread, so a change made in the GUI takes effect without leaving stale OS
bindings behind. Failed registrations are logged.

## Overlay hotkeys (clipboard overlay, command wheel)

| Slot (`overlay_hotkeys.entries[].slot`) | Hotkey | Configured in |
|---|---|---|
| `clipboard_overlay` | `clipboard.overlay_hotkey`, default `{"mods": 5, "vk": 86}` = Ctrl+Alt+V | WebGUI Settings > Clipboard > Overlay hotkey (`POST /api/settings`) |
| `clipboard_ctrl_win_v` | Ctrl+Win+V (fixed, skipped when it equals the overlay hotkey) | always on while clipboard is enabled |
| `clipboard_win_v` | Win+V (only with `clipboard.intercept_win_v`) | Settings > Clipboard > Intercept Win+V |
| `command_wheel` | `command_wheel.hotkey`, default Ctrl+Alt+Space (registered with `MOD_NOREPEAT`) | Settings > Command Wheel > Open hotkey (`POST /api/actions/wheel`) |

Both hotkeys use the same `{mods, vk}` form and bitfield as above; the WebGUI
uses the identical bits (`webgui/src/hotkeyFormat.js`). The clipboard overlay
hotkey and the wheel hotkey must differ (`400 hotkey_conflict`).

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
clipboard hotkey or the tray menu for the clipboard, Ctrl+Right click for the
command wheel (mouse hook, independent of `RegisterHotKey`).

### Command wheel trigger (`command_wheel.trigger`)

```json
"command_wheel": {"trigger": {"tap_open": true, "hold_swipe": true,
                              "hold_ms": 250, "mouse_button": "none"}}
```

- `mouse_button`: `none` (default, side buttons keep back/forward), `x1` or
  `x2` opens the wheel with that mouse side button.
- `tap_open`: a press shorter than `hold_ms` opens the wheel for clicking.
- `hold_swipe`: hold the hotkey, Ctrl+Right click or side button, move towards
  a field and release: the field runs; releasing in the middle closes without
  an action.
- `hold_ms`: threshold between tap and hold, 100..2000 ms.

At least one of `tap_open` / `hold_swipe` stays on (`400 trigger_disabled`).
Configured in WebGUI Settings > Command Wheel; saving re-registers hotkey and
side button immediately. While forwarding to another device none of the
triggers opens the wheel locally.

## Kill switch

**Ctrl+Alt+Shift+Win+F12** is a fixed emergency hotkey that stops forwarding and
quits the runtime.

## Not implemented

Profile-level display routing / video modes (Extended/Duplicated, per-monitor
source) shown in some older docs are part of the target design, not the current
hotkey system.
