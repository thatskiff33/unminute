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
        self.raw_host = host.encode("ascii")       # httpx: the exact bytes sent


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

    def test_a_proxy_refusal_is_not_mistaken_for_model_terms(self):
        class ProxyError(Exception):          # stands in for httpx.ProxyError
            pass
        try:
            try:
                raise ProxyError("403 Forbidden")
            except ProxyError as inner:
                raise RuntimeError("download failed") from inner
        except RuntimeError as outer:
            self.assertEqual(self.setup._friendly(outer), self.setup.PROXY_MESSAGE)

    def test_setup_failure_is_reported_not_raised(self):
        sent = []

        class Pipe:
            def send(self, message):
                sent.append(message)

            def recv(self):
                raise AssertionError("must not read the token after a failed setup")
        with mock.patch.object(self.setup, "prepare_download_process",
                               side_effect=ImportError("no truststore")):
            self.setup._download_main(Pipe(), "parakeet-tdt-0.6b-v2", ".")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "error")
        self.assertNotIn("truststore", sent[0][1])

    def test_the_pypi_host_is_allowed_only_for_the_whisper_component(self):
        # #248: the owner allowed files.pythonhosted.org for this one component.
        import asr_engines
        from workspace import gpu
        wheel = "https://files.pythonhosted.org/packages/70/61/x/nvidia_cublas_cu12.whl"
        for name in (*asr_engines.MANAGED_MODELS, gpu.COMPONENT):
            self.assertEqual(self.setup.download_hosts(name), self.setup.DOWNLOAD_HOSTS, name)
        with self.assertRaises(og.NetworkBlockedError):
            self.setup._check_request(FakeRequest(wheel))          # the default process
        self.assertEqual(self.setup.download_hosts(gpu.WHISPER_COMPONENT),
                         (*self.setup.DOWNLOAD_HOSTS, "files.pythonhosted.org"))
        with mock.patch("urllib.request.getproxies", return_value={}):
            og.allow_hosts(self.setup.download_hosts(gpu.WHISPER_COMPONENT))
        self.setup._check_request(FakeRequest(wheel))
        for url in ("https://pypi.org/simple/", "http://files.pythonhosted.org/packages/x",
                    "https://files.pythonhosted.org.evil.example/x"):
            with self.assertRaises(og.NetworkBlockedError, msg=url):
                self.setup._check_request(FakeRequest(url))

    def test_each_download_process_sets_up_its_own_hosts(self):
        from workspace import gpu
        seen = []

        class Pipe:
            def send(self, message):
                pass

        def prepare(hosts):
            seen.append(hosts)
            raise ImportError("stop here")
        with mock.patch.object(self.setup, "prepare_download_process", side_effect=prepare):
            for name in ("parakeet-tdt-0.6b-v2", gpu.COMPONENT, gpu.WHISPER_COMPONENT):
                self.setup._download_main(Pipe(), name, ".")
        self.assertEqual(seen, [self.setup.DOWNLOAD_HOSTS, self.setup.DOWNLOAD_HOSTS,
                                self.setup.download_hosts(gpu.WHISPER_COMPONENT)])

    def test_a_component_download_stays_on_its_own_location(self):
        from workspace import gpu
        whisper = self.setup._fetcher(gpu.WHISPER_DOWNLOAD_HOST)
        for fetch, url in ((whisper, "https://download-r2.pytorch.org/whl/cu130/torch.whl"),
                           (whisper, "https://files.pythonhosted.org/simple/x.whl"),
                           (self.setup._fetch, gpu.WHISPER_WHEELS[0]["url"]),
                           (self.setup._fetcher("http://files.pythonhosted.org/packages/"),
                            "http://files.pythonhosted.org/packages/x.whl")):
            with self.assertRaises(self.setup.SetupError, msg=url):
                fetch(url, "unused", 1)
        redirect = self.setup._SameHost(gpu.WHISPER_DOWNLOAD_HOST)
        for target in ("https://files.pythonhosted.org.evil.example/packages/x",
                       "http://files.pythonhosted.org/packages/x",
                       "https://download-r2.pytorch.org/whl/cu130/x.whl"):
            with self.assertRaises(self.setup.SetupError, msg=target):
                redirect.redirect_request(None, None, 302, "Found", {}, target)

    def test_host_names_must_be_plain_ascii(self):
        for host in (b"huggingface.co\xff", "huggingface.co\x85", "hugging\u212aface.co",
                     "huggingface.co%x.evil.example"):
            self.assertFalse(og.host_allowed(host), repr(host))
        self.assertTrue(og.host_allowed(b"huggingface.co"))


CHILD = r"""
import json, ssl, sys
sys.path.insert(0, sys.argv[1])
from workspace import setup
if len(sys.argv) > 2:          # one component's own process (#248)
    setup.prepare_download_process(setup.download_hosts(sys.argv[2]))
else:
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


class ModelDownloaderTests(unittest.TestCase):
    def test_gated_gets_the_given_token_public_never_and_none_is_never_implicit(self):
        import types

        import asr_engines
        from workspace import setup
        calls = []
        fake = types.ModuleType("huggingface_hub")
        fake.snapshot_download = lambda repo, **kw: calls.append((repo, kw["token"]))
        public = asr_engines.WHISPER_MODELS["large-v3-turbo"]
        with mock.patch.dict(sys.modules, {"huggingface_hub": fake}):
            setup.model_downloader("hf_example")(asr_engines.SPEAKER_MODEL, "unused")
            setup.model_downloader("hf_example")(public, "unused")
            setup.model_downloader(None)(asr_engines.SPEAKER_MODEL, "unused")
        # False, never None: huggingface_hub would otherwise look for a token
        # in the environment or its token file.
        self.assertEqual(calls, [(asr_engines.SPEAKER_MODEL.repo, "hf_example"),
                                 (public.repo, False),
                                 (asr_engines.SPEAKER_MODEL.repo, False)])


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

    def test_the_whisper_component_process_adds_only_its_own_host(self):
        from workspace import gpu
        r = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), gpu.WHISPER_COMPONENT],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(got["guard"])
        self.assertEqual(got["allowed"], ["huggingface.co", "hf.co", "download-r2.pytorch.org",
                                          "files.pythonhosted.org"])
        self.assertEqual(got["redirect"], "refused")


if __name__ == "__main__":
    unittest.main()
