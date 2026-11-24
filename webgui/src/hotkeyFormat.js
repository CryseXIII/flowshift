// Shared hotkey representation helpers for the WebGUI.
// mods is the tray bitmask of runtime_model.py (MOD_CTRL=1, MOD_SHIFT=2,
// MOD_ALT=4, MOD_WIN=8), the key is a Windows virtual-key code.
export const MOD_CTRL = 1
export const MOD_SHIFT = 2
export const MOD_ALT = 4
export const MOD_WIN = 8

export const MOD_LABELS = [
  { bit: MOD_CTRL, label: 'Ctrl' },
  { bit: MOD_ALT, label: 'Alt' },
  { bit: MOD_SHIFT, label: 'Shift' },
  { bit: MOD_WIN, label: 'Win' },
]

export const VK_NAMES = {
  0x08: 'Backspace', 0x09: 'Tab', 0x0D: 'Enter', 0x1B: 'Escape',
  0x20: 'Space', 0x21: 'PageUp', 0x22: 'PageDown', 0x23: 'End', 0x24: 'Home',
  0x25: 'Left', 0x26: 'Up', 0x27: 'Right', 0x28: 'Down',
  0x2D: 'Insert', 0x2E: 'Delete',
  0x30: '0', 0x31: '1', 0x32: '2', 0x33: '3', 0x34: '4',
  0x35: '5', 0x36: '6', 0x37: '7', 0x38: '8', 0x39: '9',
  0x41: 'A', 0x42: 'B', 0x43: 'C', 0x44: 'D', 0x45: 'E',
  0x46: 'F', 0x47: 'G', 0x48: 'H', 0x49: 'I', 0x4A: 'J',
  0x4B: 'K', 0x4C: 'L', 0x4D: 'M', 0x4E: 'N', 0x4F: 'O',
  0x50: 'P', 0x51: 'Q', 0x52: 'R', 0x53: 'S', 0x54: 'T',
  0x55: 'U', 0x56: 'V', 0x57: 'W', 0x58: 'X', 0x59: 'Y', 0x5A: 'Z',
  0x70: 'F1', 0x71: 'F2', 0x72: 'F3', 0x73: 'F4', 0x74: 'F5',
  0x75: 'F6', 0x76: 'F7', 0x77: 'F8', 0x78: 'F9', 0x79: 'F10',
  0x7A: 'F11', 0x7B: 'F12',
  0x90: 'NumLock', 0x91: 'ScrollLock',
  0xBD: '-', 0xBB: '=', 0xDB: '[', 0xDD: ']', 0xBC: ',', 0xBE: '.',
  0xBF: '/', 0xC0: '`', 0xDE: "'", 0xDC: '\\',
}

// Modifier keys themselves are never a valid hotkey key.
const MODIFIER_VKS = new Set([0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5])

export function vkName(vk) {
  if (!vk) return '—'
  return VK_NAMES[vk] || `0x${vk.toString(16).toUpperCase()}`
}

export function formatKeyCombo(mods, key) {
  const parts = []
  for (const m of MOD_LABELS) if (mods & m.bit) parts.push(m.label)
  const name = vkName(key)
  if (name) parts.push(name)
  return parts.join('+') || '–'
}

// Turn a keydown event into a hotkey key; returns null for modifier-only presses.
export function captureVk(event) {
  const vk = event.keyCode || event.which
  if (!vk || vk < 0x08 || MODIFIER_VKS.has(vk)) return null
  return vk
}

// ── Triggers: a key or a mouse button, each with modifiers ──────────
// Same shapes and rules as src/python/input_trigger.py:
//   key   { mods, vk }      (RegisterHotKey)
//   mouse { mods, mouse }   (FlowShift's low-level mouse hook)
//   null                    (cleared on purpose, nothing is registered)
export const MOUSE_BUTTONS = ['left', 'right', 'middle', 'x1', 'x2', 'wheel_up', 'wheel_down']
export const MOUSE_LABELS = {
  left: 'Left click',
  right: 'Right click',
  middle: 'Middle click',
  x1: 'Side button 1',
  x2: 'Side button 2',
  wheel_up: 'Wheel up',
  wheel_down: 'Wheel down',
}
// Alone these would make the computer unusable (or break scrolling).
export const NEEDS_MODIFIER = new Set(['left', 'right', 'wheel_up', 'wheel_down'])
// MouseEvent.button -> trigger button (0 left, 1 middle, 2 right, 3/4 side).
const BROWSER_BUTTONS = { 0: 'left', 1: 'middle', 2: 'right', 3: 'x1', 4: 'x2' }

export function isMouseTrigger(t) {
  return !!t && typeof t.mouse === 'string'
}

export function formatTrigger(t) {
  if (!t) return ''
  if (isMouseTrigger(t)) {
    const parts = MOD_LABELS.filter((m) => t.mods & m.bit).map((m) => m.label)
    parts.push(MOUSE_LABELS[t.mouse] || t.mouse)
    return parts.join('+')
  }
  return formatKeyCombo(t.mods, t.vk)
}

// Modifier bits held during a keyboard, mouse or wheel event.
export function eventMods(e) {
  return (e.ctrlKey ? MOD_CTRL : 0) | (e.shiftKey ? MOD_SHIFT : 0)
    | (e.altKey ? MOD_ALT : 0) | (e.metaKey ? MOD_WIN : 0)
}

export function mouseButtonOf(e) {
  return BROWSER_BUTTONS[e.button] || null
}

// Why a trigger cannot be saved, or null. Empty (null) is always fine.
export function triggerProblem(t, { keyNeedsModifier = true } = {}) {
  if (!t) return null
  if (isMouseTrigger(t)) {
    if (!t.mods && NEEDS_MODIFIER.has(t.mouse)) {
      return `${MOUSE_LABELS[t.mouse]} alone cannot be a hotkey (the computer could no longer be used). Hold Ctrl, Alt, Shift or Win with it.`
    }
    return null
  }
  if (!t.vk) return 'Click the field and press a key or a mouse button.'
  if (keyNeedsModifier && !t.mods) return 'Choose at least one modifier (Ctrl, Alt, Shift or Win).'
  return null
}

export function sameTrigger(a, b) {
  if (!a || !b || (a.mods | 0) !== (b.mods | 0)) return false
  if (isMouseTrigger(a) || isMouseTrigger(b)) return a.mouse === b.mouse
  return !!a.vk && a.vk === b.vk
}

// items: [{ label, trigger }] -> [{ hotkey, labels, message }] for triggers set twice.
export function triggerConflicts(items) {
  const groups = []
  for (const item of items) {
    if (!item.trigger || triggerProblem(item.trigger, { keyNeedsModifier: false })) continue
    const group = groups.find((g) => sameTrigger(g.trigger, item.trigger))
    if (group) group.labels.push(item.label)
    else groups.push({ trigger: item.trigger, labels: [item.label] })
  }
  return groups.filter((g) => g.labels.length > 1).map((g) => {
    const hotkey = formatTrigger(g.trigger)
    return { hotkey, labels: g.labels, message: `${hotkey} is assigned twice: ${g.labels.join(' and ')}. Choose another hotkey for one of them.` }
  })
}

// Trigger without the display text the API adds.
export function plainTrigger(t) {
  if (!t) return null
  return isMouseTrigger(t) ? { mods: t.mods | 0, mouse: t.mouse } : { mods: t.mods | 0, vk: t.vk | 0 }
}
