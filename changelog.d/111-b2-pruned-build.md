Security hardening (#111, slice B2):

- **A smaller, precompiled package.** The package leaves out what Unminute never uses: three packages (grpcio, pyannoteai-sdk, setuptools), the test suites inside packages, files only needed to build software, and binaries for other processors. About 15,000 fewer files and 17 fewer programs and libraries. Every module now ships precompiled, so Unminute writes nothing into its own folder when it runs.
