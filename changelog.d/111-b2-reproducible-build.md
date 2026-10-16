Security hardening (#111, slice B2):

- **The package says what is in it.** It now includes a software bill of materials (`sbom.cdx.json`, also published beside the zip), third-party notices, a list of every executable file with its SHA-256 and signer, and a manifest of the pinned model files for checking a copy staged by IT.
- **Reproducible release builds.** Building the same release twice gives the same zip, byte for byte, and a release can only be built from its tagged commit with no local changes.
