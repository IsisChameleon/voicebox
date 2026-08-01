# CLAUDE.md — voicebox

Guidance for Claude Code working in this repo. Read this first; it captures the architecture and
the non-obvious traps so you don't have to re-derive them.

## What this is

An **MCP server that gives an LLM agent voice + ears inside a browser**, so the LLM can play a
synthetic *user* against any web voice app (Daily, LiveKit, plain `RTCPeerConnection`) without the
app knowing. The agent drives a Playwright Chromium with an audio shim injected: the shim fakes the
page's microphone (fed by Kokoro TTS from this server) and tees the bot's remote WebRTC audio back
to Whisper STT.

Local models by default — Whisper (STT) + Kokoro (TTS) — so **no API keys needed**.

## Architecture — three processes

```
Claude (LLM) ─HTTP/JSON-RPC─► voicebox MCP server (parent, server.py)
                                   │  multiprocessing.Queue (IPC)
                                   ├─► CHILD 1: pipecat audio agent (bot.py → agent.py)
                                   │            WebsocketServerTransport on :9091
                                   │            raw 16-bit PCM: Kokoro 48 kHz out, Whisper 16 kHz in
                                   └─► CHILD 2: Playwright Chromium (browser_session.py)
                                                --remote-debugging-port=9222 + shim.js injected
                                                parks until told to stop
```

- **Audio never crosses the MCP boundary.** MCP carries text/control only. Audio flows out-of-band
  over a raw-PCM WebSocket between `shim.js` (in the page) and the pipecat child.
- **UI driving is delegated to an EXTERNAL Playwright client** over CDP. voicebox exposes the CDP
  endpoint (`http://localhost:9222`) and the client (`@playwright/mcp --cdp-endpoint=...`, or
  `chromium.connect_over_cdp(...)`) logs in / clicks / navigates. voicebox itself has **no**
  `navigate`/`click`/`snapshot` tools, and the parent holds **no** Playwright handle to the page.

## File map

| File | Role |
|---|---|
| `src/voicebox/server.py` | FastMCP HTTP surface (parent). The 4 tools Claude calls. No audio code. Ports: MCP 9090, audio 9091, CDP 9222. |
| `src/voicebox/agent_ipc.py` | The parent↔pipecat-child mailbox: `multiprocessing.Queue` + child lifecycle. Uses `spawn` (not fork). Full-duplex: requests carry a correlation `id`; a response-router task resolves per-id futures, so commands overlap and responses may arrive out of order. |
| `src/voicebox/bot.py` | The pipecat child's command loop: reads requests and spawns one task per command (`listen`/`speak`); `stop` cancels in-flight tasks and exits. |
| `src/voicebox/agent.py` | `PipecatMCPAgent` — owns the Pipecat pipeline behind `WebsocketServerTransport`. STT/TTS/VAD/turn config lives here. |
| `src/voicebox/runner_args.py` | `BrowserShimRunnerArguments` dataclass (host, port, mic_rate, tap_rate, record_dir). Pipecat ships none for plain WS-server transports. |
| `src/voicebox/raw_pcm_serializer.py` | Tiny `FrameSerializer`: raw 16-bit LE mono PCM, no protobuf/envelope. |
| `src/voicebox/processors/kokoro_tts.py` | Kokoro TTS service (`voice_id="af_heart"`). |
| `src/voicebox/shim.js` | The browser shim, injected via `addInitScript` before page code. Overrides `getUserMedia` (Hook 1) and wraps `RTCPeerConnection` (Hook 2). Diagnostics on `window.__voiceShim`. |
| `src/voicebox/browser_session.py` | Manages the Playwright child process. Supports `user_data_dir` (persistent default context, CDP-coherent — exposed via `start_browser_session` for session reuse). See the CDP context-split trap below for why `storage_state` is intentionally not offered. |
| `scripts/smoke_browser_shim.py` | Audio-path smoke test (no readme app needed). The reference for `connect_over_cdp` + reading `__voiceShim`. |
| `scripts/e2e_readme_call.py` | Full e2e driver: login → navigate → call → speak/listen → end, against the readme app. Canonical CDP-driving example. Runs `headless=True` and dumps WAVs. |

## MCP tools (`server.py`)

