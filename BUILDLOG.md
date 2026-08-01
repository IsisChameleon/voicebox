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
