# Security policy

## Supported versions

Only the latest release marked **Latest** on the [releases page](https://github.com/thatskiff33/unminute/releases) receives security fixes. Pre-releases and older versions do not.

## Reporting a vulnerability

Please report privately. Do not open a public issue with details.

1. On this repository's **Security** tab, choose **Report a vulnerability** (GitHub private vulnerability reporting).
2. If that option is not shown, open a public issue titled "Security contact request" with no technical details, and a maintainer will arrange a private channel.

Include the version (from Settings, or `version.json` in the install folder), what an attacker needs, and the steps to reproduce. Use synthetic recordings only. Never send real meeting audio, transcripts or tokens.

## What to expect

- An acknowledgement within 5 working days (New Zealand time).
- An assessment against the threat model in [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md), and a fix plan for confirmed issues.
- Credit in the release notes if you would like it.

## Scope

In scope: the Meeting Workspace app, its private runtime and installer, model download and verification, and the build and release workflows in this repository.

Out of scope: Windows, WebView2 and third-party packages themselves (report those upstream; we will update the pinned version), and attacks that need administrator rights on the device.
