Security hardening (#111, slice B1):

- **Every package in the app is hash-locked.** The package build installs only the locked set and refuses any file that does not match its pinned SHA-256, and a daily audit checks the locked packages for known vulnerabilities. The installed packages and versions are unchanged.
