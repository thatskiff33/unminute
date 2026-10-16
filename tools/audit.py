# tools/audit.py
# Known-vulnerability audit of every hash-locked requirements file (#111 B1),
# with pip-audit (from requirements/ci-audit.txt). Run on every pull request
# and daily by .github/workflows/audit.yml.
#
#   python tools/audit.py
#
# A finding that cannot be fixed yet may be ignored for a limited time in
# requirements/audit-ignore.json: each entry names the vulnerability ID, the
# package, why it does not apply or cannot be fixed yet, and an "until" date
# no more than 90 days ahead. An expired entry fails the audit, so every
# ignore is looked at again.

import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORES = ROOT / "requirements" / "audit-ignore.json"
LOCKS = ["requirements.txt", "requirements/ci-lock.txt", "requirements/ci-audit.txt",
         "requirements/ci.txt", "requirements/ci-coverage.txt"]
MAX_IGNORE_DAYS = 90
FIELDS = ("id", "package", "reason", "until")


def active_ignores(entries, today):
    """IDs to ignore, or SystemExit naming the entries that are invalid or expired."""
    problems, ids = [], []
    for e in entries:
        if not isinstance(e, dict) or set(e) != set(FIELDS) or not all(
                isinstance(e[k], str) and e[k].strip() for k in FIELDS):
            problems.append(f"{e!r}: needs exactly {', '.join(FIELDS)}")
            continue
        try:
            until = datetime.date.fromisoformat(e["until"])
        except ValueError:
            problems.append(f"{e['id']}: 'until' is not a YYYY-MM-DD date")
            continue
        if until < today:
            problems.append(f"{e['id']} ({e['package']}): ignored until {e['until']}, "
                            f"now expired. Fix it or renew the entry with a reason.")
        elif (until - today).days > MAX_IGNORE_DAYS:
            problems.append(f"{e['id']}: 'until' is more than {MAX_IGNORE_DAYS} days ahead")
        else:
            ids.append(e["id"])
    if problems:
        sys.exit("requirements/audit-ignore.json:\n  " + "\n  ".join(problems))
    return ids


def main():
    entries = json.loads(IGNORES.read_text(encoding="utf-8"))
    ids = active_ignores(entries, datetime.date.today())
    # --strict: a package that cannot be looked up (as when PyPI takes down a
    # malicious release) fails the audit instead of being skipped.
    cmd = [sys.executable, "-m", "pip_audit", "--require-hashes", "--disable-pip", "--strict",
           "--progress-spinner", "off", "--desc", "on", "--aliases", "on"]
    for vid in ids:
        cmd += ["--ignore-vuln", vid]
    # One file at a time: the locks may pin the same package at different versions.
    failed = []
    for path in LOCKS:
        print(f"== {path}", flush=True)
        if subprocess.run(cmd + ["-r", path], cwd=ROOT).returncode:
            failed.append(path)
    if failed:
        print("Known vulnerabilities (or an audit error) in: " + ", ".join(failed))
        return 1
    print("No known vulnerabilities in any lock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
