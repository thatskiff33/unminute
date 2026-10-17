Security hardening (#111, slice B3):

- **Releases are built by GitHub, with signed build provenance.** Each package is built from the tagged code on GitHub's own servers, with a signed record of how it was built and a signed software bill of materials, and is published only after the owner approves it. The zip itself is still not code-signed. A release also has to pass the performance, crash, lifecycle, window and accessibility checks, run against that exact package. IT can check a download before packaging it with `tools/intune/Get-VerifiedRelease.ps1`.
