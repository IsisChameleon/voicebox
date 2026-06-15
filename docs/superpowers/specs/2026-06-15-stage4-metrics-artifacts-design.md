# Stage 4 — Metrics & artifacts (the "test report")

*2026-06-15. Implements Stage 4 of [upgrade-roadmap.md](../../design/upgrade-roadmap.md), derived
from [architecture-review.md](../../design/architecture-review.md). Stages 0–3 are shipped.*

## Goal

Make every voicebox session self-produce an offline-analyzable test report, so both reviewers of a
test call are served:

- **Claude** (the orchestrating LLM) can't hear audio. To judge a call — was the app slow, did it
  yield when interrupted, did it answer — it needs *structured text*: the event log and computed
  metrics.
- **The human developer** wants to listen to / eyeball the recording and read the numbers without
  re-deriving them from a waveform.

The `scripts/` drivers are repo test fixtures, **not** the product surface. The real flow is a
developer pointing Claude at their voice app via the MCP tools; when the session stops, the report
must already be on disk in `record_dir`. So artifacts are written by the **session at `stop()`**,
never by a script.

No human-readable report file is generated: Claude is in the loop and narrates the call in chat,
citing `metrics.json`. A report renderer would duplicate that.

## Scope

In scope:

1. Stereo `merged.wav` (tester left, app-bot right).
2. `events.json` written by the session (the Stage 2 event log).
3. `metrics.json` written by the session (full metric set, derived purely from the event log).
4. A pure `compute_metrics` module + its unit test (introduces `pytest`).
5. Surfacing the artifacts to the calling LLM via the `start_browser_session` docstring and a
   richer `stop()` return value.

Out of scope: streaming/partial-transcript metrics, network-impairment metrics, any per-word
timing (batch Whisper is utterance-level by design — see roadmap "Out of scope").

## Architecture / components

The math is a **pure function**; the **session** does the I/O. Splitting them keeps the interval
arithmetic (where off-by-one bugs hide) testable without a browser.

| Unit | Change | Why |
|---|---|---|
| `src/voicebox/metrics.py` *(new)* | `compute_metrics(events: list[dict], vad_stop_secs: float) -> dict`. Pure: in = serialized event log, out = metrics dict. No I/O, no pipecat imports. | One purpose, isolated, unit-testable. |
| `src/voicebox/agent.py` | `merged.wav` → **stereo** via `AudioBufferProcessor(num_channels=2)`; `write_wav` gains a `channels` arg so `ember_voice.wav` / `kokoro_voice.wav` stay mono. Add `_dump_artifacts()` writing `events.json` (`[e.model_dump() for e in self._events]`) + `metrics.json` (`compute_metrics(...)` using `VAD_STOP_SECS`). Call it from `stop()` whenever `record_dir` is set. | Every session with `record_dir` self-produces the report. Events don't need the audio buffer, so artifact-writing is gated on `record_dir` alone, not on audio capture. |
| `src/voicebox/server.py` | (1) `start_browser_session` docstring lists all five artifacts + their formats. (2) `stop()` changes from `-> bool` to return `{"stopped": True, "artifacts": {...}}` (absolute paths) when `record_dir` is set, and its docstring documents `metrics.json`'s top-level keys. | The calling LLM only learns the server through tool *descriptions* and *return values* (see below). |
| `scripts/e2e_readme_call.py` | Drop the hand-rolled `events.json` dump (now redundant — the session writes it). | Remove duplication; scripts are fixtures, not the surface. |
| `tests/test_metrics.py` *(new)* | Feed a hand-built synthetic event list to `compute_metrics`, assert every metric. Adds `pytest` as a dev dependency. | The pure function is the one place a real unit test pays off in this repo. |

### The `num_channels=2` change

`AudioBufferProcessor` always keeps `_user_audio_buffer` and `_bot_audio_buffer` as separate mono
streams regardless of `num_channels`; the channel count only governs `merge_audio_buffers()` (mono
`mix_audio` vs. stereo `interleave_stereo_audio`) and the `on_audio_data` event. So switching to
`num_channels=2` makes `merged.wav` a true L/R split while the per-speaker mono WAVs are unaffected.
`write_wav` must set `nchannels=2` for the merged file and `1` for the two per-speaker files.
Verify during implementation that `interleave_stereo_audio` tolerates the two buffers having
different lengths (the mono `mix_audio` path already does).

## Surfacing artifacts to the calling LLM (MCP protocol)

An MCP client (the orchestrating LLM) discovers a server through exactly two channels, so Stage 4
uses both:

1. **Tool descriptions** — FastMCP renders each tool's docstring as the description the LLM sees.
   The `start_browser_session` docstring lists every artifact `record_dir` produces and its format:
     - `kokoro_voice.wav` — mono, the tester's (our) voice.
     - `ember_voice.wav` — mono, the app bot's voice.
     - `merged.wav` — **stereo**, tester on the left channel, app bot on the right.
     - `events.json` — the full conversation event log (array of `{type, t, ...}`; same objects
       `listen()` returns).
     - `metrics.json` — computed test report; its top-level keys are documented in the `stop()`
       docstring.
2. **Return values** — `stop()` returns the concrete artifact paths so the LLM can read them
   immediately, instead of inferring the directory layout:
   ```json
   { "stopped": true,
     "artifacts": { "events": "/abs/record_dir/events.json",
                    "metrics": "/abs/record_dir/metrics.json",
                    "merged_wav": "/abs/record_dir/merged.wav",
                    "tester_wav": "/abs/record_dir/kokoro_voice.wav",
                    "app_bot_wav": "/abs/record_dir/ember_voice.wav" } }
   ```
   When `record_dir` is unset, `stop()` returns `{"stopped": true}` with no `artifacts` key. This
   changes the tool's return type from `bool` to `dict` — an intentional contract improvement.

