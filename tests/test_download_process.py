# The download process (#111 slice A4): every HTTP request and redirect is
# checked against the host allowlist (needed behind a proxy, where this
# computer never looks up the target name), the setup that must happen before
# huggingface_hub is imported, and plain-language errors. No network is used.
#   python -m unittest discover -s tests -v

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import offline_guard as og  # noqa: E402

HAVE_DOWNLOAD_LIBS = all(importlib.util.find_spec(m) for m in ("huggingface_hub", "truststore",
                                                                 "httpx"))


class FakeURL:
    def __init__(self, scheme, host):
        self.scheme, self.host = scheme, host


class FakeRequest:
    def __init__(self, url):
        scheme, rest = url.split("://", 1)
        self.url = FakeURL(scheme, rest.split("/", 1)[0])


class RequestCheckTests(unittest.TestCase):
    def setUp(self):
        self.saved = (og._allowed_names, og._proxy_names, set(og._allowed_addresses))
        from workspace import setup
        self.setup = setup
        with mock.patch("urllib.request.getproxies", return_value={}):
            og.allow_hosts(setup.DOWNLOAD_HOSTS)

    def tearDown(self):
        og._allowed_names, og._proxy_names = self.saved[0], self.saved[1]
        og._allowed_addresses.clear()
        og._allowed_addresses.update(self.saved[2])

    def test_listed_hosts_over_https_pass(self):
        for url in ("https://huggingface.co/api/models/x", "https://us.aws.cdn.hf.co/f",
                    "https://download-r2.pytorch.org/whl/cu130/x.whl"):
            self.setup._check_request(FakeRequest(url))

    def test_other_hosts_plain_http_and_addresses_are_refused(self):
        for url in ("https://evil.example/x", "http://huggingface.co/x",
                    "https://huggingface.co.evil.example/x", "https://18.239.50.7/x",
                    "https://download.pytorch.org.evil.example/x"):
            with self.assertRaises(og.NetworkBlockedError, msg=url):
                self.setup._check_request(FakeRequest(url))

    def test_a_refused_address_has_its_own_message(self):
        try:
            try:
                raise og.NetworkBlockedError(13, "blocked")
            except OSError as inner:
                raise RuntimeError("httpx wrapper") from inner
        except RuntimeError as outer:
            self.assertEqual(self.setup._friendly(outer), self.setup.BLOCKED_MESSAGE)
        self.assertNotEqual(self.setup._friendly(TimeoutError("timeout")),
                            self.setup.BLOCKED_MESSAGE)


CHILD = r"""
import json, ssl, sys
sys.path.insert(0, sys.argv[1])
from workspace import setup
setup.prepare_download_process()
import httpx
import offline_guard
from huggingface_hub import constants
from huggingface_hub.utils import _http

client = _http.get_session()
hooks = [getattr(h, "__name__", "") for h in client.event_hooks["request"]]

def redirect_elsewhere(request):
    if request.url.host == "huggingface.co":
        return httpx.Response(302, headers={"Location": "https://evil.example/payload"})
    return httpx.Response(200, content=b"should never be reached")

mock_client = httpx.Client(transport=httpx.MockTransport(redirect_elsewhere),
                           event_hooks=client.event_hooks, follow_redirects=True)
try:
    mock_client.get("https://huggingface.co/model/resolve/main/file.bin")
    redirect = "followed"
except offline_guard.NetworkBlockedError:
    redirect = "refused"
print(json.dumps({
    "guard": offline_guard._installed,
    "allowed": list(offline_guard._allowed_names),
    "xet_disabled": bool(constants.HF_HUB_DISABLE_XET),
    "xet_loaded": "hf_xet" in sys.modules,
    "truststore": ssl.SSLContext.__module__,
    "hooks": hooks,
    "redirect": redirect,
}))
"""


@unittest.skipUnless(HAVE_DOWNLOAD_LIBS, "download libraries not installed")
class PrepareDownloadProcessTests(unittest.TestCase):
    def test_setup_before_huggingface_hub_is_used(self):
        r = subprocess.run([sys.executable, "-c", CHILD, str(ROOT)], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(got["guard"])
        self.assertEqual(got["allowed"], ["huggingface.co", "hf.co", "download-r2.pytorch.org"])
        self.assertTrue(got["xet_disabled"])
        self.assertFalse(got["xet_loaded"])
        self.assertTrue(got["truststore"].startswith("truststore"), got["truststore"])
        self.assertIn("_check_request", got["hooks"])
        self.assertEqual(got["redirect"], "refused")


if __name__ == "__main__":
    unittest.main()
