"""Finding the other FlowShift PC also over a VPN (Viktor 26.11.).

The search on the Peers page sends the ``discover`` call as a UDP broadcast on
the FlowShift port. Broadcasts stay in the local network: WireGuard and
Tailscale do not carry them, so a PC that is only reachable over such a tunnel
never hears the call and the list stayed empty without a word why.

This module adds what the broadcast cannot do, without a new port or message:

- ``collect_unicast_targets``: addresses to ask directly (unicast, the same
  ``discover`` call): with Tailscale installed the devices of
  ``tailscale status --json`` (only read when the command exists), otherwise
  the addresses in the VPN networks of this PC's own interfaces, at most
  ``MAX_UNICAST_TARGETS``. Replies look like broadcast replies.
- ``scan_hint``: the plain explanation the Peers page shows when nobody
  answered.
- ``ping_failure_hint``: the likely cause of a failed ping.
- ``PairRequests``: a PC that was added on the other side and dialed in, but is
  not in this PC's own list, is offered here as "add it too" (added only after
  the user confirms).
"""
import errno
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time

from runtime_model import DEFAULT_PORT

try:
    import version as _version
    _CREATE_NO_WINDOW = _version.CREATE_NO_WINDOW
except Exception:  # pragma: no cover - version is always importable in the app
    _CREATE_NO_WINDOW = 0

MAX_UNICAST_TARGETS = 1024
MAX_TAILSCALE_TARGETS = 256
# Networks larger than this are not walked; only the /24 around an own address.
MIN_WALKED_PREFIX = 22
COMMAND_TIMEOUT = 4.0

VIA_BROADCAST = "broadcast"
VIA_TAILSCALE = "tailscale"
VIA_VPN = "vpn"

_VPN_NAME = re.compile(r"wireguard|wintun|tailscale|zerotier|openvpn|vpn|tap-windows|nordlynx|\bwg", re.I)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _ipv4(value):
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    return address if address.version == 4 else None


def _usable_host(address):
    return not (address.is_loopback or address.is_link_local or address.is_multicast
                or address.is_unspecified or str(address) == "255.255.255.255")


# --- Tailscale --------------------------------------------------------------

def find_tailscale(which=shutil.which, exists=os.path.isfile, environ=os.environ):
    """Path of the Tailscale command line tool, or None when it is not installed."""
    found = which("tailscale")
    if found:
        return found
    for base in (environ.get("ProgramFiles"), environ.get("ProgramFiles(x86)"),
                 r"C:\Program Files"):
        if base:
            candidate = os.path.join(base, "Tailscale", "tailscale.exe")
            if exists(candidate):
                return candidate
    return None


TAILSCALE_NOT_INSTALLED = "not installed (no tailscale command)"


