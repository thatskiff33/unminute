# Tests for offline_guard: telemetry switches and the socket-level network
# guard. Stdlib only and never touches the real network: blocked attempts are
# refused before any packet is sent, and permitted lookups use numeric IPs.
#   python -m unittest discover -s tests -v

import errno
import os
import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import offline_guard as og  # noqa: E402

TEST_NET = "192.0.2.1"   # RFC 5737 documentation address, never routable


class TelemetrySwitchTests(unittest.TestCase):
    def test_switches_are_forced_off_even_if_the_user_enabled_them(self):
        saved = {k: os.environ.get(k) for k in og.TELEMETRY_SWITCHES}
        try:
            os.environ["PYANNOTE_METRICS_ENABLED"] = "true"
            os.environ["OTEL_SDK_DISABLED"] = "false"
            og.disable_telemetry()
            self.assertEqual(os.environ["PYANNOTE_METRICS_ENABLED"], "false")
            self.assertEqual(os.environ["OTEL_SDK_DISABLED"], "true")
            self.assertEqual(os.environ["HF_HUB_DISABLE_TELEMETRY"], "1")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class NetworkGuardTests(unittest.TestCase):
    def setUp(self):
        og.install()
        og.blocked.clear()

    def tearDown(self):
        og.uninstall()

    def test_dns_lookup_of_a_remote_host_is_refused_and_recorded(self):
        with self.assertRaises(og.NetworkBlockedError):
            socket.getaddrinfo("otel.pyannote.ai", 443)
        self.assertEqual(og.blocked, [("otel.pyannote.ai", 443)])

    def test_connection_to_a_remote_address_is_refused(self):
        s = socket.socket()
        try:
            with self.assertRaises(og.NetworkBlockedError):
                s.connect((TEST_NET, 443))
            self.assertEqual(s.connect_ex((TEST_NET, 80)), errno.EACCES)
        finally:
            s.close()
        self.assertEqual(og.blocked, [(TEST_NET, 443), (TEST_NET, 80)])

    def test_create_connection_is_refused(self):
        with self.assertRaises(OSError):
            socket.create_connection(("huggingface.co", 443), timeout=1)
        self.assertIn(("huggingface.co", 443), og.blocked)

    def test_loopback_still_works(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        accepted = []
        t = threading.Thread(target=lambda: accepted.append(server.accept()[0]))
        t.start()
        try:
            client = socket.create_connection(("localhost", port), timeout=5)
            client.close()
            t.join(5)
            self.assertEqual(len(accepted), 1)
        finally:
            for c in accepted:
                c.close()
            server.close()
        self.assertEqual(og.blocked, [])

    def test_permit_allows_and_is_released_even_on_error(self):
        with og.permit():
            socket.getaddrinfo(TEST_NET, 443)   # numeric: resolved locally
        with self.assertRaises(RuntimeError):
            with og.permit():
                raise RuntimeError("download failed")
        with self.assertRaises(og.NetworkBlockedError):
            socket.getaddrinfo(TEST_NET, 443)

    def test_install_is_idempotent_and_uninstall_restores(self):
        og.install()
        og.uninstall()
        self.assertIs(socket.getaddrinfo, og._orig["getaddrinfo"])
        socket.getaddrinfo(TEST_NET, 443)   # no longer guarded
        self.assertEqual(og.blocked, [])

    def test_legacy_dns_functions_are_guarded(self):
        for fn in (socket.gethostbyname, socket.gethostbyname_ex, socket.gethostbyaddr):
            with self.assertRaises(og.NetworkBlockedError):
                fn("example.com")
        with self.assertRaises(og.NetworkBlockedError):
            socket.getnameinfo((TEST_NET, 443), 0)
        self.assertEqual(socket.gethostbyname("localhost"), "127.0.0.1")

    def test_loopback_proxy_counts_as_remote(self):
        # A proxy on this machine would forward traffic to the internet.
        og.uninstall()
        saved = os.environ.get("HTTPS_PROXY")
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:8899"
        try:
            og.install()
            s = socket.socket()
            try:
                with self.assertRaises(og.NetworkBlockedError):
                    s.connect(("127.0.0.1", 8899))
            finally:
                s.close()
            self.assertIn(("127.0.0.1", 8899), og.blocked)
            with og.permit():           # deliberate downloads may still use the proxy
                self.assertTrue(og._check("127.0.0.1", 8899))
            self.assertTrue(og._check("127.0.0.1", 8900))   # other local ports fine
        finally:
            if saved is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = saved

    def test_recorded_attempts_are_bounded(self):
        for i in range(og.MAX_RECORDED + 20):
            with self.assertRaises(og.NetworkBlockedError):
                socket.getaddrinfo(f"host{i}.example", 443)
        self.assertEqual(len(og.blocked), og.MAX_RECORDED)
        self.assertEqual(og.blocked[-1][0], f"host{og.MAX_RECORDED + 19}.example")


class GuardGapTests(unittest.TestCase):
    """Paths that bypass socket.connect (#111 A4): UDP datagrams and asyncio's
    Windows event loop, which connects with ConnectEx/WSAConnect directly and
    skips DNS for IP literals."""

    def setUp(self):
        og.install()
        og.blocked.clear()

    def tearDown(self):
        og.uninstall()

    def test_udp_datagram_to_a_remote_address_is_refused(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            with self.assertRaises(og.NetworkBlockedError):
                s.sendto(b"x", (TEST_NET, 53))
            with self.assertRaises(og.NetworkBlockedError):
                s.sendto(b"x", 0, (TEST_NET, 53))
        self.assertEqual(og.blocked, [(TEST_NET, 53), (TEST_NET, 53)])

    def test_udp_to_loopback_still_works(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx, \
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            rx.bind(("127.0.0.1", 0))
            tx.sendto(b"hi", rx.getsockname())
            self.assertEqual(rx.recv(10), b"hi")

    def test_asyncio_connection_to_an_ip_literal_is_refused(self):
        import asyncio

        async def attempt():
            await asyncio.wait_for(asyncio.open_connection(TEST_NET, 443), 5)
        with self.assertRaises(og.NetworkBlockedError):
            asyncio.run(attempt())
        self.assertIn((TEST_NET, 443), og.blocked)

    def test_asyncio_udp_to_an_ip_literal_is_refused(self):
        import asyncio

        async def attempt():
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, remote_addr=(TEST_NET, 53))
            transport.close()
        with self.assertRaises(og.NetworkBlockedError):
            asyncio.run(attempt())

    def test_uninstall_restores_asyncio_and_sendto(self):
        og.uninstall()
        self.assertIsNot(socket.socket.sendto, og._guarded_sendto)
        proactor = og._iocp_proactor()
        if proactor is not None:
            self.assertIsNot(proactor.connect, og._guarded_iocp_connect)
        og.install()


class AllowHostsTests(unittest.TestCase):
    """The download process may reach only the listed hosts (and public
    addresses they resolved to) and the configured proxies: a redirect
    elsewhere fails at its DNS lookup, or at host_allowed() for each HTTP
    request when a proxy resolves names."""

    # Public-looking addresses; nothing is ever contacted.
    FAKE = {"huggingface.co": "18.239.50.7", "cdn-lfs.hf.co": "3.167.0.8",
            "rebind.hf.co": "10.0.0.5", "proxy.corp.example": "10.1.2.3"}

    def setUp(self):
        self.saved = (og._allowed_names, og._proxy_names, set(og._allowed_addresses))
        og.install()
        og.blocked.clear()
        self.real = og._orig["getaddrinfo"]

        def fake_resolver(host, port, *args, **kwargs):
            key = str(host).lower().rstrip(".")
            if key in self.FAKE:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.FAKE[key], port))]
            raise AssertionError(f"test tried a real lookup: {host}")
        og._orig["getaddrinfo"] = fake_resolver

    def tearDown(self):
        og._orig["getaddrinfo"] = self.real       # uninstall() puts this back on socket
        og.uninstall()
        og._allowed_names, og._proxy_names = self.saved[0], self.saved[1]
        og._allowed_addresses.clear()
        og._allowed_addresses.update(self.saved[2])

    def allow(self, proxies=None):
        from unittest import mock
        with mock.patch("urllib.request.getproxies", return_value=proxies or {}):
            og.allow_hosts(("huggingface.co", "hf.co"))

    def test_listed_hosts_and_subdomains_resolve_and_connect(self):
        self.allow()
        self.assertEqual(socket.getaddrinfo("huggingface.co", 443)[0][4][0], "18.239.50.7")
        self.assertEqual(socket.getaddrinfo("CDN-LFS.hf.co.", 443)[0][4][0], "3.167.0.8")
        self.assertTrue(og._check("18.239.50.7", 443))
        self.assertTrue(og._check("3.167.0.8", 443))

    def test_other_hosts_lookalikes_and_tricks_are_refused(self):
        self.allow()
        for host in ("evil.example", "huggingface.co.evil.example", "nothf.co",
                     "otel.pyannote.ai", "huggingface.co%x.evil.example",
                     "huggingface.co/evil.example", "[huggingface.co]"):
            with self.assertRaises(og.NetworkBlockedError, msg=host):
                socket.getaddrinfo(host, 443)
            self.assertFalse(og.host_allowed(host), host)
        s = socket.socket()
        try:
            with self.assertRaises(og.NetworkBlockedError):
                s.connect((TEST_NET, 443))         # never resolved from an allowed name
        finally:
            s.close()

    def test_an_allowed_name_resolving_to_a_private_address_is_not_followed(self):
        self.allow()
        socket.getaddrinfo("rebind.hf.co", 443)
        self.assertFalse(og._check("10.0.0.5", 443))

    def test_request_targets_must_be_listed_names(self):
        self.allow()
        self.assertTrue(og.host_allowed("huggingface.co"))
        self.assertTrue(og.host_allowed("us.aws.cdn.hf.co"))
        for host in ("18.239.50.7", "[::1]", "", None, "proxy.corp.example"):
            self.assertFalse(og.host_allowed(host), repr(host))

    def test_proxies_are_allowed_by_exact_name_only(self):
        self.allow({"https": "http://user:secret@proxy.corp.example:8080",
                    "http": "http://192.0.2.44:3128", "no": "localhost"})
        self.assertEqual(socket.getaddrinfo("proxy.corp.example", 8080)[0][4][0], "10.1.2.3")
        self.assertTrue(og._check("10.1.2.3", 8080))       # a proxy may be private
        self.assertTrue(og._check("192.0.2.44", 3128))     # proxy given as an address
        with self.assertRaises(og.NetworkBlockedError):
            socket.getaddrinfo("other.proxy.corp.example", 8080)
        self.assertFalse(og.host_allowed("proxy.corp.example"))

    def test_allow_hosts_rejects_a_bare_string_and_ignores_blanks(self):
        with self.assertRaises(TypeError):
            og.allow_hosts("huggingface.co")
        from unittest import mock
        with mock.patch("urllib.request.getproxies", return_value={}):
            og.allow_hosts(("", ".", "%", "huggingface.co"))
        self.assertEqual(og._allowed_names, ("huggingface.co",))


if __name__ == "__main__":
    unittest.main()
