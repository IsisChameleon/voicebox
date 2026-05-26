# qz-mcp-server

MCP server that lets an LLM client (Claude Code, Cursor, Codex, ...) act as a **synthetic voice user** against a Quarterzip "Toocan" deployment.

The LLM sends text through MCP tools; this server uses [Pipecat](https://github.com/pipecat-ai/pipecat) to convert it to audio, joins the Daily WebRTC room created by the Toocan backend, talks to the bot, and converts the bot's audio replies back to text returned through MCP. The result is a fully scriptable voice conversation with a deployed agent — useful for end-to-end testing and regression scenarios.

Forked from [`pipecat-ai/pipecat-mcp-server`](https://github.com/pipecat-ai/pipecat-mcp-server).

## Topology

```
Claude (LLM)  ──HTTP/JSON-RPC──►  qz-mcp-server  ──multiprocessing.Queue──►  Pipecat child process
                                                                                      │
                                                                                      │ audio (WebRTC)
                                                                                      ▼
                                                                              Daily room ◄── Toocan bot
                                                                                              ▲
                                                                              spawned by qz-manage (:8765)
```

See `docs/architecture-notes.md` for the data-flow walkthrough.

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A running local Toocan stack (`docker compose up` in [toocan-app](https://github.com/quarterzip/toocan-app)) exposing the API on `localhost:8765` and the SvelteKit client on `localhost:5173`
- A deployment ID to test against (visible in the client URL, e.g. `localhost:5173/d/<id>`)

By default the agent uses local models — Whisper for STT, Kokoro for TTS — so no API keys are needed in this server's environment. The Toocan backend supplies its own credentials.

## Install

```bash
git clone https://github.com/IsisChameleon/qz-mcp-server.git
uv tool install -e /path/to/qz-mcp-server[daily]
```

The `[daily]` extra pulls in `daily-python` for the WebRTC transport.

## Run

```bash
qz-mcp-server
```

The server listens on `http://localhost:9090/mcp` (streamable-HTTP transport, MCP protocol revision 2025-11-25).

## Register with your MCP client

Claude Code:

```bash
claude mcp add qz-voice-test --transport http http://localhost:9090/mcp --scope user
```

Cursor (`~/.cursor/mcp.json`):

```json
{ "mcpServers": { "qz-voice-test": { "url": "http://localhost:9090/mcp" } } }
```

## MCP tools exposed

| Tool | Purpose |
|---|---|
| `start_call(deployment_id, toocan_url?, user_id?)` | **Bot mode.** Ask the Toocan backend to create a Daily room and spawn the bot. Joins that room as the synthetic user (direct Daily peer). Returns `{room_id, room_url, joined}`. |
| `start_browser_session(url, headless?, cdp_port?, audio_port?)` | **Browser mode.** Launch a Playwright-controlled Chromium with the qz audio shim injected, navigate to `url`, expose CDP. The shim hijacks the page's mic (fed by Kokoro) and tees the page's WebRTC remote audio into Whisper. Returns `{cdp_endpoint, audio_ws_url}`. Drive the UI with any Playwright client that can `connect_over_cdp`. |
| `speak(text)` | Synthesize `text` with Kokoro TTS and stream the audio out (into Daily in bot mode, into the shim's synthetic mic in browser mode). Returns when frames are queued — not when audio has finished playing. |
| `listen(timeout=30)` | Block until the other side completes an utterance (VAD-segmented). Returns the transcribed text, or `""` on timeout. A long reply produces multiple utterances; call `listen()` in a loop. |
| `stop()` | Tear down the call (and the browser session, if one is open). |
| `list_windows()` / `screen_capture(window_id)` / `capture_screenshot()` | Screen-sharing tools, inherited from upstream. Not part of the Toocan test loop. |

### Example session — bot mode (direct-peer)

```jsonc
// 1. start
{"name": "start_call", "arguments": {"deployment_id": "8tc5je"}}
// → {"room_id": "26752fc000ae", "room_url": "https://quarterzip-dev.daily.co/26752fc000ae", "joined": true}

// 2. greet
{"name": "speak", "arguments": {"text": "Hi! Tell me what Linear is in one short sentence."}}

// 3. hear
{"name": "listen", "arguments": {"timeout": 45}}
// → "Linear is a project management tool built for software teams to track issues,"

// 4. end
{"name": "stop", "arguments": {}}
```

### Example session — browser mode (drive a real client UI)

```jsonc
// 1. launch a Playwright Chromium with the audio shim injected, navigate to the app
{"name": "start_browser_session", "arguments": {"url": "http://localhost:3000"}}
// → {"cdp_endpoint": "http://localhost:9222", "audio_ws_url": "ws://localhost:9091"}

// 2. attach a Playwright client (any flavor: @playwright/mcp, playwright-cli, your own)
//    via chromium.connect_over_cdp("http://localhost:9222") and drive the UI:
//      - log in
//      - navigate to the book / "Start reading" / whatever the call entry point is
//    The page calls getUserMedia → the shim returns a synthetic mic stream that
//    the MCP server feeds.

// 3. speak — the page's WebRTC peer sends OUR Kokoro audio to the bot
{"name": "speak", "arguments": {"text": "Hi Ember! Tell me about this book."}}

// 4. listen — the bot's remote audio track is teed to Whisper via the shim
{"name": "listen", "arguments": {"timeout": 45}}
// → "Hello, welcome. I'm so excited to have you..."

// 5. end the call either by saying "goodbye" (if the bot supports
//    UserVerballyInitiatedDisconnect — readme app does), or click the
//    red-cross / End-reading button via Playwright, then:
{"name": "stop", "arguments": {}}
```

See `scripts/e2e_readme_call.py` for a complete driver that does
login → navigate → call → conversation → end against the readme app.

## Architecture notes

- The MCP server (parent process) hosts the FastMCP HTTP endpoint. A separate Pipecat child process runs the audio pipeline — they communicate over `multiprocessing.Queue`. This keeps Pipecat's event loop and audio threads off the MCP request path.
- Audio never crosses the MCP boundary. MCP carries text and control only; audio flows out-of-band over Daily WebRTC.
- We disable Pipecat's default RTVI processor (`enable_rtvi=False`). RTVI is meant for browser SDK clients to render UI; nothing on this side subscribes to it. Leaving it on creates a validation-error feedback loop with the bot's RTVI processor that degrades end-of-turn latency significantly.
- `speak()`/`listen()` wait for the Daily transport's `on_client_connected` event before pushing frames, so the first call after `start_call` doesn't race the room join.

### File map

| File | Role |
|---|---|
| `server.py` | FastMCP HTTP surface — the tools Claude calls. Lives in the parent process. No audio code. |
| `agent_ipc.py` | The shared mailbox. Loaded into both parent and child; each side uses different functions (`send_command` in parent, `read_request`/`send_response` in child). Owns the multiprocessing queues and the child-process lifecycle. |
| `bot.py` | The child's tiny command loop: `read → dispatch → respond`. Runs in the child process only. |
| `agent.py` | Defines `PipecatMCPAgent` — the wrapper that owns the Pipecat pipeline. Dispatches on `runner_args` type: `DailyRunnerArguments` → direct Daily peer (bot mode); `BrowserShimRunnerArguments` → `WebsocketServerTransport` for the in-browser shim (browser mode). STT/TTS/VAD identical in both modes. |
| `runner_args.py` | The `BrowserShimRunnerArguments` dataclass (host/port/sample_rate) — pipecat doesn't ship one for plain WebSocket-server transports. |
| `raw_pcm_serializer.py` | Tiny `FrameSerializer` that exchanges raw 16-bit LE mono PCM with the browser shim. No protobuf, no envelope — the shim just sends/receives `Int16Array` buffers. |
| `shim.js` | The browser shim. Injected via Playwright `addInitScript` so it runs before any page code. Overrides `getUserMedia` to return a synthetic mic stream backed by a `MediaStreamTrackGenerator` fed from the WebSocket, and wraps `RTCPeerConnection` to tee every inbound audio `track` via a `MediaStreamTrackProcessor` back to the server. Defensive: only patches APIs that exist (skips cleanly on `about:blank` etc.). |
| `browser_session.py` | Manages the Playwright child process: launches Chromium with `--remote-debugging-port=<cdp_port>` + `--use-fake-ui-for-media-stream`, registers `shim.js` via `add_init_script`, navigates to the user-supplied URL, parks until told to stop. |

### What happens on `start_call`

The full instantiation chain when Claude calls `start_call(deployment_id="…")`:

```
1.  Claude → MCP                   tools/call start_call(deployment_id="…")
2.  server.py:start_call()         POST /call/pipecat/start → Toocan returns {room_url, token}
3.  agent_ipc.start_pipecat_process(room_url, token)
                                   create cmd/response queues
                                   multiprocessing.Process(target=run_pipecat_process, …)
                                   → spawn CHILD process
4.  [CHILD] agent_ipc.run_pipecat_process(cmd_q, resp_q, room_url, token)
                                   stash queue handles in child's module globals
                                   runner_args = DailyRunnerArguments(room_url, token)
                                   asyncio.run(bot(runner_args))
5.  [CHILD] bot.py:bot(runner_args)
                                   agent = await create_agent(runner_args)
                                   await agent.start()       ← pipeline is built here
                                   while True: read_request → dispatch → send_response
6.  [CHILD] agent.py:create_agent(runner_args)
                                   pick DailyParams / TransportParams based on runner_args type
                                   transport = await create_transport(runner_args, …)
                                   return PipecatMCPAgent(transport, runner_args)
7.  [CHILD] PipecatMCPAgent.start()
                                   build Whisper STT, Kokoro TTS, SmartTurn, Silero VAD
                                   build Pipeline([transport.input → … → transport.output])
                                   self._pipeline_task = PipelineTask(pipeline, enable_rtvi=False)
                                   self._pipeline_runner = PipelineRunner(handle_sigterm=True)
                                   register on_client_connected / on_user_turn_stopped handlers
                                   asyncio.create_task(self._pipeline_runner.run(self._pipeline_task))
                                   ✓ pipeline is now running and the synthetic user has joined the room
```

After step 7, subsequent `speak`/`listen` MCP tool calls flow through the same `agent_ipc` queue mailbox into the already-running child, where `bot.py`'s loop dispatches them to `PipecatMCPAgent.speak()` / `.listen()`.

### What happens on `start_browser_session`

```
1.  Claude → MCP                   tools/call start_browser_session(url="http://localhost:3000")
2.  server.py:start_browser_session()
                                   audio_ws_url = "ws://localhost:9091"
                                   start_pipecat_process(BrowserShimRunnerArguments(port=9091, …))
                                   start_browser(url, audio_ws_url, cdp_port=9222, …)
3.  [CHILD-1: pipecat]             agent_ipc.run_pipecat_process(…, BrowserShimRunnerArguments)
                                   create_agent dispatches on type → WebsocketServerTransport
                                     with RawPCMSerializer @ 48 kHz, no add_wav_header, no RNNoise
                                   pipeline = transport.input → STT(Whisper) → … → TTS(Kokoro) → transport.output
                                   websocket listening on :9091
4.  [CHILD-2: browser]             browser_session._run_browser_async
                                   read shim.js, prepend `window.__QZ_AUDIO_WS_URL__ = "ws://localhost:9091"`
                                   chromium.launch(args=[--remote-debugging-port=9222, --use-fake-ui-for-media-stream])
                                   context.add_init_script(shim)
                                   page.goto(url)
                                   ready_event.set()  ← MCP tool returns
5.  [page]                         shim runs before any page code:
                                     - opens WebSocket to ws://localhost:9091
                                     - overrides navigator.mediaDevices.getUserMedia
                                     - wraps window.RTCPeerConnection
                                   when the page later calls getUserMedia({audio}), the shim
                                   returns MediaStream([MediaStreamTrackGenerator]) — the
                                   "synthetic mic". When the page creates an RTCPeerConnection,
                                   the wrapper subscribes to its `track` events.
6.  External Playwright client     connect_over_cdp("http://localhost:9222")
    (Claude's @playwright/mcp,     drives the UI: login, navigate, click "Start reading"
     playwright-cli, etc.)         → the app calls getUserMedia (shim returns synthetic mic)
                                   → the app creates RTCPeerConnection to Daily / its server
                                   → bot starts streaming audio → shim's track-event hook
                                     pipes it via MediaStreamTrackProcessor → WebSocket → pipecat
7.  [Claude] speak("hi ember")     pipecat Kokoro renders audio → WebsocketServerTransport
                                   writes Int16 PCM frames over the WS → shim writes
                                   AudioData chunks into the MediaStreamTrackGenerator → the
                                   page's WebRTC peer encodes Opus → bot hears it
8.  [Claude] listen()              VAD/SmartTurn waits for the bot's utterance to end →
                                   on_user_turn_stopped fires with the Whisper transcript →
                                   returned to MCP
9.  [Claude] stop()                terminates pipecat child + browser child
```

### Tradeoffs and known sharp edges (browser mode)

- **Headless Chromium works**, but the shim relies on `MediaStreamTrackGenerator` / `MediaStreamTrackProcessor` (modern Chromium-only). Tested with Playwright 1.60 + bundled Chromium 1223.
- **Sample rate** is hardcoded to 48 kHz on the wire. Daily/WebRTC tracks in Chrome are typically 48 kHz mono; the shim logs the first AudioData's `sampleRate` to `window.__qzShim.outboundSampleRate` for verification.
- **VAD `stop_secs`** is tuned to 1.0 s in browser mode (vs. 0.2 s for the Toocan flow) — TTS-clean bot audio in Toocan was fine with aggressive VAD, but a real WebRTC stream with natural pauses needs more breathing room to avoid single-word transcripts.
- The shim's `RTCPeerConnection` wrap won't catch peer connections inside cross-origin iframes or Web Workers — neither applies to the readme/Toocan flow, but it's a real limitation for apps using Daily Prebuilt's `<DailyIframe>` (workaround: hook `<audio>` elements via `MutationObserver` + `captureStream()`).
- We pre-grant `microphone` permission via `--use-fake-ui-for-media-stream` and `context = new_context(permissions=["microphone"])`. No permission prompt to dismiss.

## License

BSD-2-Clause (inherited from upstream).
