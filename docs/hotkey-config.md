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

## Kill switch

**Ctrl+Alt+Shift+Win+F12** is a fixed emergency hotkey that stops forwarding and
quits the runtime.

## Not implemented

Profile-level display routing / video modes (Extended/Duplicated, per-monitor
source) shown in some older docs are part of the target design, not the current
hotkey system.
