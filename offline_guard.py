# offline_guard.py
# Keeps the transcriber offline: no usage statistics, no network calls.
#
# Two layers, both applied by transcriber_core before any speech library loads:
#   1. disable_telemetry() forces every known telemetry switch off. pyannote
#      4.x creates an OpenTelemetry exporter to otel.pyannote.ai at import and
#      exports on a timer; Hugging Face libraries have their own telemetry.
#   2. install() patches the socket layer so any DNS lookup or connection to a
#      non-loopback address is refused, unless the caller is inside permit()
#      (the deliberate download/repair path). Refusals are recorded in
#      `blocked` so tests can prove nothing tried to reach the network.
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


def _check(host, port):
    if _permits > 0:
        return True
    if _is_local(host) and (str(host).lower(), port) not in _proxy_endpoints:
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
    if not _check(host, port):
        raise _refuse(host, port)
    return _orig["getaddrinfo"](host, port, *args, **kwargs)


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
        _orig["getaddrinfo"] = socket.getaddrinfo
        _orig["getnameinfo"] = socket.getnameinfo
        for name in _LOOKUPS:
            _orig[name] = getattr(socket, name)
            setattr(socket, name, _guard_lookup(name))
        socket.socket.connect = _guarded_connect
        socket.socket.connect_ex = _guarded_connect_ex
        socket.getaddrinfo = _guarded_getaddrinfo
        socket.getnameinfo = _guarded_getnameinfo
        _installed = True


def uninstall():
    """Restore the original socket functions (used by tests)."""
    global _installed
    with _lock:
        if not _installed:
            return
        socket.socket.connect = _orig["connect"]
        socket.socket.connect_ex = _orig["connect_ex"]
        socket.getaddrinfo = _orig["getaddrinfo"]
        socket.getnameinfo = _orig["getnameinfo"]
        for name in _LOOKUPS:
            setattr(socket, name, _orig[name])
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
