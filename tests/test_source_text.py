# Source files show what they contain: no invisible or bidirectional control
# characters (for example a right-to-left override, which can make code read
# differently from how it runs). Tests that need such characters write them
# as escapes: a backslash, u, then the code point. Stdlib only.
#   python -m unittest discover -s tests -v

import subprocess
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".ps1", ".cmd", ".yml", ".yaml", ".toml",
            ".json", ".txt", ".adml", ".admx", ".cfg", ".ini"}
ALLOWED = {"\t", "\r", "\n"}


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split("\n")
        return [ROOT / f for f in out if f]
    except (OSError, subprocess.CalledProcessError):     # not a git checkout
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


class SourceTextTests(unittest.TestCase):
    def test_no_invisible_or_bidirectional_characters(self):
        found = []
        for path in tracked_files():
            if path.suffix.lower() not in SUFFIXES or not path.is_file():
                continue
            try:
                text = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.split("\n"), 1):
                for ch in line:
                    if ch not in ALLOWED and unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
                        found.append(f"{path.relative_to(ROOT)}:{n}: U+{ord(ch):04X}")
        self.assertEqual(found, [], "Write these characters as escapes instead")


if __name__ == "__main__":
    unittest.main()
