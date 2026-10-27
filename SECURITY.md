# Security policy

## Supported versions

Only the newest pre-release from v0.5.0-preview.4 onward, on the [releases page](https://github.com/thatskiff33/unminute/releases), receives security fixes. Older versions do not: v0.4.3 was built before the security hardening tracked in #111, and v0.5.0-preview.2 and v0.5.0-preview.3 have only part of it.

The Summary of the threat model, [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#summary), lists what is protected today and what is still planned.

## Reporting a vulnerability

Please report privately. Do not open a public issue with details.

1. On this repository's Security tab, choose "Report a vulnerability" (GitHub private vulnerability reporting).
2. If that option is not shown, open a public issue titled "Security contact request" with no technical details, and a maintainer will arrange a private channel.

Include the version (from Settings, or `version.json` in the install folder), what an attacker needs, and the steps to reproduce. Use synthetic recordings only. Never send real meeting audio, transcripts or tokens.

## What to expect

- We aim to acknowledge a report within 5 working days (New Zealand time).
- An assessment against the threat model in [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md), and a fix plan for confirmed issues.
- Credit in the release notes if you would like it.

## Scope

In scope: Unminute, its private runtime and installer, model download and verification, and the build and release workflows in this repository.

Out of scope:

- Windows, WebView2 and third-party packages themselves. Report those upstream; where we pin a package, we will update the pin.
- Attacks that need administrator rights on the device, or code already running as the signed-in user (see "What the controls do not cover" in the threat model).
