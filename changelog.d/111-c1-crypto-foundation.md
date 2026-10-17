Security hardening (#111, slice C1):

- **The package now includes the cryptography library, with its own copy of OpenSSL, ready for encryption at rest.** Nothing uses it yet: a later update encrypts meeting content with it (`docs/ENCRYPTION.md`). IT will see it in the bill of materials, the third-party notices and the binary list.
