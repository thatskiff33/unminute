# Validation runbook: checks that need people or hardware

These are the checks still open before a business release. Each one needs a machine, an organisation or people that the development laptop cannot supply. Run them against a published package. The validation tracker #189 groups them into sittings; record each result there, under the sitting named, using anonymous, aggregate figures only.

**Get the package and tools:**
- From the release page, download `Unminute-<version>.zip`, its `.sha256` file and "Source code (zip)". `tools/intune/Get-VerifiedRelease.ps1 -Tag v<version>` downloads the package and checks its signature (docs/PACKAGING.md, "Verifying a release before packaging").
- The source zip contains `tools/`, which holds the evidence scripts.
- The scripts run with the installed app's own Python runtime, so nothing else needs installing.

**Paths used below:**
- `<install>` is `%LOCALAPPDATA%\Programs\Unminute\versions\<version>`.
- Models live in `%LOCALAPPDATA%\Unminute\models`, after Setup has downloaded them.
- Use synthetic or consented, non-client audio only.

## 1. Clean standard-user profile (#189, sitting 3)

**Machine:** a Windows 10 (1809 or later) or Windows 11 x64 device. Sign in with a standard (non-admin) account that has never had Python, Git or developer tools.

1. Install with `install.cmd`, or with `powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Package Unminute-<version>.zip`. Expect exit code 0 and no administrator prompt.
2. Open **Unminute** from the Start menu. Complete Setup: download Parakeet v2, and optionally speaker identification with a Hugging Face token.
3. Import a synthetic recording, review it, and confirm review and then the final save.
4. Run `<install>\runtime\python.exe -I -B -m workspace --self-test` from `<install>\app`. Expect `PASS`.
5. Uninstall from Settings > Apps. Check that `%LOCALAPPDATA%\Programs\Unminute` is gone, and that the workspace and models are kept, as documented.

**Record:** the Windows version, whether each step passed, and any prompt or error text.

## 2. Managed deployment with Intune (#189, sitting 3)

Follow `docs/PACKAGING.md`, "Managed deployment (Intune)": a Win32 app in user context, `install.ps1 -Silent -Managed`, the registry detection rule, and exit code 5 mapped to Retry.

1. Assign the app to a test device and user, and check that detection reports it as installed.
2. Open the app and check that Settings shows "Updates are managed by your IT team".
3. Deploy the next version. With a meeting under a hold, the install waits (exit 5). After the hold is released and cleanup is confirmed, it installs.
4. Uninstall through Intune.
5. On the Intune-managed device, check `--health` as a Remediation script, and the crash-dump settings (SECURITY_MODEL.md T21).

**Signing route:** choose one of the options in `docs/PACKAGING.md`, "Signing and trust", and record the decision on #189 (sitting 7).

## 3. Lowest-supported laptop and the release performance run (#189, sitting 2)

