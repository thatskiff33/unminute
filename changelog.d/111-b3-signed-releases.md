Security hardening (#111, slice B3):

- **Releases are built and signed by GitHub.** Each package is built from the tagged code on GitHub's own servers, with signed build provenance and a software bill of materials, and is published only after the owner approves it. The release also has to pass the performance, crash, lifecycle, window and accessibility checks, run against that exact package. IT can check a download before packaging it with `tools/intune/Get-VerifiedRelease.ps1`.
