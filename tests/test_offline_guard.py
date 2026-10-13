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

    def test_recorded_attempts_are_bounded(self):
        for i in range(og.MAX_RECORDED + 20):
            with self.assertRaises(og.NetworkBlockedError):
                socket.getaddrinfo(f"host{i}.example", 443)
        self.assertEqual(len(og.blocked), og.MAX_RECORDED)
        self.assertEqual(og.blocked[-1][0], f"host{og.MAX_RECORDED + 19}.example")


if __name__ == "__main__":
    unittest.main()
