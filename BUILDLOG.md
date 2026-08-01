# BUILDLOG

Append-only decision record for voicebox. One numbered entry (D1, D2, …) per decision,
written **at the moment the decision is made** — never reconstructed, never rewritten.
Reversing a decision gets a *new* entry that points back at the old one.

Walkthroughs, specs and evidence artefacts cite entries by number (`BUILDLOG.md D3`).

**Before D1:** this repo predates the practice. Decisions taken before 2026-08-01 are not
numbered here; they live in `docs/specs/` (design + execution specs), `docs/design/`
(architecture reviews) and the "Non-obvious facts & traps" section of `CLAUDE.md`.

---

## D1 — Adopt walkthrough discipline mid-branch rather than at the next branch

*2026-08-01. Branch: `fix/audio-path-and-reporting`.*

**Decided:** start `BUILDLOG.md`, `docs/walkthroughs/fix-audio-path-and-reporting.md` and
per-task evidence artefacts now, on a branch that is already three commits in, instead of
waiting for a clean branch start.

**Why:** this branch is exactly the case the practice exists for — eight tasks (A–H), five of
them touching `agent.py` serially, each changing numbers that later tasks depend on
(`docs/specs/2026-07-31-fix-plan-execution.md`). Without a living walkthrough the review
surface is a 1500-line diff.

**Rejected:** back-dating BUILDLOG entries for Tasks A–C so the log looks complete. A
reconstructed rationale is a guess wearing a timestamp; the pre-D1 note above says plainly
where the earlier reasoning lives instead.

**Consequence:** Task A's evidence artefact is written retrospectively and labelled as such —
its contents are test output captured on 2026-08-01, not at the time A landed.

---

## D2 — Transcript→interval matching stays arrival-ordered; the test moves instead

*2026-08-01. Task B, branch `fix/audio-path-and-reporting`.*

**Context:** `test_turns_count_matches_utterances` asserted that a session with a dropped
middle transcript puts `"three"` on the *third* app-bot turn. `_match_app_transcripts`
(`metrics.py:210-229`) puts it on the second, and the test failed.

**Decided:** keep the implementation's rule — a transcript is claimed by the earliest
still-unclaimed interval that had already finished when it arrived — and correct the test.

**Why:** the expected list `["one", None, "three", None]` is not produced by *any* coherent
matching rule. Claiming from the left gives `["one", "three", None, None]`; claiming from the
right (latest finished interval) gives `["one", None, None, "three"]`. It was hand-written, and
its own explanatory comment contradicts it. The test also drifted from the spec story it cites
(B1 says four utterances, three transcripts, **one** flagged missing; the fixture had two).

**Rejected:** content- or lag-based matching, which would assign `"three"` correctly. Whisper's
per-turn lag is not constant and nothing here can read the audio, so any such rule would be a
heuristic dressed as a fact. Arrival order is the only thing the log actually knows.

**Consequence:** the failure mode is real and stays — a dropped transcript shifts text onto a
neighbouring turn. It is documented in `metrics._BIAS_NOTES` and now *pinned by a test*
(`test_dropped_transcript_shifts_text_onto_the_next_turn`) so it cannot change silently. Turn
*counts* still reconcile, which is what Task B's criteria actually require: the shift moves text
between turns, it never loses one.

---

## D3 — The VAD moves upstream as a pipeline stage, not as a transport parameter

*2026-08-01. Task D, branch `fix/audio-path-and-reporting`.*

**Context:** the execution spec's criterion 1 says `WebsocketServerParams` must carry
`vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=VAD_STOP_SECS))`. It does not: in
pipecat 1.3.0 `WebsocketServerParams.model_fields` has no `vad_analyzer` at all (checked, not
assumed — the check is in the Task D artefact). The VAD became a pipeline processor,
`pipecat.processors.audio.vad_processor.VADProcessor`.

**Decided:** implement the task's *intent* — put a `VADProcessor` stage between
`transport.input()` and the STT — rather than stop and ask. Recorded here and surfaced in the
commit message and the task report instead.

**Why:** the spec's title is "Move the VAD upstream of the STT" and its whole rationale is
about frame ordering, not about which object owns the analyzer. In this pipecat version there
is exactly one way to place a VAD upstream of a processor, so there was no judgement call to
delegate. `VADProcessor` broadcasts the same VAD frames both directions that the aggregator
did, so nothing downstream changes shape.

**Rejected:** pinning an older pipecat that still had the transport parameter. The parameter
was a convenience wrapper over the same `VADController`; downgrading to satisfy the letter of a
spec is not a fix.

**Consequence:** the spec's criterion 1 can never be satisfied as written; the artefact records
it as *adapted* with the API check attached, rather than as met. Three factories were extracted
from `start()` (`_create_vad_processor`, `_create_context_aggregators`, `_build_stages`) so the
stage order and the 1.0 s `stop_secs` are assertable without booting a pipeline — Tasks E–G all
touch `start()` and now touch a shorter one.
