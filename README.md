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
| `start_call(deployment_id, toocan_url?, user_id?)` | Ask the Toocan backend to create a Daily room and spawn the bot. Joins that room as the synthetic user. Returns `{room_id, room_url, joined}`. |
| `speak(text)` | Synthesize `text` with Kokoro TTS and stream the audio into the Daily room. Returns when the frames are queued — not when audio has finished playing — so the LLM can barge in at any time. |
| `listen(timeout=30)` | Block until the bot completes an utterance (VAD-segmented). Returns the transcribed text, or `""` on timeout. A long bot reply produces multiple utterances; call `listen()` in a loop. |
| `stop()` | Tear down the call cleanly. |
| `start()` | Start the pipeline without a Toocan call (uses Pipecat's local playground transport). Kept for parity with upstream. |
| `list_windows()` / `screen_capture(window_id)` / `capture_screenshot()` | Screen-sharing tools, inherited from upstream. Not part of the Toocan test loop. |

### Example session

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
| `agent.py` | Defines `PipecatMCPAgent` — the wrapper that owns the Pipecat pipeline and acts as the synthetic user-side participant in the Daily room. The bot is the other participant; both are headless pipelines on the same room. |

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

## License

BSD-2-Clause (inherited from upstream).
