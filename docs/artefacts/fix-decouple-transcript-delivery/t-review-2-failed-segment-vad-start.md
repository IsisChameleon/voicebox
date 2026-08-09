# Review fix 2 — a failed segment still has to retire its VAD start

*2026-08-09. Commit `624fe22`. Decision: `BUILDLOG.md` D26 (corrects D25).*

Evidences no spec criterion — this is a **review finding against the R1 fix**, raised by a second
review pass over the branch diff. It closes the last open door on the Phase 2 invariant in
[`docs/specs/2026-08-06-decouple-transcripts-from-turn-closure.md`](../../specs/2026-08-06-decouple-transcripts-from-turn-closure.md)
§ "Phase 2 — Re-home the empty-transcript signal": *"exactly once per silent segment, or the
VAD-start deque drifts."* R1 fixed **what** a failed segment is called; it did not fix **that it must
still be counted**.

## The defect

`_unclaimed_bot_speech_starts` is a FIFO the observer appends to once per app-bot VAD start, and
`_emit_app_bot_transcript` pops the oldest entry unconditionally:

```python
# src/voicebox/agent.py:369-373  (unchanged by this fix)
started = (
    self._unclaimed_bot_speech_starts.popleft()
    if self._unclaimed_bot_speech_starts
    else time.time()
)
```

After R1, a failed segment called **no** callback at all. D25's reasoning — *"the VAD start stays
unclaimed, which is correct: the utterance it belongs to was never transcribed, so no transcript
should claim it"* — is right about whose start it is and wrong about where it goes. Leaving it at
the head of the queue does not park it; it hands it to the next transcript:

| # | segment | VAD start logged | deque before | transcript stamped |
|---|---|---|---|---|
| 1 | Whisper `ErrorFrame` | 100 | `[100]` | — (no frame) |
| 2 | "hello" | 200 | `[100, 200]` | **100** ❌ (should be 200) |

and the offset persists for every later transcript in the session. R1 moved the drift from the
empty-signal door to the error door rather than closing it. The R1 test passed because it never
reaches the agent — it asserts the worker's classification only.

A **raised** failure (`except Exception` in the worker) drifted the deque identically. D25 had
explicitly left that path alone.

## The change

The invariant is per-**segment**, not per-outcome: every segment that yields no
`TranscriptionFrame` signals exactly once, and the signal says which outcome it was. Written as one
branch, so a future third outcome cannot forget — the choice is *which* signal, never *whether*:

```python
# src/voicebox/processors/nonblocking_whisper_stt.py (worker loop, post-fix)
except Exception as e:
    # A failed segment must not kill the worker: every later
    # transcript in the session would be lost with it. A raised
    # failure and a yielded ErrorFrame are the same outcome to the
    # segment, so they take the same branch below.
    errors += 1
    logger.error(f"{self}: transcription failed, segment dropped: {e}")
try:
    ...
    if not transcripts:
        signal = self.on_failed_segment if errors else self.on_empty_segment
        if signal is not None:
            await signal()
```

```python
# src/voicebox/agent.py
async def _on_failed_segment(self):
    """Retire the VAD start of a segment whose transcription failed.
    ...
    """
    if self._unclaimed_bot_speech_starts:
        self._unclaimed_bot_speech_starts.popleft()
```

Wired next to the existing one, in `start()`:

```
$ grep -n "on_empty_segment\|on_failed_segment" src/voicebox/agent.py
364:        instead (``_on_empty_segment`` / ``_on_failed_segment``).
386:    async def _on_empty_segment(self):
400:    async def _on_failed_segment(self):
485:        stt.on_empty_segment = self._on_empty_segment
486:        stt.on_failed_segment = self._on_failed_segment
```

## Proof: the regression test fails against the pre-fix behaviour

The new test drives the **real** `_PipelineEventObserver` and the **real** `PipecatMCPAgent` over a
running pipeline whose Whisper fails segment 1 — the layer the R1 test never reached. Mutating
`_on_failed_segment` back to a no-op (`return  # MUTATION: pre-fix behaviour`) reproduces the
reviewer's scenario exactly:

```
$ uv run pytest tests/test_nonblocking_stt.py -q -k "vad_start_alone"     # with the mutation
>       assert transcripts[0].turn_started_at == "1970-01-01T00:03:20.000+00:00"
E       AssertionError: assert '1970-01-01T0...:40.000+00:00' == '1970-01-01T0...:20.000+00:00'
E         - 1970-01-01T00:03:20.000+00:00
E         + 1970-01-01T00:01:40.000+00:00

1 failed, 11 deselected, 1 warning in 3.78s
```

`00:01:40` is epoch **100.0** — the failed segment's start, claimed by segment 2's transcript.
`00:03:20` is epoch **200.0**, its own. Restored, the same test passes.

## Test output

```
$ uv run pytest tests/test_nonblocking_stt.py -q
12 passed, 1 warning in 39.53s
```

Three tests added / extended (`tests/test_nonblocking_stt.py`):

| Test | What it pins |
|---|---|
| `test_error_frame_is_not_an_empty_segment` (extended) | an `ErrorFrame` fires `on_failed_segment` **exactly once** and `on_empty_segment` never — R1's assertion plus the missing half |
| `test_raised_failure_signals_the_same_as_an_error_frame` | the third door: a raised failure takes the same branch |
| `test_failed_segment_leaves_the_next_transcripts_vad_start_alone` | end-to-end through observer + agent: segment 2's transcript carries segment 2's start, and the deque ends empty |

Full suite, lint, format, types:

```
$ uv run pytest -q
90 passed, 6 warnings in 63.99s (0:01:03)

$ uv run ruff check src/ tests/
All checks passed!

$ uv run ruff format --check src/ tests/
26 files already formatted

$ uv run pyright src/
2 errors, 0 warnings, 0 informations
```

The two pyright errors are **pre-existing on `HEAD`** — verified by stashing this change and
re-running: same two (`agent.py` `start_recording` on an `Optional`, `browser_session.py:39`
`reportInvalidTypeForm`), only the line number shifts.

## Also in this commit — `.codex/config.toml` untracked

The R1 docs commit `fceec63` swept in `.codex/config.toml`, a personal Codex CLI config
(`sandbox_mode = "workspace-write"`, `network_access = true`) unrelated to the branch and unmentioned
in its title. It is now untracked (`git rm --cached`) and `.codex/` is gitignored, so the file stays
on the author's disk and off the repo. Nobody's execution policy changes by merging this branch.

## Not covered

- 🔴 **No live reproduction.** Both failure modes are stubbed (`_ErrorFrameSTT` yields an
  `ErrorFrame`, `_ExplodingSTT` raises); no session has been run against a genuinely broken Whisper.
  The pipecat source that motivates the stubs is quoted in the R1 artefact.
- **The `start()` wiring is asserted by grep, not by a test.** The integration test sets
  `stt.on_failed_segment` itself, mirroring `agent.start()`; no test constructs the real pipeline to
  prove `start()` does it. Same gap `on_empty_segment` has had since Phase 2 — closing it means
  standing up the transport.
- **A failed transcription still surfaces nothing to `listen()`** — deliberate, for want of a
  consumer (D25, unchanged). A caller sees the app bot's speech span with no transcript after it,
  and no way to tell "Whisper broke" from "nothing intelligible was said".
- **Only the ordered, single-worker case is proven.** The FIFO is correct *because* the STT worker
  is single and ordered (D10); nothing here defends the invariant if that ever becomes a pool.
