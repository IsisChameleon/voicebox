# qz-mcp-server: Audio Pipeline Architecture Notes

Companion to the Excalidraw diagram.

## Process Model

Two OS processes, connected by `multiprocessing.Queue` (spawn mode):

| Process | Runs | Why separate? |
|---------|------|---------------|
| **MCP Server** (parent) | FastMCP on stdio/streamable-http | MCP protocol owns stdio; can't share with Pipecat |
| **Pipecat Child** (spawned) | bot() command loop + full Pipecat pipeline | Needs its own event loop, audio threads, WebRTC stack |

## Queues (where things get buffered)

| Queue | Type | Direction | What sits in it |
|-------|------|-----------|-----------------|
| `cmd_queue` | `multiprocessing.Queue` | MCP server -> child | `{"cmd": "speak", "text": "..."}` or `{"cmd": "listen"}` |
| `response_queue` | `multiprocessing.Queue` | child -> MCP server | `{"text": "transcribed speech"}` or `{"ok": true}` |
| `user_speech_queue` | `asyncio.Queue` (in-process) | UserAggregator -> agent.listen() | Completed user utterances (after SmartTurn + VAD) |
| Pipecat pipeline frames | Internal frame queues | Between pipeline processors | Audio frames, text frames, LLM frames |

## Full Turn: Claude speaks, Bot replies

### Outbound (Claude -> Bot)

1. Claude calls `speak("Hello")` via MCP tool
2. `server.py` puts `{"cmd": "speak", "text": "Hello"}` on `cmd_queue`
3. `bot()` loop reads the command, calls `agent.speak("Hello")`
4. Agent queues `LLMFullResponseStartFrame` + `LLMTextFrame` + `LLMFullResponseEndFrame`
5. Kokoro TTS synthesizes audio frames from the text
6. Assistant Aggregator passes frames through
7. Daily Transport `.output()` sends audio into the WebRTC room
8. Toocan bot (other participant in the room) receives the audio

### Inbound (Bot -> Claude)

9. Toocan bot generates its reply (its own LLM + TTS pipeline)
10. Bot's audio enters the Daily room
11. Daily Transport `.input()` receives the audio + RNNoise filters it
12. Whisper STT (MLX on macOS) transcribes the audio to text
13. User Aggregator collects text frames
14. SmartTurn V3 + Silero VAD detect end-of-turn (stop_secs=0.2)
15. `on_user_turn_stopped` fires, puts transcription on `user_speech_queue`
16. `agent.listen()` awaits on `user_speech_queue`, gets the text
17. `bot()` loop sends `{"text": "bot's reply"}` on `response_queue`
18. MCP server polls `response_queue` (timeout=0.5s loop), gets the response
19. Claude receives the transcribed bot reply

## Key Design Decisions

- **`multiprocessing.spawn`**: Avoids forking from async context (would copy event loop state, file descriptors, locks)
- **Polling with timeout**: `_wait_for_command_response` uses 0.5s timeout + health check loop, not blocking `get()` — allows cancellation and dead-process detection
- **SmartTurn V3 + Silero VAD**: Determines when the bot has finished speaking. `stop_secs=0.2` is aggressive — tuned for bot speech which has predictable pauses
- **ParallelPipeline**: Audio branch (STT -> Aggregator -> TTS) runs alongside Vision branch — screen capture doesn't block audio

## Limitations

- **Text-only bridge**: Claude sees transcribed text, not raw audio. Whisper errors (hallucinations, missed words) are invisible to Claude
- **No barge-in control**: If Claude's TTS is still playing when the bot starts replying, both audio streams overlap in the Daily room. No explicit mechanism to wait for TTS completion before calling listen()
- **Single-threaded command loop**: `bot()` processes one command at a time — can't speak and listen concurrently
- **VAD sensitivity**: `stop_secs=0.2` may trigger false end-of-turn during natural bot pauses, splitting one reply into multiple listen() returns
- **Latency stack**: Each turn adds: IPC queue serialization + Whisper STT inference + SmartTurn detection delay + queue polling (up to 0.5s). Realistic per-turn overhead: 1-3s on top of actual speech
- **No echo cancellation**: The pipeline hears its own TTS output via the Daily room. RNNoise helps but isn't AEC — could cause feedback loops or self-transcription
- **Process lifecycle**: If the child process dies mid-conversation, the MCP server only discovers it on the next `_check_process_alive()` call (up to 0.5s later)
- **start_call() is synchronous-ish**: The HTTP POST to Toocan and process spawn happen sequentially. If Toocan is slow, the MCP tool call blocks

## start_call() Flow (Toocan-specific)

```
Claude calls start_call(deployment_id="xyz")
  -> server.py POSTs to {toocan_url}/call/pipecat/start
  -> Toocan backend creates Daily room, spawns bot, returns:
     { dailyRoomUrl, dailyToken, roomId }
  -> start_pipecat_process(room_url, token)
     -> spawns child process
     -> child calls bot(DailyRunnerArguments(room_url, token))
     -> bot joins the SAME Daily room as the Toocan bot
  -> returns { room_id, room_url, joined: true } to Claude
```

Both bots are now in the same Daily room. Claude's pipeline acts as a synthetic user.