## Metric definitions

All derived in one pass over the event log. Speech intervals are formed by pairing start/stop
events:

- **app_bot interval**: `app_bot_speech_started` → `app_bot_speech_stopped`.
- **tester interval**: `tester_speech_started` → `tester_speech_stopped` *or* `tester_speech_interrupted`.

An interval still open at teardown closes at `session_stopped.t`.

Metrics:

- **App response latency (user-perceived)** — per app-bot turn:
  `app_response_latency_secs = app_bot_speech_started.t − (preceding tester_speech_stopped.t)`.
  This is the **silence the synthetic user experiences** between finishing their utterance and
  hearing the app reply — the headline voice-UX number. Composition matters and must be documented
  so nobody misreads it as a server-compute time:
    - `tester_speech_stopped.t` is **playout-derived** (our `BotStoppedSpeakingFrame`) — the moment
      our Kokoro audio finished playing into the app's mic. Accurate; we control it.
    - `app_bot_speech_started.t` is the **VAD-detected onset** of the app's incoming audio. VAD
      *start* detection lags only ~tens of ms (Silero default) — the 1.0 s `stop_secs` bias applies
      to *stop*, not start, so it does **not** inflate this number.
    - The gap therefore bundles the **app's own endpointing + STT→LLM→TTS think time + TTS start**.
      That is the correct UX figure (the real perceived wait), but it is **not** a pure app-compute
      measurement. Document this in the `biases` notes.
  This is distinct from two other latencies in the system that this metric is NOT: our own playout
  latency (`speak` → our audio finished, already covered by `wait_for_playout`), and the app's
  utterance *duration*.

  **Direction is one-way on purpose.** We compute latency only for the **tester → app_bot**
  transition (how fast the app under test answers). We deliberately do **not** compute the reverse
  **app_bot → tester** latency: that would measure Claude's multi-second LLM round-trip plus
  Kokoro/arming delay — i.e. how slow *our own* synthetic tester is, which is not the system under
  test and is uninteresting. So `response_latency_secs` attaches **only to app-bot turns**, never to
  tester turns.
- **Talk-over windows** — intersections of tester intervals with app-bot intervals:
  `{start, end, duration_secs}`. Scores barge-in behaviour (Stage 3).
- **Dead-air gaps** — silent spans between the merged union of all speech intervals:
  `{start, end, duration_secs}`.
- **Talk time / ratio** — summed `tester_secs`, `app_bot_secs`, and `ratio_tester_over_app_bot`.
- **Utterance counts** — number of tester intervals, number of app-bot intervals.
- **Turn-by-turn transcripts** — `app_bot_transcript` + `tester_transcript` merged, sorted by `t`,
  each `{speaker, t, text}`. The app-bot turn carries its `response_latency_secs`.
- **Biases header** — `vad_stop_secs` (from `session_started`) plus notes: app-bot stop trails true
  speech end by ~`vad_stop_secs` (so app-bot talk time and post-bot dead-air are overestimated by
  ~that much), and batch Whisper means transcript *text* arrives late and is utterance-level, not
  word-accurate.

### `metrics.json` shape

```json
{
  "session": { "started_at": 1234.5, "stopped_at": 1290.1, "duration_secs": 55.6,
               "biases": { "vad_stop_secs": 1.0, "notes": ["...", "..."] } },
  "turns": [ {"speaker": "tester", "t": 1240.0, "text": "..."},
             {"speaker": "app_bot", "t": 1242.3, "text": "...", "response_latency_secs": 2.3} ],
  "app_response_latencies_secs": [2.3, 1.1],
  "talk_over_windows": [ {"start": 1250.0, "end": 1251.2, "duration_secs": 1.2} ],
  "dead_air_gaps":     [ {"start": 1244.0, "end": 1246.0, "duration_secs": 2.0} ],
  "talk_time": { "tester_secs": 12.4, "app_bot_secs": 31.0, "ratio_tester_over_app_bot": 0.40 },
  "utterances": { "tester": 4, "app_bot": 5 },
  "summary": { "mean_app_response_latency_secs": 1.7, "max_app_response_latency_secs": 2.3,
               "total_talk_over_secs": 1.2, "total_dead_air_secs": 2.0 }
}
```

### Edge cases (handle simply, no over-defensiveness)

- Empty / no-speech session → empty interval lists, zeroed summary, no crash.
- Start without matching stop → close at `session_stopped.t` (or the last event's `t` if no
  `session_stopped`).
- An app-bot turn with no preceding tester stop (app speaks first) → omit `response_latency_secs`
  for that turn rather than emitting a negative or null number.
- `session_started` absent (defensive read) → `vad_stop_secs` falls back to the value the caller
  passes in; the biases header still records it.

## Verify

1. **Unit** — `tests/test_metrics.py`: build a synthetic event list with known intervals, assert
   each metric (latency, talk-over, dead-air, ratio, counts, transcript ordering, biases header).
   `uv run pytest`.
2. **End-to-end** — `scripts/e2e_readme_call.py` with `record_dir=temp/e2e_readme_call`: confirm
   `events.json`, `metrics.json`, and a stereo `merged.wav` are written; cross-check one turn's
   `response_latency_secs` against the waveform in an audio editor.

## Quality checks

`uv run ruff check src/ tests/`, `uv run ruff format src/ tests/`, `uv run pyright src/`,
`uv run pytest`.
