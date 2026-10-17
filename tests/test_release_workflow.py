# tests/test_release_workflow.py
# The CI-built, attested release (#111 B3; .github/workflows/release.yml) and
# the IT verification script (tools/intune/Get-VerifiedRelease.ps1) agree with
# each other and with the build and evidence tools, and keep their safeguards:
# least-privilege jobs, the owner's approval before anything is public, the
# attestation checks and the release evidence check.

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_package  # noqa: E402
import evidence  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
VERIFY = ROOT / "tools" / "intune" / "Get-VerifiedRelease.ps1"


def read(path):
    return Path(path).read_text(encoding="utf-8")


def jobs():
    """Each job's text, by job id (the two-space-indented keys under jobs:)."""
    text = read(WORKFLOW).split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):\n", text)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def permissions(job_text):
    block = re.search(r"(?m)^    permissions:\n((?:      .+\n)+)", job_text)
    return dict(re.findall(r"(?m)^      ([a-z-]+): (\w+)", block.group(1))) if block else None


class WorkflowTests(unittest.TestCase):
    def test_the_jobs_run_in_order_with_least_privilege(self):
        j = jobs()
        self.assertEqual(["check", "build", "draft", "publish"], list(j))
        self.assertIn("\npermissions: {}\n", read(WORKFLOW))
        self.assertEqual({"contents": "read"}, permissions(j["check"]))
        self.assertEqual({"contents": "read", "id-token": "write", "attestations": "write"},
                         permissions(j["build"]))
        self.assertEqual({"contents": "write", "attestations": "read"}, permissions(j["draft"]))
        self.assertEqual({"contents": "write", "attestations": "read"}, permissions(j["publish"]))
        self.assertIn("    needs: check\n", j["build"])
        self.assertIn("    needs: [check, build]\n", j["draft"])
        self.assertIn("    needs: [build, draft]\n", j["publish"])

    def test_only_a_tag_starts_it_and_only_after_approval_is_anything_public(self):
        text = read(WORKFLOW)
        self.assertRegex(text, r'(?m)^on:\n  push:\n    tags: \["v\*"\]\n\n')
        self.assertNotIn("workflow_dispatch", text)
        j = jobs()
        self.assertIn("    environment: release\n", j["publish"])
        self.assertNotIn("environment:", j["draft"] + j["build"] + j["check"])
        self.assertIn("--draft --verify-tag", j["draft"])
        self.assertIn("--draft=false", j["publish"])
        self.assertNotIn("--draft=false", j["draft"])

    def test_every_action_is_pinned_and_no_checkout_keeps_its_token(self):
        text = read(WORKFLOW)
        uses = re.findall(r"uses: (\S+)", text)
        self.assertTrue(uses)
        for use in uses:
            self.assertRegex(use, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
        self.assertEqual(text.count("actions/checkout@"), text.count("persist-credentials: false"))
        # Event values reach scripts through env:, never ${{ }} inside run:.
        blocks = re.findall(r"(?ms)^        run: [|>]\n(.*?)(?=^      - |^  \w|\Z)", text)
        blocks += re.findall(r"(?m)^        run: ([^|>].*)$", text)
        self.assertGreater(len(blocks), 10)              # the patterns still find them
        for block in blocks:
            self.assertNotIn("${{", block)

    def test_the_build_is_the_release_build_with_its_checks(self):
        build = jobs()["build"]
        self.assertIn("runs-on: windows-2025", build)
        self.assertIn(f'python-version: "{build_package.PYTHON_VERSION}"', build)
        self.assertIn("python -I tools/build_package.py --release", build)
        self.assertIn("-I -B -X disable-remote-debug -m workspace --self-test", build)
        self.assertIn("tools/build_package.py --verify-precompiled", build)
        self.assertNotIn("cache:", build)                    # nothing restored from a cache
        self.assertEqual(2, build.count("uses: actions/attest@"))
        self.assertIn("sbom-path:", build)

    def test_the_audit_and_the_tag_checks_come_first(self):
        check = jobs()["check"]
        for step in ("git merge-base --is-ancestor", "does not match VERSION",
                     "requirements/ci-audit.txt", "python tools/audit.py", "notes.md"):
            self.assertIn(step, check)

    def test_publishing_checks_the_draft_attestations_and_evidence(self):
        publish = jobs()["publish"]
        for kind in evidence.KINDS:
            self.assertIn(f"evidence-{kind}.json", publish)
        for name in ('"$NAME.zip"', '"$NAME.zip.sha256"', '"$NAME.cdx.json"',
                     '"$NAME.attestations.jsonl"'):
            self.assertIn(name, publish)
        self.assertIn("diff expected.txt found.txt", publish)       # nothing added, nothing missing
        self.assertIn('= "$BUILT"', publish)                          # the zip that was built
        self.assertIn('= "$SBOM"', publish)
        for flag in ('--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release.yml"',
                     '--source-ref "refs/tags/$TAG"', '--source-digest "$GITHUB_SHA"',
                     "--deny-self-hosted-runners", "https://slsa.dev/provenance/v1",
                     "https://cyclonedx.org/bom"):
            self.assertIn(flag, publish)
        self.assertIn('python tools/evidence.py check "$BUILT" release/evidence-*.json', publish)
        # The checks come before publishing.
        self.assertLess(publish.index("tools/evidence.py check"), publish.index("--draft=false"))
        self.assertLess(publish.index("attestation verify"), publish.index("--draft=false"))

    def test_the_tag_shape_is_the_same_everywhere(self):
        workflow = re.search(r"grep -Eq '(\^v[^']+\$)'", read(WORKFLOW)).group(1)
        script = re.search(r"ValidatePattern\('(\^v[^']+\$)'\)", read(VERIFY)).group(1)
        self.assertEqual(r"^v[0-9]+\.[0-9]+\.[0-9]+(-preview\.[0-9]+)?$", workflow)
        self.assertEqual(workflow, script)
        self.assertEqual(build_package.VERSION_SHAPE.pattern,
                         workflow.removeprefix("^v").removesuffix("$"))


class VerificationScriptTests(unittest.TestCase):
    def test_it_checks_what_the_release_workflow_signs(self):
        script = read(VERIFY)
        for name in ('"$name.zip"', '"$name.zip.sha256"', '"$name.cdx.json"',
                     '"$name.attestations.jsonl"'):
            self.assertIn(name, script)
        for flag in ("--bundle $bundle", "--signer-workflow $workflow",
                     '--source-ref "refs/tags/$Tag"', "--deny-self-hosted-runners",
                     "'https://slsa.dev/provenance/v1', 'https://cyclonedx.org/bom'"):
            self.assertIn(flag, script)
        self.assertIn('$workflow = "$Repository/.github/workflows/release.yml"', script)
        self.assertIn("if ($release.draft)", script)
        self.assertIn("$release.prerelease -and -not $AllowPrerelease", script)

    def test_the_repository_is_named_once_and_is_this_one(self):
        # The signer identity names the repository, so a move to another owner
        # changes this default (and nothing else in the workflow, which uses
        # GITHUB_REPOSITORY). In CI this fails until the default follows a move.
        script = read(VERIFY)
        default = re.search(r"\[string\]\$Repository = '([^']+)'", script).group(1)
        self.assertRegex(default, r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
        self.assertEqual(1, script.count(default))
        self.assertNotIn(default, read(WORKFLOW))
        if os.environ.get("GITHUB_REPOSITORY"):
            self.assertEqual(os.environ["GITHUB_REPOSITORY"], default)

    def test_it_is_plain_ascii(self):
        # Windows PowerShell 5.1 reads a file without a byte-order mark as ANSI.
        self.assertTrue(VERIFY.read_bytes().isascii())


if __name__ == "__main__":
    unittest.main()
