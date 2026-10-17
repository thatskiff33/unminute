Security hardening (#111, slice B2):

- **A leaner, precompiled package.** The package leaves out what Unminute never uses: three packages (grpcio, pyannoteai-sdk, setuptools), the test suites inside packages, files only needed to build software, and some binaries for other processors: about 15,000 files, 15 of them programs and libraries. Every module now ships precompiled (about 10,000 files), so Unminute writes nothing into its own folder when it runs. The zip has about 22,800 files instead of 27,800, and is about 38 MB larger (375 MB instead of 337 MB) because of the precompiled files.
