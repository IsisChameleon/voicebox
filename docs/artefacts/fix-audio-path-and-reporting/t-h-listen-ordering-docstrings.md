# Task H — ordered `listen()` batches, honest docstrings

*Captured 2026-08-04. Commit `61aa484` (code), decision `BUILDLOG.md` D18.
Evidences the success criteria in
`docs/specs/2026-07-31-fix-plan-execution.md` § "Task H" (criteria 1–3, stories H1/H2/H3)
plus the round-1 judged-scope item (`wait_for_turn` return shape,
`r1-blind-verification.md` "issues" list).*

## Criterion 1+2 — stable sort per batch, cursor semantics untouched (H1, H2)

`listen_events` (`src/voicebox/agent.py`) now returns
`sorted(events, key=lambda e: e.t)` while the cursor stays `cursor + len(events)` on the
unsorted slice — Python's `sorted` is stable, so equal-`t` events keep append order, and the
sort cannot reach across a cursor boundary.

```
$ uv run pytest tests/test_listen_ordering.py tests/test_agent_surface.py -v
tests/test_listen_ordering.py::test_batch_sorted_by_t PASSED
tests/test_listen_ordering.py::test_equal_t_events_keep_append_order PASSED
tests/test_listen_ordering.py::test_cursor_paging_lossless_under_sort PASSED
tests/test_agent_surface.py::test_wait_for_turn_reports_wait_duration PASSED
tests/test_agent_surface.py::test_ungated_speak_has_no_wait_key PASSED
(9 pre-existing surface tests also PASSED)
14 passed, 3 warnings in 3.35s
```

`test_cursor_paging_lossless_under_sort` is the H2 shape: page 1 read, then two more events
land — one with `t=5.0`, *earlier* than everything already read (the late-transcript case) —
and the concatenation of both pages contains every event exactly once, each page individually
time-ordered. The late event surfaces in the later batch, as documented, not lost.

## Criterion 3 — the docstrings state what the timestamps mean (H3, reviewer check)

All accumulated docstring debt from rounds 1–6, now in the tool docstrings
(`src/voicebox/server.py`) and mirrored in `agent.py`/`events.py`:

| Claim | Where |
|---|---|
| Batch is time-ordered but a late event can land after the cursor moved on; merge on `t` across batches | `server.py` `listen()`, `agent.py` `listen_events()` |
| `tester_transcript.t` is the `speak()` CALL time — ground truth, not STT/playout | `server.py` `listen()`, `events.py` `TesterTranscriptEvent` |
| `transcription_lag_secs` is STT-queue age; 0.0 while text is held by an open turn (D11/D14) — 0.0 ≠ "nothing pending" | `server.py` `listen()`, `agent.py` `listen_events()` |
| Barge-in arming is instantaneous server-side (~1 ms measured, D17) — arm early; fires on next matching event after arm; no disarm; audio at `timer_secs` + TTS synthesis (~3–9 s) | `server.py` + `agent.py` `speak()` |
| Drained transcripts land AFTER `session_stopped` — a listen loop exiting on it never sees them; `events.json` does | `server.py` `stop()` |

## Round-1 judged-scope item — `wait_for_turn` return shape (D18)

Decision recorded in `BUILDLOG.md` D18: add the small honest key rather than document the
opacity. `speak(wait_for_turn=True)` results now carry `waited_for_turn_secs` (monotonic span
around the silence gate, ms precision); ungated results keep the bare `{queued: true}`, so the
key's presence marks the gated path. Pinned by the two `test_agent_surface.py` tests above.

## Quality gates

```
$ uv run pytest -q
70 passed, 5 warnings in 54.95s        # 65 → 70
$ uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
All checks passed!
22 files already formatted
$ uv run pyright src/
2 errors, 0 warnings, 0 informations   # the 2-error baseline (agent.py:482, browser_session.py:35)
```

## Not covered

* The sort is exercised on synthetic logs only; no live session has yet produced an
  out-of-order batch on the post-D/E code (skew is now ms-scale by design). Round 7 reads
  `events.json` for ordering as a checklist item, not a blocker.
* The new docstrings reach MCP clients only after a server restart (`server.py` is
  parent-side, not hot-reloaded) — the restart is scheduled before round 7, together with the
  two earlier restart-pending docstrings (listen timeout ceiling, stop teardown note).
* `waited_for_turn_secs` is unit-tested, not yet observed live (round 7).
* 🔴 live-only stories elsewhere in the spec are unaffected; H has no live-only story of its
  own (H3 is a reviewer check).
