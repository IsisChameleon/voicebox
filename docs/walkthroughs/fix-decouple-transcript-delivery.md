# Walkthrough — `fix/decouple-transcript-delivery`

*Status: **in progress**. Started 2026-08-06. Branched from `56807fd`.*

Removes the coupling between the **app bot's** transcript reaching `listen()` and the app bot's
turn aggregator closing a turn on a timer. Design and verified open questions:
[`docs/specs/2026-08-06-decouple-transcripts-from-turn-closure.md`](../specs/2026-08-06-decouple-transcripts-from-turn-closure.md).
Decision record: `BUILDLOG.md` D24.

**Who is who** (pipecat names parties by direction, not identity): `user_aggregator` belongs to the
**app bot** (audio in), `assistant_aggregator` belongs to the **tester** (what we speak as). Labelled
pipeline: [`docs/architecture/pipeline-parties.html`](../architecture/pipeline-parties.html) — open
in a browser.

## Status

| Phase | What it changes | Commits | Evidence | Done |
|---|---|---|---|---|
| **0** | Spec + labelled-pipeline diagram committed; D24 appended; walkthrough opened | | — | ⬜ |
| **1** | App-bot transcript emitted from the pipeline observer on `TranscriptionFrame` arrival, not on turn closure | | | ⬜ |
| **2** | Empty-transcript signal re-homed to the STT worker (`on_empty_segment` callback) | | | ⬜ |
| **3** | `TURN_STOP_TIMEOUT_SECS` deleted; `session_started` note and `CLAUDE.md` refreshed | | | ⬜ |

**Phase 4 (removing the vestigial aggregator + smart-turn analyzer) is deferred** — it is a
structural change whose blast radius includes the tester side, and it gets its own design pass
(spec § "Phase 4").

Live-only (🔴): the slow-decode scenario (spec § "Scenarios" 1) needs a running voice app on
`localhost:3000` and a decode slower than pipecat's 5 s default turn timer. It is the checklist for
the Phase 3 dogfood session, not provable by `pytest`.

## Try it

```bash
uv run pytest -q                     # whole suite
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
uv run pyright src/
```
