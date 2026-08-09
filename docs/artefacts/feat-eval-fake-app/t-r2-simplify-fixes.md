# R2 evidence — simplify-pass fixes on the fake app

*2026-08-09. Commit `69b251e`. Review: `/simplify` (4 parallel angle agents: reuse,
simplification, efficiency, altitude) over `main..HEAD`.*

Success criteria: each finding fixed with fresh verification, or skipped with the reason
recorded. Spec under review: `docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md`.

## Fixed (5)

| Finding (angle) | Fix |
|---|---|
| R1's per-record open/close was built on a false premise — `PipelineWorker._cleanup` DOES forward `cleanup()` to observers (`pipecat/pipeline/worker.py:1006` → `worker_observer.py:123-131`) (altitude) | session-scoped line-buffered handle + `cleanup()` override writing `session_ended` and closing the file |
| Ground-truth log had `session_started` but no end marker — a reader can't tell a clean end from a crash (altitude) | `session_ended` record from `cleanup()`; a missing one now marks a crash |
| A streaming reply cut mid-generation left `_reply_parts` to leak into the next `brain_reply` (altitude, LLM-mode only) | on `InterruptionFrame`, non-empty parts flush as `brain_reply` with `interrupted: true` |
| `runner_args.handle_sigterm` dropped — SIGTERM (our own `pkill` teardown path) bypassed pipeline shutdown (altitude, minor) | forwarded to `WorkerRunner` |
| `ScriptedBrain` hand-rolled a cycling cursor: two fields + modulo (simplification) | `itertools.cycle` + `next()` |
| Module docstrings restated the README's prose — same facts in four places (simplification) | trimmed to pointers |

Reuse angle: **no findings** (the observer-skeleton overlap with `agent.py`'s
`_PipelineEventObserver` is the spec-prescribed pattern, not accidental duplication).

## Skipped, with reasons (4)

- **Swap voicebox's vendored Kokoro for pipecat's stock `pipecat.services.kokoro.tts`**
  (altitude, strongest finding). The spec settled "TTS reuses
  `voicebox.processors.kokoro_tts.KokoroTTSService`" and this branch's brief forbids
  re-litigating. The reviewer's argument is real, though: the reference instrument and the
  system under test currently share one TTS implementation, so a TTS regression moves both and
  the ground truth is blind to it — and pipecat 1.3.0's stock service is the same upstream code,
  no new extra needed. **Follow-up decision for Isabelle.**
- **Restructure the observer onto pipecat's turn hooks** (`on_assistant_turn_stopped` carries
  `content` + `interrupted`; `TurnTrackingObserver` computes `was_interrupted`) — a design
  change to a spec-prescribed mechanism ("same `BaseObserver` pattern as `agent.py`'s");
  belongs to the deferred eval-harness design pass, where the aligner's needs decide the
  record shape.
- **`ScriptedBrain` should extend `LLMService`, not `FrameProcessor`** — reviewer self-rated
  weakest; nothing in this pipeline consumes the `LLMService`-only hooks today, and changing
  the base class would invalidate the live-verified barge-in behavior without a re-run.
- **Validate-only startup helper instead of the throwaway `create_brain()`** — the two review
  agents disagreed (efficiency: wasted SDK client; simplification: "one line, the comment earns
  it"). Splitting validation from construction creates a parallel flow that can drift; the cost
  is one discarded object per process start of a dev fixture. Kept as-is.

## Verification (verbatim)

Probe — worker teardown reaches the observer (the load-bearing claim behind the fd fix):

```
$ uv run python scratchpad/probe_observer_cleanup.py   # trivial 2-relay pipeline,
                                                       # GroundTruthObserver attached,
                                                       # stop_when_done() → run() returns
events: ['session_started', 'session_ended']
PROBE PASS: cleanup() fired, session_ended written, file closed cleanly
```

Boot smoke (scripted, no key): `GET / -> 200` via the prebuilt UI, `Bot ready` banner seen,
clean shutdown. Bonus: the 15 s `timeout`-killed variant showed SIGTERM now shuts uvicorn down
gracefully (`Shutting down … Application shutdown complete`).

Gates:

```
$ uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
All checks passed!  /  28 files already formatted
$ uv run pyright tests/eval/fake_app/   → exit 0
$ uv run pytest -q                      → 92 passed
```

## Not covered

- `session_ended`, the interrupted-reply flush, and graceful SIGTERM in a real WebRTC session —
  next live dogfood covers them incidentally (the probe covers the cleanup path itself).
- The four skips above.