- `start_browser_session(url, headless=False, cdp_port=9222, audio_port=9091)` → `{cdp_endpoint, audio_ws_url}`
- `speak(text, wait_for_playout=False, wait_for_turn=False, when=None, timer_secs=0.0)` → `{queued: true}` when queued. `wait_for_playout=True` returns only after OUR OWN audio finishes playing, adding `{started_at, finished_at, interrupted}` (it waits for our Kokoro audio — NOT the app bot). `wait_for_turn=True` waits until the app bot is silent, then speaks (the polite path). `when=<event>, timer_secs=N` arms a one-shot barge-in: returns `{armed: true}` immediately, then N s after the next `when` event (e.g. `"app_bot_speech_started"`) speaks over the bot. `wait_for_turn`/`when` gate WHEN we start; `wait_for_playout` gates WHEN the call returns.
- `listen(timeout=30, cursor=0)` → `{events: [...], cursor}` — timestamped conversation events (`session_started`, `session_stopped`, `client_connected/disconnected`, `app_bot_speech_started/stopped`, `app_bot_transcript`, `tester_speech_started/stopped/interrupted`, `tester_transcript`, `tester_barge_in_armed/fired`); pass the returned cursor back to resume; empty `events` on timeout. Two parties: **app_bot** = the app's voice agent under test, **tester** = our synthetic user. Event vocabulary lives in `events.py`.
- `stop()` → tears down pipecat child + browser child

## Non-obvious facts & traps (verified, don't re-derive)

- **Asymmetric sample rates are intentional.** Browser→pipecat tap = **16 kHz** because
  `mlx_whisper.transcribe()` has no `sample_rate` param and hard-assumes 16 kHz; the shim's outbound
  `AudioContext` does the 48→16 resample. pipecat→browser mic = **48 kHz** to match the page's native
  AudioContext. See `runner_args.py` docstring, `agent.py:280-291`, `shim.js:31-37`.
- **VAD `stop_secs=1.0s`** (`agent.py:101`), not pipecat's default 0.2s — 0.2s chops remote WebRTC
  speech mid-sentence into single-word transcripts. Consequence: utterance "end" wall-clock lands
  ~1 s after speech truly stops.
- **The shim taps via Web Audio, NOT WebCodecs `MediaStreamTrackProcessor`** (`shim.js:156-304`).
  `MediaStreamTrackProcessor` drops silence on a remote track → sparse stream → WAV plays ~3× fast.
  Web Audio is pulled at a fixed rate and fills silence with zeros, preserving real-time pacing.
- **`RTCPeerConnection` track events are deduped by `track.id`** (`shim.js:181-246`) — Daily opens
  multiple peer connections and the same logical audio surfaces more than once (commits `26bd9c5`,
  `2b3d7f1`).
- **`enable_rtvi=False`** (`agent.py:133-137`) — we're a headless synthetic user; transcripts reach
  Claude via `listen()`'s return value, not RTVI data-channel notifications.
- **Timestamps surface via the event log** (Stage 2): a pipeline observer in `agent.py` turns
  `VADUserStarted/StoppedSpeakingFrame` (wall-clock `timestamp`), `BotStarted/StoppedSpeakingFrame`
  (our playout span) and `UserTurnStoppedMessage` into `listen()` events. Still true: STT is
  batch+VAD-segmented, so *per-word* receive timestamps are NOT obtainable — utterance-level only,
  and `bot_speech_stopped.t` lands ~`vad_stop_secs` (1.0 s) late by construction.
- **`record_dir` exists** (`runner_args.py`, `agent.py:105-128,194-231`): set it and `stop()` writes
  user/bot/merged WAVs via `AudioBufferProcessor`. Snapshot buffers BEFORE `stop_recording()` — it
  resets them.
- **Ending a `PipelineWorker` means `stop_when_done()`, not `stop()`.** `BaseWorker.stop()` cancels
  job groups and sets the finished event but never ends the pipeline run, so `WorkerRunner.run()`
  never returns — a test that awaits it hangs forever. `stop_when_done()` queues the `EndFrame`,
  which is what `agent.py:449` does in production.
- **`spawn`, not fork** (`agent_ipc.py:24`) — forking from the async MCP context copies the event
  loop / fds / locks and breaks.
- **Shim is defensive**: every hook is gated on the API existing; on insecure origins (non-localhost
  http, about:blank) or missing WebCodecs the hook is skipped silently. `window.__voiceShim` always
  exists with diagnostics (`installed`, `wsReady`, `inboundChunks`, `outboundChunks`, `errors`, …).