def read_tailscale_status(run=subprocess.run, which=shutil.which, exists=os.path.isfile,
                          environ=os.environ, diag=None):
    """Parsed ``tailscale status --json``, or None (not installed, not running, bad output).

    With a ``diag`` dict, ``diag["reason"]`` says in a few words why there is
    no status, or the backend state (``Running``, ``Stopped``, ...) when there is.
    """
    def why(text):
        if diag is not None:
            diag["reason"] = text
        return None

    exe = find_tailscale(which, exists, environ)
    if not exe:
        return why(TAILSCALE_NOT_INSTALLED)
    try:
        proc = run([exe, "status", "--json"], capture_output=True, text=True,
                   timeout=COMMAND_TIMEOUT, creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return why(f"tailscale status gave no answer within {COMMAND_TIMEOUT:g} s")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return why(f"tailscale status could not run ({exc.__class__.__name__})")
    if getattr(proc, "returncode", 1) != 0 or not (proc.stdout or "").strip():
        detail = " ".join(str(getattr(proc, "stderr", "") or "").split())[:120]
        return why(f"tailscale status failed (exit {getattr(proc, 'returncode', '?')}"
                   + (f": {detail}" if detail else "") + ")")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return why("tailscale status gave no readable answer")
    if not isinstance(data, dict):
        return why("tailscale status gave no readable answer")
    state = str(data.get("BackendState") or "").strip()[:32]
    if diag is not None:
        diag["reason"] = state or "running"
    return data


def _tailscale_ipv4(node):
    for value in (node.get("TailscaleIPs") or []) if isinstance(node, dict) else []:
        address = _ipv4(value)
        if address is not None:
            return str(address)
    return None


def tailscale_targets(status, limit=MAX_TAILSCALE_TARGETS):
    """``[{"host", "name"}]`` of the other online devices in ``tailscale status --json``."""
    if not isinstance(status, dict):
        return []
    peers = status.get("Peer")
    if not isinstance(peers, dict):
        return []
    targets = []
    for node in peers.values():
        if not isinstance(node, dict) or node.get("Online") is False:
            continue
        host = _tailscale_ipv4(node)
        if not host:
            continue
        name = str(node.get("HostName") or node.get("DNSName") or "").strip().rstrip(".")[:64]
        targets.append({"host": host, "name": name})
        if len(targets) >= limit:
            break
    return targets


def tailscale_offline_count(status):
    """How many other devices ``tailscale status`` lists as offline (skipped by the scan)."""
    peers = status.get("Peer") if isinstance(status, dict) else None
    if not isinstance(peers, dict):
        return 0
    return sum(1 for node in peers.values() if isinstance(node, dict) and node.get("Online") is False)


def tailscale_self(status):
    """``{"address", "dns_name"}`` of this PC in Tailscale, or None."""
    node = status.get("Self") if isinstance(status, dict) else None
    host = _tailscale_ipv4(node)
    if not host:
        return None
    return {"address": host, "dns_name": str(node.get("DNSName") or "").strip().rstrip(".")[:128]}


# --- VPN interfaces ---------------------------------------------------------

_INTERFACES_PS = (
    "$a = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
    "Select-Object IPAddress,PrefixLength,InterfaceAlias,InterfaceIndex); "
    "$r = @(Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
    "Select-Object DestinationPrefix,InterfaceIndex); "
    "$n = @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | "
    "Select-Object ifIndex,InterfaceDescription); "
    "@{addresses=$a; routes=$r; adapters=$n} | ConvertTo-Json -Compress -Depth 3"
)


def read_interfaces(run=subprocess.run, platform=os.name):
    """Addresses, routes and adapter descriptions of this PC (Windows), else None."""
    if platform != "nt":
        return None
    for shell in ("powershell", "pwsh"):
        try:
            proc = run([shell, "-NoProfile", "-Command", _INTERFACES_PS], capture_output=True,
                       text=True, timeout=COMMAND_TIMEOUT, creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if getattr(proc, "returncode", 1) == 0 and (proc.stdout or "").strip():
            try:
                data = json.loads(proc.stdout)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return None


def _as_list(value):
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def vpn_targets(interfaces, limit=MAX_UNICAST_TARGETS):
    """``(hosts, interface_names)`` to ask in the VPN networks of this PC.

    An interface counts as VPN when its name or adapter description says so
    (WireGuard, Tailscale, OpenVPN, ZeroTier, ...) or its address is in the
    carrier-grade range Tailscale uses. Walked are the network of its address
    and the routes over it (WireGuard ``AllowedIPs``); a default route and
    networks larger than /22 are not walked, only the /24 around an own
    address. Own addresses are left out.
    """
    if not isinstance(interfaces, dict):
        return [], []
    descriptions = {}
    for adapter in _as_list(interfaces.get("adapters")):
        descriptions[adapter.get("ifIndex")] = str(adapter.get("InterfaceDescription") or "")
    own = set()
    vpn = {}
    for entry in _as_list(interfaces.get("addresses")):
        address = _ipv4(entry.get("IPAddress"))
        if address is None:
            continue
        own.add(address)
        index = entry.get("InterfaceIndex")
        alias = str(entry.get("InterfaceAlias") or "")
        if not (_VPN_NAME.search(alias) or _VPN_NAME.search(descriptions.get(index, ""))
                or address in _CGNAT):
            continue
        info = vpn.setdefault(index, {"alias": alias, "own": [], "networks": []})
        info["own"].append(address)
        try:
            info["networks"].append(ipaddress.ip_network(f"{address}/{int(entry.get('PrefixLength'))}",
                                                         strict=False))
        except (TypeError, ValueError):
            pass
    for route in _as_list(interfaces.get("routes")):
        info = vpn.get(route.get("InterfaceIndex"))
        if info is None:
            continue
        try:
            network = ipaddress.ip_network(str(route.get("DestinationPrefix")), strict=False)
        except ValueError:
            continue
        if network.version == 4:
            info["networks"].append(network)

    hosts, seen, names = [], set(), []
    for info in vpn.values():
        before = len(hosts)
        for network in info["networks"]:
            if network.prefixlen == 0 or network.is_multicast or network.is_loopback \
                    or network.is_link_local:
                continue
            if network.prefixlen < 32 and network.overlaps(_CGNAT):
                # Tailscale addresses are scattered over 100.64.0.0/10: walking
                # a block finds nothing, its devices come from tailscale status.
                continue
            if network.prefixlen < MIN_WALKED_PREFIX:
                inside = [a for a in info["own"] if a in network]
                if not inside:
                    continue
                network = ipaddress.ip_network(f"{inside[0]}/24", strict=False)
            candidates = [network.network_address] if network.prefixlen == 32 else network.hosts()
            for address in candidates:
                if address in own or address in seen or not _usable_host(address):
                    continue
                seen.add(address)
                hosts.append(str(address))
                if len(hosts) >= limit:
                    break
            if len(hosts) >= limit:
                break
        if len(hosts) > before and info["alias"] and info["alias"] not in names:
            names.append(info["alias"])
        if len(hosts) >= limit:
            break
    return hosts, names


def collect_unicast_targets(read_tailscale=read_tailscale_status, read_ifaces=read_interfaces,
                            limit=MAX_UNICAST_TARGETS):
    """``(targets, summary)`` for the direct (unicast) part of a peer scan.

    ``targets`` is ``[{"host", "via", "name"}]``: the Tailscale devices first,
    then the VPN network addresses (no host twice, at most ``limit``).
    ``summary`` says what was asked, for the hint text.
    """
    targets, seen = [], set()
    diag = {}
    try:
        status = read_tailscale(diag=diag)
    except TypeError:  # a reader without the diag argument (tests, older callers)
        status = read_tailscale()
    summary = {"tailscale": {"available": status is not None, "asked": 0,
                             "reason": diag.get("reason", ""),
                             "offline": tailscale_offline_count(status)},
               "vpn": {"interfaces": [], "asked": 0}}
    for target in tailscale_targets(status):
        if target["host"] not in seen and len(targets) < limit:
            seen.add(target["host"])
            targets.append({"host": target["host"], "via": VIA_TAILSCALE, "name": target["name"]})
            summary["tailscale"]["asked"] += 1
    hosts, names = vpn_targets(read_ifaces(), limit=limit)
    summary["vpn"]["interfaces"] = names
    for host in hosts:
        if len(targets) >= limit:
            break
        if host not in seen:
            seen.add(host)
            targets.append({"host": host, "via": VIA_VPN, "name": ""})
            summary["vpn"]["asked"] += 1
    own = tailscale_self(status)
    if own:
        summary["tailscale"]["self"] = own
    return targets, summary


# --- Texts ------------------------------------------------------------------

def scan_hint(found, summary, port=DEFAULT_PORT):
    """Plain English text for an empty scan, None when something was found."""
    if found:
        return None
    summary = summary if isinstance(summary, dict) else {}
    tailscale = summary.get("tailscale") or {}
    vpn = summary.get("vpn") or {}
    parts = [
        "No FlowShift PC answered. Over a VPN this is normal: the search call reaches only "
        "PCs in the same local network, WireGuard and Tailscale do not pass it on.",
    ]
    asked = []
    if tailscale.get("asked"):
        asked.append(f"{tailscale['asked']} Tailscale device(s)")
    if vpn.get("asked"):
        names = ", ".join(vpn.get("interfaces") or [])
        asked.append(f"{vpn['asked']} address(es) in the VPN network"
                     + (f" ({names})" if names else ""))
    if asked:
        parts.append(f"FlowShift also asked {' and '.join(asked)} directly, but none answered: "
                     "FlowShift may not run there, or Windows Firewall on that PC blocks "
                     f"port {port}.")
    parts.append(
        "Enter the other PC's address below: its IP address or name "
        f"(with Tailscale the 100.x.y.z address from the Tailscale app). The port is {port}.")
    return " ".join(parts)


def scan_log_lines(found, summary, searched, port=DEFAULT_PORT):
    """INFO lines for ``flowshift.log`` and the Event Log: how a scan searched, what it found.

    BOOK-Viktor's log had three scans with ``200`` and not a word which
    networks were asked or whether Tailscale was used.
    """
    summary = summary if isinstance(summary, dict) else {}
    adapters = []
    for row in searched or ():
        if not isinstance(row, dict):
            continue
        label = row.get("label") or row.get("address") or "adapter"
        status = row.get("status") or "?"
        text = {"sent": "broadcast sent", "point_to_point": "no broadcast (point-to-point)",
                "failed": "failed"}.get(status, status)
        if row.get("error") and status == "failed":
            text += f" ({str(row['error'])[:60]})"
        adapters.append(f"{label}: {text}")
    tailscale = summary.get("tailscale") or {}
    if tailscale.get("available"):
        ts = (f"Tailscale {tailscale.get('reason') or 'running'}, asked "
              f"{tailscale.get('asked', 0)} online device(s) directly")
        if tailscale.get("offline"):
            ts += f", {tailscale['offline']} offline device(s) skipped"
        own = tailscale.get("self") or {}
        if own.get("address"):
            ts += f", this PC is {own['address']}"
    else:
        ts = f"Tailscale not used: {tailscale.get('reason') or 'not available'}"
    vpn = summary.get("vpn") or {}
    if vpn.get("asked"):
        names = ", ".join(vpn.get("interfaces") or [])
        vpn_text = f"VPN: asked {vpn['asked']} address(es)" + (f" over {names}" if names else "")
    else:
        vpn_text = "VPN: no WireGuard/VPN network found to ask"
    lines = [("INFO", f"peer scan on port {port}: " + ("; ".join(adapters) or "no adapter")),
             ("INFO", f"peer scan: {ts}; {vpn_text}")]
    if found:
        parts = [f"{row.get('name') or '?'} {row.get('host')}:{row.get('port')} via "
                 f"{row.get('via') or 'broadcast'}" for row in found if isinstance(row, dict)]
        lines.append(("INFO", f"peer scan found {len(parts)}: " + ", ".join(parts)))
    else:
        lines.append(("INFO", "peer scan found no FlowShift PC"))
    return lines


_UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}


def ping_failure_hint(exc, host=None, port=DEFAULT_PORT):
    """Likely cause of a failed ping, in plain English."""
    where = host or "the other PC"
    port = port or DEFAULT_PORT
    on_tailscale = _ipv4(host) in _CGNAT if _ipv4(host) is not None else False
    firewall = (f"Most likely Windows Firewall on {where} blocks FlowShift: Windows treats a "
                "WireGuard or Tailscale connection as a 'Public' network. On that PC allow "
                f"port {port} for UDP and TCP for all network profiles (installing FlowShift "
                "there sets this rule); a blocking rule for Python or FlowShift under "
                "'Inbound Rules' wins over it. Also check that the PC is on and FlowShift runs.")
    if on_tailscale:
        firewall += " In Tailscale, the access rules must let both PCs reach each other."
    if isinstance(exc, OSError) and exc.__class__.__name__ == "gaierror":
        return (f"The name {where} is unknown on this PC. Enter the IP address instead "
                "(with Tailscale the 100.x.y.z address, or the full name ending in .ts.net).")
    if isinstance(exc, TimeoutError) or exc.__class__.__name__ == "timeout":
        return f"No answer from {where} on port {port}. {firewall}"
    if isinstance(exc, ConnectionRefusedError):
        return (f"{where} answers, but nothing listens on port {port}: FlowShift does not run "
                "there, or it uses another port.")
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if isinstance(exc, OSError) and code in _UNREACHABLE:
        return (f"There is no way to {where} from this PC: the VPN (WireGuard or Tailscale) is "
                "not connected here, or the address is wrong.")
    if isinstance(exc, ValueError) or isinstance(exc, ConnectionResetError):
        return (f"Something answered on {where} port {port}, but not as FlowShift. Check the "
                "address and port.")
    return f"The ping to {where} failed. {firewall}"


# --- Pair requests ----------------------------------------------------------

_DEVICE_ID = re.compile(r"^[0-9a-z_-]{1,64}$")
MAX_PAIR_REQUESTS = 16
PAIR_REQUEST_TTL = 3600.0


def _known(peers, device_id, host):
    for peer in peers or []:
        if not isinstance(peer, dict):
            continue
        if device_id and str(peer.get("device_id") or "").strip().lower() == device_id:
            return True
        if host and str(peer.get("host") or "").strip().lower() == host.lower():
            return True
    return False


class PairRequests:
    """PCs that dialed in and have this PC in their list, but are not in this PC's list.

    Everything comes from the network and is untrusted: the device ID must be a
    plain ID, the name is cut to 64 characters, the port must be valid, at
    most ``MAX_PAIR_REQUESTS`` entries live for ``PAIR_REQUEST_TTL`` seconds.
    The address is the one the connection came from, never a claimed one.
    Nothing is added to the configuration here; the WebGUI does that only
    after the user clicked Add.
    """

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._items = {}
        self._dismissed = set()

    @staticmethod
    def key_for(device_id, host):
        return f"device:{device_id}" if device_id else f"host:{host}"

    def note(self, device_id, name, host, port, peers):
        device_id = str(device_id or "").strip().lower()
        if device_id and not _DEVICE_ID.match(device_id):
            return None
        address = _ipv4(host)
        if address is None:
            return None
        host = str(address)
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        if not 1 <= port <= 65535:
            port = DEFAULT_PORT
        if _known(peers, device_id, host):
            return None
        key = self.key_for(device_id, host)
        name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()[:64] or host
        with self._lock:
            if key in self._dismissed:
                return None
            self._expire_locked()
            if key not in self._items and len(self._items) >= MAX_PAIR_REQUESTS:
                oldest = min(self._items, key=lambda k: self._items[k]["seen"])
                self._items.pop(oldest)
            self._items[key] = {"key": key, "name": name, "host": host, "port": port,
                                "device_id": device_id, "seen": self._clock()}
            return dict(self._items[key])

    def _expire_locked(self):
        limit = self._clock() - PAIR_REQUEST_TTL
        for key in [k for k, v in self._items.items() if v["seen"] < limit]:
            self._items.pop(key)

    def pending(self, peers):
        """Open requests, without devices that are in ``peers`` by now."""
        with self._lock:
            self._expire_locked()
            return [dict(v) for v in sorted(self._items.values(), key=lambda v: v["seen"])
                    if not _known(peers, v["device_id"], v["host"])]

    def take(self, key):
        with self._lock:
            return self._items.pop(str(key or ""), None)

    def dismiss(self, key):
        with self._lock:
            item = self._items.pop(str(key or ""), None)
            if item is not None:
                self._dismissed.add(item["key"])
            return item is not None
