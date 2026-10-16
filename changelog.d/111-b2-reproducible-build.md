Security hardening (#111, slice B2):

- **The package says what is in it.** It now includes a software bill of materials (`sbom.cdx.json`, also published beside the zip), third-party notices that also list the native libraries each component bundles, a list of every executable file with its SHA-256 and signature details, and a manifest of the pinned model files for checking a copy staged by IT.
- **Reproducible release builds.** Building the same release twice with the same tools gives the same zip, byte for byte. A release is built only from the committed files of its tagged commit, with no local changes, and any new executable file or changed signer stops the build until it is reviewed.
