# offline_guard.py
# Keeps the transcriber offline: no usage statistics, no network calls.
#
# Two layers, applied before any speech library loads (workspace/__main__.py,
# the worker and decode processes):
#   1. disable_telemetry() forces every known telemetry switch off. pyannote
#      4.x creates an OpenTelemetry exporter to otel.pyannote.ai at import and
#      exports on a timer; Hugging Face libraries have their own telemetry.
#   2. install() patches the socket layer so any DNS lookup, TCP connection or
#      UDP datagram to a non-loopback address is refused, including asyncio's
#      Windows path (ConnectEx and WSASendTo, which bypass socket.connect),
#      unless the host is on the allow_hosts() list (the workspace's download
#      process). Nothing lets a download through in the app's own processes
#      (the earlier app's permit() is gone); uninstall() is for tests only.
#      Refusals are recorded in `blocked` so tests can prove nothing tried to
#      reach the network.
#
# This is a tripwire for Python code, not a firewall: native libraries with
# their own sockets are not covered. On managed devices an outbound firewall
# rule for the runtime is the enforcing control (#111 B7).
#
# Stdlib only, so it is importable (and testable) without the app's packages.

import errno
import ipaddress
import os
import socket
import threading

# Forced, not defaulted: a value inherited from the user's environment must not
# re-enable telemetry.
TELEMETRY_SWITCHES = {
    "HF_HUB_DISABLE_TELEMETRY": "1",     # huggingface_hub
    "PYANNOTE_METRICS_ENABLED": "false",  # pyannote.audio 4.x usage metrics
    "OTEL_SDK_DISABLED": "true",          # any OpenTelemetry SDK provider
    "DO_NOT_TRACK": "1",                  # honoured by several libraries
}

MAX_RECORDED = 100

blocked = []                 # (host, port) of refused attempts, oldest first
_lock = threading.Lock()
_installed = False
_orig = {}


class NetworkBlockedError(OSError):
    """Raised when code tries to reach the network."""


def disable_telemetry():
    """Force every known telemetry switch off. Call before importing libraries."""
    os.environ.update(TELEMETRY_SWITCHES)


def disable_onnxruntime_telemetry():
    """Turn off ONNX Runtime's Windows telemetry events, if it is installed."""
    try:
        import onnxruntime
        onnxruntime.disable_telemetry_events()
    except Exception:
        pass   # not installed, or an older build without the switch


