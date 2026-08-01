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
