# Meeting workspace: target product and delivery scope

This is the public, generic product plan for roadmap [#7](https://github.com/thatskiff33/unminute/issues/7).
It describes planned behaviour, not shipped functionality. The README describes the current app.
Child issue acceptance criteria supply implementation detail; resolve material contradictions before coding.
See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for coding order and review gates.

## Product workflow

The app is a temporary local meeting workspace, not a permanent meeting archive.
Import a recording, transcribe, identify/name speakers and review against audio.
After explicit transcript-review confirmation, durably save reviewed text and clean up
app audio plus specifically authorised intake originals. Hand reviewed Markdown and
an editable prompt to an approved external AI assistant through copy/save. The user
creates, reviews and files the final deliverable outside the app. Only explicit
confirmation that the final output is reviewed and saved triggers transcript cleanup.
Copy/export alone never triggers deletion. The final document need not return to the app.

Initial navigation is Inbox, Active meetings and Settings. Manual import and the
complete review/handoff/cleanup workflow precede folder automation and later recording.
Keep final-document branding/filing external. No direct AI APIs, document-management
integration, system capture, live transcription, remembered speaker names or voiceprints
are required for the first release. Speaker names apply only to the current meeting.

## Desktop, installation and models

Prove pywebview with bundled HTML/CSS/JavaScript, Python and WebView2 before building
the full shell (#10). Verify standard-user packaging, safe bounded playback, long-text
rendering, narrow bridge commands, path/junction validation and the actual listener/network
footprint. Do not assume a local asset path means serverless. No remote executable UI.

Ship one versioned package/private runtime through self-service or managed deployment
(#11). Models remain separately verified downloads. Users need no manual Python, Git,
FFmpeg or GPU tooling. Resolve signing/trust and clean-machine installation in the trial.
Self-service updates are user-initiated; managed updates follow IT policy (#26).

Evaluate Parakeet v3 INT8 against English v2 and the existing Whisper alternative (#12).
Offer one recommended Parakeet model with useful alternatives under Advanced (#13),
without mandatory duplicate downloads. Retain pyannote and guided individual account,
model-terms and token setup. Remember tokens in Windows Credential Manager with a
Remove token action; cached offline processing must still work after removal (#19).
Pin complete Windows dependency sets and model revisions, verify trusted hashes and
licenses, and load-test artifacts before showing Ready (#9).

## Durable processing and resources

Use SQLite outside synced storage, one app instance/state owner, managed heavy workers,
stable source identity, exact sample/chunk ranges, model/configuration revisions and
atomic chunk completion (#14). Keep generated text separate from human edits. Preserve
committed work, repeat only uncommitted work, and test overlap/repeated speech rather than
deduplicating solely by text. Changed sources/models create a separate working version.

One active meeting and one heavy model: transcribe, unload ASR, then separate speakers
(#15/#16). Speaker failure leaves text usable; an interrupted whole-meeting speaker pass
may restart. Acknowledge Pause promptly but report actual safe-boundary progress truthfully.
Explicit pauses stay paused after restart. No recovery after authorised audio deletion.

Quiet defaults use half the logical CPUs, capped at four and minimum one, below-normal
priority and approximately two-minute idle model unloading. Automatic processing waits
on battery unless overridden. Bound buffers/downloads/events and preflight disk space.
Idle targets (under 1% average CPU over five minutes and under 400 MB memory without
models) are unverified targets, not shipped guarantees.

### Optional GPU acceleration

Evaluate NVIDIA acceleration during #9/#12, as an optional measured Faster-mode
capability. It must not gate the first usable CPU release. Parakeet/ONNX Runtime,
Whisper/CTranslate2 and pyannote/PyTorch are separate compatibility paths. Test the
actual export and precision; an INT8 CPU artifact is not proof of GPU suitability.
Keep CPU usable. Defer broader Intel/AMD support until this baseline is proven.

Measure end-to-end time including model loading and speaker separation, correction
effort, consequential errors, RAM/VRAM, responsiveness and power behaviour. Include a
4 GB NVIDIA laptop class and the lowest-supported CPU laptop; neither is certified yet.
Prove a clean packaged install without manual CUDA setup. Benchmark inference serially
so concurrent experiments do not distort results or exhaust memory.

Select GPU only after a real capability/load check. Report actual device use and CPU
fallback. Recoverable GPU failures must release the worker/resources, retain committed
text and retry only uncommitted work using a validated compatible CPU path. Record actual
backend/precision per attempt; a different model or incompatible output configuration
requires a separate working version. Do not repeatedly retry a broken GPU or confuse
partial CPU operator fallback with fully accelerated execution.

Evaluate pyannote on GPU after ASR unload; do not require simultaneous residency.
Create a separate GPU implementation issue only after the feasibility report establishes
the supported configuration and benefit. It should depend on #9, #12, #13 and #15,
and integrate with #11/#16/#19. A negative trial should record deferral, not block CPU work.

## Review and handoff

Provide searchable timestamped text, audio-linked review, autosaved edits and undo (#20).
Speaker naming offers three representative bounded clips, more examples, merge and
passage reassignment (#21). Preserve overlap evidence and useful exclusive attribution.
Playback/retranscription becomes unavailable after audio cleanup, with an explanation.

Copy prompt and transcript, Copy prompt only, and Save Markdown include an exact preview
and handoff-only omissions/redactions (#22). Templates require source-grounded output,
uncertainty, no invented owners/dates, and treating transcript text as data. Prefer file
handoff for long content; never truncate silently. External AI accounts and retention
remain the user's responsibility; opening a website does not select a business account.

## Cleanup, holds and updates

Persist cleanup intent before removal and retry safely after failures/restarts (#17).
Verify source identity immediately before deleting originals; folder watching is not
deletion authority. Never remove changed replacements or unauthorised/shared sources.
Include staged media, decoded/prepared audio, clips, recovery fragments, transcript edits,
names, turns, indexes, previews and app-managed handoffs in the applicable cleanup phase.
Stop workers/release handles before cleanup; retry/recovery must not resurrect content.
Address SQLite free pages/WAL/temp files and test application-accessible residual content.
Ordinary deletion is not guaranteed forensic erasure or deletion of cloud/backups/AI copies.

Issue #18 refines the lifecycle: any user may explicitly hold audio/transcript material
indefinitely with a reason, without approval or automatic expiry. Holds survive restart
and block cleanup retries. Show monthly hold-review reminders while the app can run;
a fully closed app does not pretend to send reminders. Ordinary unfinished work shows
ages/actions without scheduled reminders. Releasing a hold requires the applicable
explicit cleanup confirmation, not silent deletion.

Completed cleanup receipts contain only random job ID, timestamps and result, with no
names, filenames or source hashes; expire them 30 days after successful cleanup. Failed
cleanup records remain only as needed. Keep bounded re-import suppression separate and
document its fields/lifetime. No permanent named meeting history or content backups.

Issue #26 refines updates: schema-changing updates wait until no meeting content,
retention holds or incomplete cleanup remain. Never expire a hold or delete content to
permit installation; explain the blocker. Configuration/minimal completed receipts alone
do not block migration. Database-compatible updates preserve state when idle; all updates
wait during processing/cleanup. Test rollback and artifact verification for both deployment
modes. Configuration backups exclude secrets/content. Preserve legacy outputs, settings
and models; no retroactive cleanup or legacy cleanup/import feature is added by this plan.

## Intake, security and validation

Selected-folder intake uses notifications/reconciliation or equivalently reliable bounded
polling (#23/#24). Catch up on launch/wake, detect newly arrived old files, handle cloud
placeholders and paused sync, stage stable read-only sources and fingerprint while copying.
Distinguish duplicates, changed replacements and intentional reruns. Never invent hydration
percentages or treat size stability as proof of completeness. Explain separate phone,
shared storage and cloud retention boundaries.

Disable telemetry before imports and verify packaged cached inference with denied outbound
access and network observation (#8/#28), on each shipped CPU/GPU path. Keep diagnostics free
of content, names, identifying paths and secrets; preview before export. Render untrusted
transcript/AI output as text. No arbitrary shell/Python execution through bridge input.

Start with 3-5 consented recordings (#12). Before broad release, use held-out excerpts from
at least ten meetings, targeting 60-90 minutes with varied sources/accents, New Zealand
English, Maori names, amounts, dates, negations and overlap (#25). Compare with audio and measure
correction effort, speaker attribution and important errors. Keep recordings/references
private with separate consent/cleanup; publish synthetic fixtures and safe aggregate results.
At least three nontechnical pilot users test the full accessible workflow (#27).

| Stage | Issues and release evidence |
| --- | --- |
| 0: feasibility | #8-#12: offline sample, Windows lock, packaging/bridge trial, model and optional GPU evidence |
| 1: reliable core | #13-#18: adapters, atomic recovery, workers, speakers, explicit cleanup and holds |
| 2: first usable release | #19-#22: setup, import, review/naming, Markdown and both cleanup confirmations; manual import is sufficient |
| 3: automated intake | #23-#24: hydration, wake, replacement, deduplication and source authority |
| 4: business readiness | #25-#29: quality, updates, accessibility, privacy/recovery/resources and deployment guides |
| Later | #30: interruption-safe microphone recording; no system capture/live transcription |

Security, accessibility, recovery and deletion correctness apply throughout. Stage 2
needs evidence for its shipped slice; later broad validation is not permission to skip
those checks. GPU, automatic intake and microphone recording do not gate that first slice.