def _is_local(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = str(host).strip("[]").split("%")[0].lower()
    if host in ("", "localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False   # a hostname that needs DNS: not local


def _local_proxies():
    """(host, port) of proxies configured on this machine itself. Traffic sent
    to a loopback proxy would be forwarded to the internet, so such a
    connection counts as remote. Read from the environment and, on
    Windows, the registry (what requests/httpx/urllib honour by default)."""
    import urllib.parse
    import urllib.request
    found = set()
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    for url in proxies.values():
        try:
            parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
            if parts.hostname and _is_local(parts.hostname):
                found.add((parts.hostname.lower(), parts.port or 80))
        except ValueError:
            continue
    return found


_proxy_endpoints = set()
_allowed_names = ()          # host names (and their subdomains) a download may use
_proxy_names = ()            # configured proxies: exact names only
_allowed_addresses = set()   # addresses those names resolved to in this process
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_")


def _norm_address(host):
    """An IP literal in canonical form, or None if `host` is not one."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    text = str(host).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return str(ipaddress.ip_address(text.split("%")[0]))
    except ValueError:
        return None


def _norm_name(host):
    """A host name, lower case, without a trailing dot; None if it contains
    anything a real host name cannot (so tricks such as
    "huggingface.co%x.evil.example" never match)."""
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(host, str) or not host.isascii():
        return None                        # before lower(): no Unicode case tricks
    name = host.strip().lower().rstrip(".")
    if not name or not set(name) <= _NAME_CHARS or ".." in name or name.startswith("."):
        return None
    return name


def host_allowed(host):
    """True if `host` is an allowed download host (a listed name or one of its
    subdomains). IP literals and proxies are never valid request targets.
    Used for every HTTP request a download makes, including each redirect,
    which also covers downloads through a proxy (the proxy, not this
    computer, resolves the target name)."""
    name = _norm_name(host)
    return name is not None and _norm_address(name) is None and any(
        name == allowed or name.endswith("." + allowed) for allowed in _allowed_names)


def allow_hosts(names):
    """Let this process reach exactly these host names (and their
    subdomains), and the proxies configured for it, and nothing else. Used by
    the download process: a redirect to any other host fails at its DNS
    lookup, or at the HTTP check (host_allowed) when a proxy resolves names.
    Connections are allowed only to public addresses an allowed name resolved
    to here, and to the proxies."""
    global _allowed_names, _proxy_names
    if isinstance(names, (str, bytes)):
        raise TypeError("allow_hosts expects a list of host names")
    import urllib.parse
    import urllib.request
    proxy_names, proxy_addresses = [], []
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    for key, url in proxies.items():
        if key == "no":
            continue
        try:
            parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
        except ValueError:
            continue
        if parts.hostname:
            address = _norm_address(parts.hostname)
            if address:
                proxy_addresses.append(address)        # proxy given as an IP
            elif _norm_name(parts.hostname):
                proxy_names.append(_norm_name(parts.hostname))
    allowed = tuple(n for n in (_norm_name(x) for x in names) if n and not _norm_address(n))
    with _lock:
        _allowed_names, _proxy_names = allowed, tuple(proxy_names)
        _allowed_addresses.clear()
        _allowed_addresses.update(proxy_addresses)


def _resolvable(host):
    """May this name be looked up? Allowed download hosts and proxies only."""
    name = _norm_name(host)
    return name is not None and (host_allowed(name) or name in _proxy_names)


def _record_addresses(host, result):
    """Remember what an allowed name resolved to. Download hosts count only
    for public addresses (a lookup that returns a private or local address is
    not followed); a proxy may be anywhere on the firm's network."""
    proxy = _norm_name(host) in _proxy_names
    found = set()
    for entry in result:
        address = _norm_address(entry[4][0])
        if address and (proxy or ipaddress.ip_address(address).is_global):
            found.add(address)
    with _lock:
        _allowed_addresses.update(found)


def _check(host, port, resolving=False):
    if _is_local(host) and (str(host).lower(), port) not in _proxy_endpoints:
        return True
    if (_allowed_names or _proxy_names) and (
            _norm_address(host) in _allowed_addresses
            or (resolving and _norm_address(host) is None and _resolvable(host))):
        return True
    with _lock:
        blocked.append((str(host), port))
        del blocked[:-MAX_RECORDED]
    return False


def _refuse(host, port):
    return NetworkBlockedError(
        errno.EACCES,
        f"Network access blocked: {host}:{port}. This app works offline; "
        f"only its model download process may go online.")


def _guarded_connect(self, address):
    if isinstance(address, tuple) and not _check(address[0], address[1]):
        raise _refuse(address[0], address[1])
    return _orig["connect"](self, address)


def _guarded_connect_ex(self, address):
    if isinstance(address, tuple) and not _check(address[0], address[1]):
        return errno.EACCES
    return _orig["connect_ex"](self, address)


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    if not _check(host, port, resolving=True):
        raise _refuse(host, port)
    result = _orig["getaddrinfo"](host, port, *args, **kwargs)
    if (_allowed_names or _proxy_names) and _resolvable(host):
        _record_addresses(host, result)
    return result


def _guarded_sendto(self, data, *args):
    address = args[-1] if args else None          # sendto(data[, flags], address)
    if isinstance(address, tuple) and not _check(address[0], address[1]):
        raise _refuse(address[0], address[1])
    return _orig["sendto"](self, data, *args)


def _guarded_sendmsg(self, buffers, *args):
    address = args[2] if len(args) > 2 else None  # sendmsg(buffers, anc, flags, address)
    if isinstance(address, tuple) and not _check(address[0], address[1]):
        raise _refuse(address[0], address[1])
    return _orig["sendmsg"](self, buffers, *args)


def _guarded_iocp_connect(self, conn, address):
    # asyncio's Windows event loop connects with ConnectEx (TCP) or WSAConnect
    # (UDP) directly, bypassing socket.connect; IP literals also skip DNS.
    if isinstance(address, tuple) and not _check(address[0], address[1]):
        raise _refuse(address[0], address[1])
    return _orig["iocp_connect"](self, conn, address)


def _guarded_iocp_sendto(self, conn, buf, flags=0, addr=None):
    if isinstance(addr, tuple) and not _check(addr[0], addr[1]):
        raise _refuse(addr[0], addr[1])
    return _orig["iocp_sendto"](self, conn, buf, flags, addr)


def _iocp_proactor():
    """asyncio's Windows proactor class, or None elsewhere."""
    if os.name != "nt":
        return None
    try:
        from asyncio import windows_events
    except ImportError:
        return None
    return windows_events.IocpProactor


def _guard_lookup(name):
    """Wrap a legacy single-argument DNS function (gethostbyname and friends)."""
    def guarded(host, *args, **kwargs):
        if not _check(host, 0):
            raise _refuse(host, 0)
        return _orig[name](host, *args, **kwargs)
    return guarded


def _guarded_getnameinfo(sockaddr, flags):
    if not _check(sockaddr[0], sockaddr[1]):
        raise _refuse(sockaddr[0], sockaddr[1])
    return _orig["getnameinfo"](sockaddr, flags)


_LOOKUPS = ("gethostbyname", "gethostbyname_ex", "gethostbyaddr")


def install():
    """Refuse non-loopback network access from now on (idempotent)."""
    global _installed, _proxy_endpoints
    with _lock:
        if _installed:
            return
        _proxy_endpoints = _local_proxies()
        _orig["connect"] = socket.socket.connect
        _orig["connect_ex"] = socket.socket.connect_ex
        _orig["sendto"] = socket.socket.sendto
        _orig["getaddrinfo"] = socket.getaddrinfo
        _orig["getnameinfo"] = socket.getnameinfo
        for name in _LOOKUPS:
            _orig[name] = getattr(socket, name)
            setattr(socket, name, _guard_lookup(name))
        socket.socket.connect = _guarded_connect
        socket.socket.connect_ex = _guarded_connect_ex
        socket.socket.sendto = _guarded_sendto
        if hasattr(socket.socket, "sendmsg"):          # not on Windows
            _orig["sendmsg"] = socket.socket.sendmsg
            socket.socket.sendmsg = _guarded_sendmsg
        socket.getaddrinfo = _guarded_getaddrinfo
        socket.getnameinfo = _guarded_getnameinfo
        proactor = _iocp_proactor()
        if proactor is not None:
            _orig["iocp_connect"] = proactor.connect
            _orig["iocp_sendto"] = proactor.sendto
            proactor.connect = _guarded_iocp_connect
            proactor.sendto = _guarded_iocp_sendto
        _installed = True


def uninstall():
    """Restore the original socket functions (used by tests)."""
    global _installed
    with _lock:
        if not _installed:
            return
        socket.socket.connect = _orig["connect"]
        socket.socket.connect_ex = _orig["connect_ex"]
        socket.socket.sendto = _orig["sendto"]
        if "sendmsg" in _orig:
            socket.socket.sendmsg = _orig["sendmsg"]
        socket.getaddrinfo = _orig["getaddrinfo"]
        socket.getnameinfo = _orig["getnameinfo"]
        for name in _LOOKUPS:
            setattr(socket, name, _orig[name])
        proactor = _iocp_proactor()
        if proactor is not None and "iocp_connect" in _orig:
            proactor.connect = _orig["iocp_connect"]
            proactor.sendto = _orig["iocp_sendto"]
        _installed = False