- **Known limitation:** the `RTCPeerConnection` wrap can't reach peer connections inside cross-origin
  iframes or Web Workers (e.g. Daily Prebuilt `<DailyIframe>`).
- **CDP context split (verified — why only `user_data_dir` is offered):** `chromium.launch()` +
  `new_context()` puts the shim page in a non-default browser context. A client attached via
  `connect_over_cdp` *sees the page* under `contexts[0]` but cookie ops (`context.cookies()`,
  `context.storage_state()`) hit the **default** context and come back empty. So a Playwright
  `storage_state` would LOAD (the page sends the cookie — confirmed by echo test) but could not be
  SAVED via CDP — you couldn't generate it from within a session. `launch_persistent_context`
  (`user_data_dir`) uses the default context, so it's fully CDP-coherent; it's the only session-reuse
  knob exposed.

## Driving the UI from another agent

`start_browser_session` returns `attach_hint` — paste it verbatim to wire up
`playwright-cli`:

```bash
playwright-cli attach --cdp http://localhost:9222
```

**Why `attach` and not `open` (verified from playwright-core source):**
`playwright-cli open` with no URL runs `goto about:blank` on the current page
(`playwright-core/lib/tools/cli-client/program.js:128`) — over CDP that is
voicebox's shim tab, and blanking it destroys the audio shim. `attach` only
takes a `snapshot` (same file, the `case "attach"` branch) and navigates
nowhere.

**Do not open new tabs.** The audio shim (`shim.js`) is page-scoped to the
voicebox-owned tab. A second tab connecting to WS :9091 triggers pipecat's
"only one client" kick and causes a 1 Hz reconnect storm.

## Dev workflow

```bash
uv sync                                   # install (uv, not pip; deps in pyproject.toml)
uv tool install -e .                      # install the `voicebox` CLI entry point
voicebox                                  # run MCP server on http://localhost:9090/mcp
uv run python scripts/smoke_browser_shim.py   # audio-path smoke test (no app needed)
uv run python scripts/e2e_readme_call.py      # full e2e against a localhost:3000 app
```

## Quality checks (run before committing)

```bash
uv run pytest -q                    # unit tests (pytest-asyncio, auto mode)
uv run ruff check src/ tests/       # lint: docstring (D) + import (I) rules, line-length 100
uv run ruff format src/ tests/      # format
uv run pyright src/                 # types
```

The unit suite covers the pure/mockable parts (metrics, browser-session startup, timing
instrumentation). The audio path itself is verified by the two `scripts/` drivers, which need a
real browser (and, for e2e, a running voice app on `localhost:3000`) — anything marked 🔴 in a
spec is live-only and cannot be proven by `pytest`.

## Branch discipline (multi-task branches)

Established 2026-08-01; the repo predates it, so older branches have none of this.

- **`BUILDLOG.md`** at the repo root — append-only, numbered (`D1`, `D2`, …). One entry per
  decision, written *when the decision is made*: what was decided, why, what was rejected.
  Reversing a decision gets a new entry pointing back at the old one; entries are never rewritten.
- **`docs/walkthroughs/<branch>.md`** — created at branch start with a task → commit → evidence
  status table, updated as each task lands, marked complete before the review/PR pass. This is the
  review surface: a reviewer should not have to re-derive the diff.
- **`docs/artefacts/<branch>/t-<task>-<slug>.md`** — one per task, written when the task lands.
  *Captured proof, not narrative*: real test output, greps, probe transcripts, pasted verbatim.
  Opens by naming the success criteria it evidences (with the spec's path), and closes with a
  "not covered" section — untested paths, 🔴 live-only stories, scope cuts.

Cite a task's commit in the walkthrough **after** the commit exists — `git commit --amend` to
insert the sha changes that sha, leaving a dead reference. Land the code, read the sha, then
commit the doc update.

## Conventions

- Python ≥ 3.11, `uv` for everything. Google-style docstrings (ruff `D` enforced).
- License header (BSD-2-Clause, "Copyright (c) 2026, Daily") at the top of every `.py` — copy the
  existing block when adding files.
- Single session at a time: ports 9090/9091/9222 are pinned unless overridden via tool args.
- **All run artifacts go under `temp/` (gitignored, never committed).** Point `record_dir` at
  `temp/<run-name>` for any dogfood/manual run (e.g. `temp/dogfood`), and the `scripts/` drivers
  write there too (`temp/e2e_readme_call`). Treat it as a scratch dir: WAVs, PNGs, `events.json`,
  run logs.
