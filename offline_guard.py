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
#      unless the caller is inside permit() (the earlier app's download path)
#      or the host is on the allow_hosts() list (the workspace's download
#      process). Refusals are recorded in `blocked` so tests can prove nothing
#      tried to reach the network.
#
# This is a tripwire for Python code, not a firewall: native libraries with
# their own sockets are not covered. On managed devices an outbound firewall
# rule for the runtime is the enforcing control (#111 B7).
#
# The guard is process-wide: while one thread holds permit() (a deliberate
# download), other threads are not blocked. The legacy app only downloads while
# its single worker is preparing a model, so nothing else is running then.
# Stdlib only, so it is importable (and testable) without the app's packages.

import contextlib
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
_permits = 0
_installed = False
_orig = {}


class NetworkBlockedError(OSError):
    """Raised when code tries to reach the network outside permit()."""


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
    to a loopback proxy would be forwarded to the internet, so outside permit()
    such a connection counts as remote. Read from the environment and, on
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
_allowed_addresses = set()   # addresses those names resolved to in this process


def _norm_host(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    return str(host).strip("[]").split("%")[0].lower().rstrip(".")


def _name_allowed(host):
    host = _norm_host(host)
    return any(host == name or host.endswith("." + name) for name in _allowed_names)


def allow_hosts(names):
    """Let this process reach exactly these host names (and their
    subdomains), and the proxies configured for it, and nothing else. Used by
    the download process: a redirect to any other host fails at its DNS
    lookup. Connections are allowed only to addresses that an allowed name
    resolved to here."""
    global _allowed_names
    import urllib.parse
    import urllib.request
    extra = []
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
            extra.append(parts.hostname)
            try:
                ipaddress.ip_address(parts.hostname.strip("[]"))
                _allowed_addresses.add(_norm_host(parts.hostname))   # proxy given as an IP
            except ValueError:
                pass
    with _lock:
        _allowed_names = tuple(_norm_host(n) for n in (*names, *extra) if n)


def _check(host, port, resolving=False):
    if _permits > 0:
        return True
    if _is_local(host) and (str(host).lower(), port) not in _proxy_endpoints:
        return True
    if _allowed_names and (_norm_host(host) in _allowed_addresses
                           or (resolving and _name_allowed(host))):
        return True
    with _lock:
        blocked.append((str(host), port))
        del blocked[:-MAX_RECORDED]
    return False


def _refuse(host, port):
    return NetworkBlockedError(
        errno.EACCES,
        f"Network access blocked: {host}:{port}. This app works offline; "
        f"only Update / repair and model downloads may go online.")


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
    if _allowed_names and _name_allowed(host):
        with _lock:
            _allowed_addresses.update(_norm_host(r[4][0]) for r in result)
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


@contextlib.contextmanager
def permit():
    """Allow network access for the duration of one deliberate download."""
    global _permits
    with _lock:
        _permits += 1
    try:
        yield
    finally:
        with _lock:
            _permits -= 1
