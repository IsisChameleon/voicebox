# Real-pipeline tests + a runnable smoke script — evidence

*2026-08-14. Commits `4ef5384` (tests) and `c2fb0fd` (scripts).*

Evidences the two gaps left open by [t-2-review-fixes.md](t-2-review-fixes.md) "Not covered":

> - `scripts/smoke_browser_shim.py` still not run — it hard-codes `localhost:3000`, which nothing
>   in this repo serves.
> - Barge-in into a mid-buffer utterance, and a real synthesis failure, are unit-tested only.

and the first item of [issue #24](https://github.com/IsisChameleon/voicebox/issues/24), folded into
this PR by [BUILDLOG D33](../../../BUILDLOG.md).

Conclusion: the buffer's tests now run inside real pipelines and were each proven able to fail;
the smoke script runs end to end with no voice app, and now exits non-zero when the audio path is
dead.

---

## Task 3a — the buffer's tests run through `pipecat.tests.utils.run_test`

### What changed

- `tests/test_generated_utterance_audio_buffer.py` — rewritten. `_CaptureBuffer` (a subclass that
  overrode `push_frame` to capture) and all 11 hand-driven `process_frame` calls are gone. Every
  test now sends frames through a real `Pipeline` inside a real `PipelineWorker` and captures both
  directions.
- Warm-up coverage moved to `tests/test_generated_utterance_in_pipeline.py`, onto the **real**
  production Text-to-Speech (TTS) service, because a real `StartFrame` is what sets `sample_rate`.
- `src/voicebox/processors/generated_utterance_audio_buffer.py:41-51` — comment corrected. It
  claimed "`agent.py` wires that case to `discard()` through the service's `on_error` event". No
  such wiring exists.

Proof the claim was false — graph search over `src/` for `on_error`, whose only hit is the comment
itself:

```
$ search_code(pattern="on_error|discard\(\)", path_filter="^src/")
  src/voicebox/processors/generated_utterance_audio_buffer.py  (match_lines 46,47,49,69)
  total_grep_matches: 4   total_results: 1
```

### Test-by-test, and what each one can catch

| Test | Framework behaviour it needs | Old harness could see it? |
|---|---|---|
| `test_generated_audio_is_withheld_until_its_utterance_closes` | ordering of a real queue | no — it asserted mid-stream internal state |
| `test_upstream_frames_pass_through_unchanged` | frames sent from the END of the pipeline | no — direction was faked by an argument |
| `test_an_error_from_below_discards_the_partial_utterance` | `push_error_frame` → UPSTREAM | **no — the old test fed the `ErrorFrame` DOWNSTREAM, a direction production never produces (D32)** |
| `test_interruption_discards_generated_audio_that_has_not_reached_playout` | `InterruptionFrame` as an out-of-band SystemFrame | partly — it hand-rolled a worker with `sleep()` calls |
| `test_warm_up_synthesizes_once_the_pipeline_hands_over_a_sample_rate` | `StartFrame` sets `TTSService.sample_rate` | no — the fake service set `sample_rate` itself |

Withholding is asserted by **order inversion**: a marker frame sent *after* the first audio frame
must arrive *before* it. In a real pipeline the buffered and unbuffered sequences are otherwise
identical.

### Mutation evidence — every test was made to fail for its stated reason

A green port proves nothing (D31's lesson). Four mutations were applied to the **production**
processor, one at a time, each reverted with `git checkout` afterwards:

```
=== MUTATION A: buffering disabled (audio forwarded immediately) ===
FAILED tests/test_generated_utterance_audio_buffer.py::test_generated_audio_is_withheld_until_its_utterance_closes
FAILED tests/test_generated_utterance_audio_buffer.py::test_an_error_from_below_discards_the_partial_utterance
FAILED tests/test_generated_utterance_audio_buffer.py::test_interruption_discards_generated_audio_that_has_not_reached_playout
3 failed, 1 passed, 1 warning in 2.56s
=== MUTATION B: no discard on ErrorFrame ===
FAILED tests/test_generated_utterance_audio_buffer.py::test_an_error_from_below_discards_the_partial_utterance
1 failed, 3 passed, 1 warning in 2.45s
=== MUTATION C: interruption no longer discards ===
FAILED tests/test_generated_utterance_audio_buffer.py::test_interruption_discards_generated_audio_that_has_not_reached_playout
1 failed, 3 passed, 1 warning in 2.96s
```

```
=== MUTATION D: warm-up no longer waits for the sample rate ===
E       AssertionError: warm-up must park until the pipeline sets a sample rate
E       assert not True
FAILED tests/test_generated_utterance_in_pipeline.py::test_warm_up_synthesizes_once_the_pipeline_hands_over_a_sample_rate
1 failed, 1 passed, 1 xfailed, 1 warning in 5.82s
```

Note B and C: only the test that names each behaviour fails. The tests are specific, not merely
sensitive.

### Suite + lint

```
$ uv run pytest -q
113 passed, 1 xfailed, 3 warnings in 65.50s (0:01:05)

$ uv run ruff check src/ tests/ scripts/
All checks passed!

$ uv run ruff format --check src/ tests/ scripts/
39 files already formatted
```

The 1 xfailed is the D32 strict xfail — the unimplemented failure contract, deliberately red.

---

## Task 3b — `localhost:3000` is gone and the smoke script actually runs

### What changed

| Site | Before | After |
|---|---|---|
| `src/voicebox/server.py:130` | `url: str = "http://localhost:3000"` | `url: str` — required |
| `scripts/smoke_browser_shim.py` | hard-coded `:3000` | `serve_blank_page()` |
| `scripts/smoke_full_duplex.py` | hard-coded `:3000` | `serve_blank_page()` |
| `tests/test_browser_session.py` ×3 | `http://localhost:3000` | `https://app.example` (pure mocks; nothing is contacted) |

New `scripts/blank_page_server.py` (36 lines) serves one empty page on an ephemeral port. Both
smoke scripts needed a **secure origin** for the shim's hooks to install, not an app — so the
dependency is removed rather than repointed at the fake app's `:7860`.

Verification that no code reference survives (documentation keeps its historical mentions — those
are records):

```
$ grep -rn "localhost:3000" src/ scripts/ tests/ README.md CLAUDE.md
exit=1 (1 = no matches left)
```

### The live run — first successful execution of this script

```
2026-08-14 15:58:22 | INFO | page url: http://localhost:41385/, title: 'voicebox smoke'
2026-08-14 15:58:22 | INFO | shim state (pre-speak): {'installed': True, 'micHookInstalled': True,
                             'pcHookInstalled': True, 'wsReady': False, 'inboundChunks': 0, ...
                             'errors': ['WS error event: [object Event]', × 5]}
2026-08-14 15:58:22 | WARNING | ⚠ shim WS not ready yet — still retrying (see issue #22)
2026-08-14 15:58:33 | SUCCESS | ✓ shim received 40 audio chunks from pipecat
2026-08-14 15:58:39 | INFO | done.
EXIT=0
```

- The shim installed on the self-served page (`installed`, `micHookInstalled`, `pcHookInstalled`
  all true).
- 40 Kokoro audio chunks crossed the WebSocket into the page — the audio path works.

### A scoring bug found by running it

The script logged `✗ no inbound audio chunks received` as an ERROR and still exited 0. Only the
"shim never installed" branch called `sys.exit(1)`; every other check just logged. The script
could not fail however dead the audio path was. Fixed in `c2fb0fd`
(`scripts/smoke_browser_shim.py:139-142`).

### Issue #22 reproduced in-repo, with no app

From the first run's timeline, the audio WebSocket server binds ~9 s **after**
`start_browser`returns:

```
15:57:12.945 | browser ready: {'cdp_endpoint': ..., 'audio_ws_url': 'ws://localhost:9091', ...}
15:57:16.910 | shim state (pre-speak): wsReady: False, errors: [4 × 'WS error event']
15:57:19.011 | pipecat.transports.websocket.server: Starting websocket server on localhost:9091
15:57:19.964 | pipecat.transports.websocket.server: New client connection from ('127.0.0.1', 58012)
```

This is exactly [issue #22](https://github.com/IsisChameleon/voicebox/issues/22)'s complaint —
"`start_browser_session()` should not surface a usable `client_connected` state until the audio
WebSocket is accepting frames" — now reproducible in this repo in ~25 seconds with no external
voice app. It is left as a WARNING because the shim retries and recovers; it is product behaviour,
not a script defect.

---

## Not covered

- **The other five hand-driven test files.** `test_nonblocking_stt.py`, `test_vad_placement.py`,
  `test_transcript_delivery.py`, `test_stop_drains_stt.py` and the observer half of
  `test_agent_surface.py` still drive processors by hand. They stay with issue #24 on its own
  branch (BUILDLOG D33) because they test code this PR does not touch.
- **The D32 failure contract is still unimplemented** and still pinned as a `strict=True` xfail. A
  failed synthesis still plays its partial audio. Needs the design pass named in D32.
- **`scripts/smoke_full_duplex.py` was not run.** Its `localhost:3000` was removed the same way,
  but the run itself is not evidenced here.
- **No live dogfood against the fake app (Nova)** in this round. The smoke script proves the audio
  path out of pipecat into the page; it does not prove a two-way conversation.
- **Timing is one WSL2 host.** The ~9 s pipeline start and the 40-chunk count are from this
  machine, not a distribution.
