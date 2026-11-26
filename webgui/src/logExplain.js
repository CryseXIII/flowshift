// Plain-words sentences for the most common technical log lines of the Event
// Log page. The original line stays visible below the sentence.
const EDGE_REFUSED = {
  invalid_target: 'The other PC refused the edge switch because it is not paired by device ID. Click Ping on its profile under Peers once, then try again; if it stays, update FlowShift on both PCs.',
  busy: 'The other PC refused the edge switch because it is already in another switch.',
  update_maintenance: 'The other PC refused the edge switch because an update or installation is running there.',
  set_cursor_failed: 'The other PC refused the edge switch because it could not place the mouse pointer.',
  invalid_edge: 'The other PC refused the edge switch because the edge assignment is invalid. Check the Display page.',
  timeout: 'The other PC did not answer the edge switch within 3 seconds.',
}

const RULES = [
  [/^edge_enter rejected: missing session or source identity/,
    () => 'An edge switch request from the other PC was incomplete and was ignored. Update FlowShift on both PCs.'],
  [/^edge_enter rejected: (\S+)/,
    (m) => EDGE_REFUSED[m[1]] || `The other PC refused the edge switch (reason: ${m[1]}).`],
  [/^ignoring edge_enter for foreign target/,
    () => 'The other PC tried to switch to this PC but addressed a different device, so this PC refused it.'],
  [/^edge trigger ignored: no peer for (\w+)/,
    (m) => `The mouse reached the ${m[1]} edge, but the PC assigned to it is no longer in the peer list. Assign the edge again on the Display page.`],
  [/^edge trigger connection failed/,
    () => 'The mouse reached a screen edge, but FlowShift could not connect to the other PC. Check that FlowShift runs there and that Ping works under Peers.'],
  [/^edge_enter send failed/,
    () => 'The edge switch request could not be sent; the connection to the other PC dropped.'],
  [/^edge-watch trigger (\w+) -> (.+)$/,
    (m) => `The mouse reached the ${m[1]} edge; switching to ${m[2]}.`],
  [/^ping failed -> (.+?) \S+:\d+:/,
    (m) => `Ping to ${m[1]} failed: the other PC did not answer. Check that FlowShift runs there and both PCs are in the same network.`],
  [/^(no connection for active peer|forward send failed ->)/,
    () => 'The connection to the controlled PC was lost, so forwarding was switched off. Mouse and keyboard work on this PC again.'],
  [/^kill switch pressed|^WM_HOTKEY kill switch received/,
    () => 'The emergency key combination was pressed; forwarding was stopped.'],
  [/^hotkey target unresolved/,
    () => 'A hotkey points to a PC that is no longer in the peer list. Assign it again under Settings.'],
  [/^RegisterHotKey .*failed/,
    () => 'A hotkey could not be registered because Windows or another program already uses it. Choose another one under Settings.'],
  [/^web_api (GET|POST|DELETE) /,
    () => 'A request from this page failed inside FlowShift. The technical line below helps with a bug report.'],
  [/^worker crashed: (\S+?):/,
    (m) => `An internal part of FlowShift (${m[1]}) stopped because of an error. Restarting FlowShift helps; please report the technical line below.`],
  [/^(tcp|discovery) listener failed on port (\d+)/,
    (m) => `FlowShift could not open port ${m[2]}. Another program, or a second FlowShift, may be using it.`],
  [/^another FlowShift runtime instance is already running/,
    () => 'FlowShift is already running; this second start was stopped.'],
  [/^refusing self-connection/,
    () => 'A peer entry points to this PC itself, so FlowShift does not connect to it.'],
  [/^peer .+ speaks link protocol/,
    () => 'The other PC runs a FlowShift version that does not fit this one. Update FlowShift on both PCs.'],
]

export function explainLogLine(text) {
  const line = String(text || '')
  for (const [pattern, explain] of RULES) {
    const match = line.match(pattern)
    if (match) return explain(match)
  }
  return null
}