There is no named reference laptop (owner decision, 18 October 2026, #121). The release performance run is on whichever Windows machine prepares the release, and records that machine's specification. The lowest-supported run below is for information.

**Define the floor first.** Agree the lowest-supported specification, for example 4 cores and 8 GB of memory, CPU only. Record it on #189: it becomes the documented minimum.

From the extracted source folder, using the installed runtime:

```
<install>\runtime\python.exe tools\packaged_check.py <install> %LOCALAPPDATA%\Unminute\models <15-minute synthetic recording.wav> packaged_check.json
<install>\runtime\python.exe tools\packaged_crash_check.py <install> %LOCALAPPDATA%\Unminute\models <recording.wav> crash_check.json 240
powershell -NoProfile -ExecutionPolicy Bypass -File tools\import_dialog_check.ps1 -Version <install> -Wav <recording.wav>
```

- Expect `"pass": true` from both checks and from the dialog check. Each report lists the criteria that failed under `"failed"`.
- Add `--zip <the zip installed>` to make a report release evidence: it then names the zip and checks that the version folder still holds exactly its files (`tools/evidence.py`; docs/RELEASING.md, step 3).
- Do not copy anything to the clipboard during the crash check; it compares the clipboard sequence number.
- From `packaged_check.json`, record:
  - the transcription and speaker time against the recording length;
  - the worker's peak memory;
  - idle CPU and memory;
  - no marker text found, and no backups.
- Note whether the laptop stayed usable during processing in Quiet mode.
- Also in this sitting:
  - the first launch after an install;
  - a full `tools/perf_check.py` release run against the package (`--app`) on the machine preparing the release (docs/RELEASING.md, step 3), with its specification put in the release notes (`python -I tools\evidence.py machine evidence-perf.json`), and the cost of the GPU check;
  - the GPU failure cases;
  - the packaged build's crash and hang records.

## 4. Representative transcription quality (#189, sitting 4)

1. Collect consented business recordings, with each person's agreement, outside any client matter. Cover the expected range: accents including New Zealand English, meeting sizes, Teams recordings and in-room microphones.
2. Have a person write a reference transcript for each one, or for a sampled excerpt.
3. Write a JSON Lines manifest (`{"set", "id", "wav", "ref"}`), then run `tools/compare_engines.py run` once per engine, followed by `score`. The header of the file shows the usage.
4. Publish only the aggregate figures: word error rate per set and per engine, and speed. No transcripts, names or audio.

## 5. Accessibility and pilot users (#189, sitting 5)

**Screen reader:**
1. With NVDA or Narrator, complete import, review, naming speakers, handoff and both cleanup confirmations by keyboard only.
2. Repeat with a Windows contrast theme turned on.
3. Note anything unlabelled, unannounced or unreadable.
4. Check the WCAG 2.2 AA criteria the automated check leaves out, listed in [RELEASE_STANDARD.md](RELEASE_STANDARD.md), "Accessibility against WCAG 2.2 AA". Include a recording in progress and the confirmation windows the app draws outside the page (recording, deletion authority, discard). Record each criterion as passed, failed or not checked.

**Pilot users:**
1. Recruit at least three nontechnical people, each on a standard-user profile.
2. Give each person one synthetic recording and the written task: "Transcribe this meeting, name the speakers, copy the minutes prompt, then clear it from the workspace."
3. Do not coach. Note where people hesitate, any errors they see, and whether they finish.
4. Record findings without personal details.

**Quick checks for the same session:**
- dragging a recording onto the window (#20);
- pressing Ctrl+plus to zoom;
- the handoff check listed on #189.

## 6. Microphone recording on real hardware (#30)

Turn on Settings > Recording and speakers > Record with a microphone. Use a USB or Bluetooth headset that can be unplugged or switched off.

1. Start a recording, speak for 20 seconds, then unplug the headset or switch it off.
2. Within a few seconds, the panel should say the microphone stopped responding and that recording stopped without switching device. The saved part should become a meeting.
3. Check that the meeting's length matches the time before the unplug. If recording continued with silence instead, record that on #30: it means the device-loss check needs a silence-based rule for this driver.
4. Optional: during a recording, end the app in Task Manager. At the next start, the saved part is imported, and the report gives about how much at the end was lost.

## 7. Laptop sleep, lid and timers (#189, sitting 1)

On a laptop, with a synthetic recording or an idle workspace:
- the scan of watched folders after the laptop wakes (within about 60 s);
- closing the app from the taskbar while it is minimised;
- a long idle session;
- that the app's timers count time asleep;
- the lid and sleep checks listed on #189 (recording with the lid closed, if that setting is in the release);
- uninstalling puts the lid setting back (#191): with **Keep recording when the lid is closed** on, start a recording, end the app in Task Manager, check that Windows Settings > System > Power & battery > Lid & power button controls shows "Do nothing", then uninstall without starting the app again. The setting is back as it was, and the uninstaller showed no warning. Note the setting first, and put it back by hand if the check fails.

## After the checks

Update `docs/RELEASE_CHECKLIST.md` and record each sitting's results on #189; it closes when every sitting is done, and the checklist's open items and release readiness are sitting 7. A full release then follows `docs/RELEASING.md`.
