# Coding and integration plan

Read [WORKSPACE_PLAN.md](WORKSPACE_PLAN.md), [AGENTS.md](../AGENTS.md) and the live
[roadmap #7](https://github.com/thatskiff33/unminute/issues/7). This is a
coding sequence, not a claim that the planned features or hardware have been validated.
Live child issues and reviewed acceptance evidence govern completion.

## Use both CLIs with clear ownership

Use one CLI as implementation owner and the other as an independent reviewer for a
given issue. A useful starting allocation is Codex for dependencies, workers, persistence
and recovery; Claude Code for the desktop trial, review UX and onboarding. This is a
coordination choice, not a claim that either product is inherently better at that work.
Swap roles when useful. Premium access does not remove rate limits, review needs or
hardware bottlenecks; choose available capable models and reserve deeper reasoning for
state transitions, deletion, security and package compatibility. Do not hard-code model
names or launch unlimited background sessions.

Codex and Claude Code (2.1.277 or later) both read `AGENTS.md` natively. Both must read the same
issue and current comments. Personal global instructions are not assumed to transfer
between products. On first launch, have each CLI summarise the shared constraints and
report its branch/worktree before making changes.

Start with at most two implementation lanes, only on non-overlapping surfaces. Each
issue gets one branch/worktree and one active writer. Review a fixed committed revision
in a separate read-only/detached checkout; do not review files while the writer changes
them. Read-only here is a role instruction, not an OS security boundary. One designated
integration session owns pushes, PR state, merge order and branch cleanup. Reviewers do
not edit, push, merge, post comments or use private recordings without task authorisation.

## Sequence and practical assignments

Start #9 with current dependency-advisory triage as well as compatibility. Verify the
affected package, version range and actual loading path before selecting a fix or
recording non-applicability. A passing resolver is not evidence that a security alert
is resolved. Keep security updates focused and run the affected integration checks.

| Wave | Implementation and order | Exit evidence |
| --- | --- | --- |
| A: foundation | Codex: #9 dependency/model-integrity trial. Claude: #8 telemetry/offline isolation, coordinating import/config changes with #9. Keep experiments in separate environments. | Reproducible CPU lock/model load; denied-network inference evidence. No claims based only on source inspection. |
| B: feasibility | After #9: Claude owns #10 pywebview/bridge/playback trial; Codex owns #12 CPU/optional NVIDIA comparison. | Standard-user shell/bridge proof; measured quality, full-pipeline runtime and RAM/VRAM on named hardware. Keep real evaluation material private. |
| C: package and adapters | #11 follows #9/#10; #13 follows #9/#12. One owner coordinates the dependency/runtime contract across both. | Clean packaged install and stable timed-segment interface, tested models/manifests, preserved legacy path. |
| D: durable core | #14 first; then #15, followed by #16. #17 can run alongside worker work only after agreeing state ownership/cancellation/cleanup contracts; #18 follows #17. | Fault injection proves no missing/duplicate committed chunks; usable text after speaker failure; explicit cleanup, hold races and bounded resources verified. |
| E: first complete workflow | #19 follows #11/#13/#16. #20 follows #10/#14/#17/#18; #21 follows #20/#16; #22 follows #20/#21. | A nontechnical user imports, reviews/names, hands off Markdown, and completes both cleanup confirmations safely. Ship this CPU-capable slice before waiting for intake or GPU extras. |
| F: intake and broad readiness | #23 then #24; #25 can begin after #12/#22, #26 after #11/#18/#20; #27 after #22/#24; #28 after #8/#18/#22/#24/#26; #29 follows #25/#26/#27/#28. | Held-out quality, both deployment/update modes, accessible workflow, cloud-file authority and packaged privacy/recovery evidence. |
| Later | #30 after #29. | Interruption-safe microphone capture; no system capture/live transcription. |

Do not turn these waves into a long chain of unmerged branches. Prefer merging each
small dependency PR to `main`, then branching the dependent issue from refreshed `main`.
When two issues overlap in requirements, state schema, worker events, UI bridge or shared
docs, serialise that surface or agree its contract before either writer proceeds.

Issue #8 starts in wave A, but its packaged-build acceptance cannot be completed until
#11 supplies that artifact. Merge a verified foundation increment without closing #8,
then record the packaged network evidence and close it only when all criteria pass.
Likewise, prototypes and synthetic fixtures do not complete #12's consented-recording
comparison. Collect consented evaluation material, lowest-supported hardware, deployment
restrictions and a signing route early; report gaps explicitly instead of inventing evidence.

Before #14/#15/#17 implementation, capture the proposed state transitions, chunk identity,
worker cancellation/ownership and hold-versus-cleanup arbitration in a small reviewed
design within the owning issue. This prevents separate agents inventing incompatible
recovery and deletion models. It is technical design, not a new product-direction vote.

## GPU decision gate

Use #9/#12 to establish exact engine/model/export/precision/runtime combinations and a
CPU baseline. Benchmark loading, ASR, unload and pyannote separately and together. Include
long inputs, constrained VRAM, other normal desktop activity, plugged-in/battery behaviour
and offline execution. Run heavy benchmarks one at a time; do not share mutable runtime
environments, test databases or model-download caches between CLI lanes.

GPU implementation proceeds only when there is a meaningful measured benefit without
unacceptable quality, memory, packaging or reliability costs. Record the evidence and
supported configuration in #12, then open a bounded child issue under #7 if justified.
That issue depends on #9/#12/#13/#15 and coordinates #11/#16/#19. Otherwise record deferral
and keep CPU delivery moving. Do not quietly add unsupported Intel/AMD paths or switch
models during CPU fallback. NVIDIA acceleration is optional in Faster mode, not required
for first release or for every engine to be useful.

## Per-issue working loop

1. Refresh `main`; read the issue, comments, dependencies and relevant code. Confirm
   prerequisites are actually merged and evidenced. Record owner, branch, touched surfaces
   and acceptance checks in the issue when coordination comments are authorised.
2. Implement one bounded issue in its worktree. Add tests for its meaningful failure
   modes and update docs with the behaviour. Keep unrelated work out of the diff.
3. Run the relevant local checks. Commit the candidate and ask the other CLI to review
   that exact commit against current `main`. Require concrete scenarios and file/line
   evidence; review includes counter-evidence, not just a list of possible problems.
4. The writer fixes confirmed findings. Verify the failing cases and re-review changed
   risk areas. Distinguish a skipped hardware/manual check from a passed check.
5. The integration owner opens a focused PR with issue links, what changed, validation,
   limitations and remaining external evidence. Do not use a closing keyword until all
   issue acceptance criteria are actually met; keep evidence-dependent issues open.
6. Wait for required CI and scanning checks. Merge using the live repository's allowed
   squash method, without bypassing protection, when the task authorises merge. Check
   post-merge CI and refresh local `main`.
7. Delete only verified merged branches after checking for dirty worktrees or extra
   commits. Squash merges are not necessarily reported by `git branch --merged`; check
   the PR's merged head and resulting diff. Never discard unique work to make a list clean.

Release publication is separate from merging code. Existing CI does not run speech models;
new packaged behaviour needs the explicit evidence in the issues. A model-free CI test
suite should grow around adapters, manifests, recovery and cleanup while hardware and
real-audio checks are recorded separately.

## First terminal setup

From the repository root, after checking for existing branch/path names:

```powershell
git fetch origin --prune
git worktree add -b codex/issue-9-runtime ../whisper-issue-9 origin/main
git worktree add -b claude/issue-8-offline ../whisper-issue-8 origin/main
```

Open separate terminals in those worktrees and start `codex` and `claude` respectively.
Do not launch both writers in the original checkout. These commands create isolated
code checkouts, not separate model caches or Python environments; isolate those explicitly.
Use native Windows for acceptance checks: a WSL result does not certify Windows packaging.

Give the implementation owner this task, replacing the issue number:

> Read AGENTS.md, docs/WORKSPACE_PLAN.md, docs/IMPLEMENTATION_PLAN.md and issue #9
> including comments. Implement only this issue in this worktree, coordinating shared
> dependency/import changes with the other lane. Run the applicable checks and document
> evidence and genuine blockers. Do not access private recordings or claim unavailable
> hardware tests passed. Commit a review candidate; leave push/merge to the integration
> owner. Do not start dependent issues or publish a release.

For the other CLI's independent review:

> Review the specified candidate commit against the specified main/base commit and the
> issue acceptance criteria. Do not edit or publish anything. Find concrete regressions,
> missing recovery/privacy checks and unsupported completion claims. Attempt to refute
> each finding against the code and tests; report only supported findings with file/line
> references and failure scenarios. State what could not be verified.

Codex also offers interactive `/review` for a selected diff. For either CLI, keep the
review scope fixed and explicitly identify the candidate and base commits.

## Documentation references

- [Codex project instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Codex CLI and review](https://learn.chatgpt.com/docs/codex/cli)
- [Claude Code project memory and imports](https://code.claude.com/docs/en/memory)

External capabilities and subscription limits change; use installed CLI help and current
official documentation before relying on specific flags or model availability.
