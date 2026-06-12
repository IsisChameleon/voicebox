# Architecture review — bugs & flaws

*2026-06-11. Review of the voicebox MCP server against pipecat 1.2.1 (installed). Companion docs:
[voice-testing-landscape.md](voice-testing-landscape.md) (competitive research),
[upgrade-roadmap.md](upgrade-roadmap.md) (staged plan), `architecture-stages.html` (diagram).*

## Verified bugs

**1. The synthetic user's speech is silently cancelled whenever the bot talks — pipecat's
interruption machinery is on by default.**
In `agent.py:90-103` only the `stop` turn strategy is configured. The `start` strategies therefore
default to `[VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()]` with
`enable_interruptions=True` [1]. When the remote bot's audio trips VAD, the user aggregator calls
`broadcast_interruption()` [2], which cancels in-flight TTS and flushes queued output audio.
Consequence: if Claude `speak()`s and the bot starts answering (or coughs, or plays a noise) before
Kokoro finishes streaming, the rest of the utterance is silently dropped — and `speak` already
returned `true`. This is exactly backwards for a tool whose job is to impersonate a human tester:
humans don't instantly mute themselves, and you can never test talk-over because pipecat actively
prevents your side from overlapping. Fix: pass start strategies with `enable_interruptions=False`
(or make it a `speak` arg).

**2. The child's command loop is strictly serial — you cannot `speak` while a `listen` is
pending.** `bot.py:36-62` does `read_request()` → `await asyncio.wait_for(agent.listen(), ...)` →
respond, one command at a time. A `speak` issued during a 30 s `listen` sits in the queue until the
listen resolves. This is the hard architectural blocker for interruption: even if bug 1 were fixed,
Claude mechanically can't inject speech while listening. Fix: dispatch each command as an
`asyncio.create_task` and tag responses with the request's correlation ID.

**3. No request↔response correlation on the IPC queues — cancellation or concurrency desyncs the
protocol.** `send_command` (`agent_ipc.py:209-241`) writes to one shared cmd queue and reads the
next item off one shared response queue. Two failure modes:

- If an MCP tool call is cancelled (client timeout/disconnect), the child still eventually puts its
  response; the *next* command's caller reads that stale response — a `speak` can receive a
  `listen`'s `{"text": ...}` forever after, off-by-one.
- FastMCP in `stateless_http` mode will happily serve concurrent tool calls; two parent coroutines
  polling the same response queue race, and whichever executor thread wins gets whichever response
  arrives first.

**4. Errors masquerade as transcripts, and any error kills the child.** `bot.py:59-62` responds
`{"text": str(e)}` — keyed `"text"`, not `"error"` — so a `listen` failure returns the exception
message to Claude *as if the bot said it*, and the parent's `"error" in response` check in
`agent_ipc.py:237` never fires. Then it `break`s, so one transient error (e.g. a Whisper hiccup)
tears down the whole command loop while the parent still thinks the session is alive (next command
raises "Voice agent process has stopped" at best, or hangs until the process is noticed dead).

**5. The disconnect sentinel is injected as fake speech.** `agent.py:151` puts the literal string
`"I just disconnected, but I might come back."` into the transcript queue. Claude receives this
from `listen()` indistinguishable from a real bot utterance — a tester would log it as something
the app said. Should be a structured out-of-band event (see redesign below).

**6. Second `getUserMedia(audio)` call steals the mic.** `makeSyntheticMicStream()`
(`shim.js:123-130`) reassigns the module-level `micWriter` on every call. Apps commonly call
`getUserMedia` more than once (mic-check UI, device-change handler, reconnection). The track
actually attached to the RTCPeerConnection keeps the *old* generator, which no longer receives any
frames — the call goes silent with no error anywhere. Inbound Kokoro audio should fan out to all
live generators (or at least the ones whose tracks aren't ended).

## Smaller flaws

- **`stop()` doesn't reap the child.** `server.py:158-175` sends the `stop` command but never calls
  `stop_pipecat_process()`, so the process object/queues linger until the next
  `start_browser_session` or server exit. And if the child hangs during `stop`,
  `_wait_for_command_response` polls forever — the MCP call never returns (no overall parent-side
  timeout on any command).
- **`listen` timeout semantics vs `speak` returning early.** `speak` returns when frames are
  *queued* (`server.py:148-154`); Kokoro playout of a long question may take 10+ s, but a follow-up
  `listen(timeout=30)` starts its clock immediately — the budget silently includes your own speech.
  There's no "speech finished playing" signal at all.
- **VAD `stop_secs=1.0` + batch Whisper means every `listen` resolves ≥1 s + transcription time
  after the bot actually stopped** — fine for turn-taking, but it's a fixed bias in any latency
  measurement you'd want for testing.
- **All telemetry is discarded** (timestamps dropped at `agent.py:155-156`) — covered below since
  it's the core of the improvement story.
- Minor: `AudioData.timestamp = performance.now()*1000` (`shim.js:107`) jitters with WS arrival
  rather than being sample-count-derived; harmless in Chrome today but worth a comment-level caveat.

# Improving the pipeline for human impersonation

The current limits: `listen` is pull, `speak` is fire-and-forget, and interruption is impossible
(bugs 1+2 above make it doubly so). One more constraint matters before designing the fix:
**Claude's own round-trip is seconds**, so *reactive* real-time barge-in ("I hear the bot saying X,
interrupt now!") can never live in the LLM loop. Every serious voice-testing product moves the
real-time behavior down into the pipeline and lets the LLM direct it declaratively. That suggests:

**a. Make the child full-duplex (fixes bugs 2+3).** Correlation-ID'd requests, each dispatched as a
task. Then `speak` during `listen` "just works".

**b. Turn `listen` into an event stream, not a transcript string.** The pipeline already observes
everything you need: `VADUserStarted/StoppedSpeakingFrame` carry wall-clock `time.time()`, TTS
start/stop are observable, and the aggregator reports `interrupted`. Keep a monotonic, timestamped
event log in the child (`bot_speech_started`, `bot_speech_stopped`, `transcript`,
`our_speech_started/finished/interrupted`, `client_disconnected`) and have `listen` return
events-since-cursor as structured JSON (blocking until at least one new event or timeout). This one
change gives Claude: streaming-ish awareness ("bot started speaking 0.3 s ago" before the
transcript exists), the disconnect signal done properly (bug 5), and response-latency numbers for
free.

**c. Add declarative interruption primitives executed in the child at audio-rate:**

- `speak(text, interrupt=True)` — flush bot-side listening state and start TTS immediately even
  mid-bot-utterance (requires `enable_interruptions=False` from bug 1).
- `speak(text, when="bot_speaking", after_secs=1.5)` — the canonical barge-in test: child arms a
  one-shot trigger on the next `bot_speech_started`, waits 1.5 s, speaks. Claude scripts it; the
  pipeline times it.
- `speak(..., wait=True)` or a `speech_done` event — so the agent knows when its own audio finished
  playing.

**d. Persist a metrics artifact per session** (extends the existing `record_dir`): per-turn JSON
with utterance boundaries, time-from-our-speech-end-to-bot-first-audio (the app's response
latency), talk-over windows, dead-air gaps. The WAVs already exist; the event log makes them
assertable.

## References

[1] `pipecat/turns/user_turn_strategies.py:27-41,76-80` and
`pipecat/turns/user_start/base_user_turn_start_strategy.py:55` (installed pipecat 1.2.1) — defaults
`VADUserTurnStartStrategy` with `enable_interruptions=True`.

[2] `pipecat/processors/aggregators/llm_response_universal.py:895-896` —
`broadcast_interruption()` on user-turn start.

---

# Appendix — B1 to B5 in plain English, with code

*Code snippets show the pre-fix state as reviewed on 2026-06-11. B1, B4, B5 are addressed in
Stage 0; B2, B3 in Stage 1 (see [upgrade-roadmap.md](upgrade-roadmap.md)).*

## B1 — Our own voice gets cut off whenever the bot starts talking

In `agent.py` the pipeline is configured like this:

```python
user_params=LLMUserAggregatorParams(
    user_turn_strategies=UserTurnStrategies(
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]
    ),
    ...
)
```

Notice only `stop` is set. `UserTurnStrategies` fills in the missing `start` with defaults
(pipecat source):

```python
def __post_init__(self):
    if not self.start:
        self.start = default_user_turn_start_strategies()
        # → [VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()]
```

And those default start strategies are built with `enable_interruptions: bool = True`. Inside
pipecat, when a "user turn starts" — and remember, in voicebox the "user" is **the bot in the
browser**, because that's whose audio comes into our pipeline — this runs:

```python
if params.enable_interruptions:
    await self.broadcast_interruption()
```

`broadcast_interruption()` is pipecat's "the human started talking, shut the assistant up"
mechanism: it cancels the TTS that's currently generating and throws away any audio still queued
for output. In a normal voice bot that's exactly what you want. In voicebox it's backwards: *our
Kokoro speech* is the "assistant" side, so **the moment the browser bot makes any sound, voicebox
stops talking mid-sentence**. The bot answering quickly, saying "mm-hmm", or even playing a chime
is enough. And since `speak()` already returned `true`, Claude believes the full sentence was
delivered. The fix is one line of intent: construct the start strategies with
`enable_interruptions=False`.

## B2 — The child can only do one thing at a time, so "speak while listening" is impossible

The child process's entire brain is this loop in `bot.py`:

```python
while True:
    request = await read_request()          # take ONE command off the queue
    cmd = request.get("cmd")
    if cmd == "listen":
        text = await asyncio.wait_for(agent.listen(), timeout=timeout)  # blocks here, up to 30s
        await send_response({"text": text})
    elif cmd == "speak":
        await agent.speak(request["text"])
    ...
```

It reads a command, **fully finishes it**, then reads the next. So if Claude calls
`listen(timeout=30)` and one second later calls `speak("wait, let me stop you there")`, the speak
command just sits in the queue for up to 29 more seconds — the loop is stuck inside
`asyncio.wait_for(agent.listen(), ...)`. That's why interruption is mechanically impossible today
regardless of B1: you can never get audio out while you're waiting for audio in. The fix is to
make the loop only *dispatch*: `asyncio.create_task(handle(request))`, so a listen and a speak run
concurrently.

## B3 — Requests and responses aren't matched up, so they can pair off wrong

The parent talks to the child through two shared queues with no IDs. Sending a command
(`agent_ipc.py`):

```python
request = {"cmd": cmd, **kwargs}
await loop.run_in_executor(None, _cmd_queue.put, request)   # put request
response = await self._wait_for_command_response()          # take NEXT response, whatever it is
```

"Whatever response shows up next belongs to me" is only true if exactly one command is ever in
flight. Two ways that breaks:

1. **Cancellation.** Claude's MCP call to `listen` gets cancelled (client timeout, user hits
   Escape). The parent coroutine dies, but the *child* doesn't know — it finishes listening and
   puts `{"text": "..."}` on the response queue anyway. That response now sits there like a
   landmine. The next command — say a `speak` — puts its request, then reads the queue and gets
   the **old listen's transcript** as its answer. Every subsequent command is now answered by the
   previous one's response, permanently off by one.
2. **Concurrency.** The MCP server is `stateless_http` and will happily run two tool calls at
   once. Both end up polling the same response queue, and whoever's thread wins the race gets
   whichever response arrives first — possibly the other call's.

The fix is the classic one: tag every request with an ID (`{"id": uuid4(), "cmd": ...}`), have the
child echo it back, and have one router in the parent that matches responses to waiting futures by
ID. A cancelled call's late response then just resolves a future nobody is awaiting — harmless.

## B4 — Errors come back disguised as things the bot said, and then the child dies

The error handler in `bot.py`:

```python
except Exception as e:
    logger.warning(f"Error processing command '{cmd}': {e}")
    await send_response({"text": str(e)})   # ← keyed "text", like a transcript!
    break                                    # ← and the loop exits
```

Two problems in three lines:

- The response key is `"text"` — the same key a successful `listen` uses. So if Whisper throws
  during a listen, Claude receives something like
  `"[Errno 2] No such file or directory: ..."` as the return value of `listen()`, **as if the bot
  had spoken those words.** The parent never notices either, because its error check looks for a
  key that's never sent: `if "error" in response:`.
- Then `break` exits the command loop, which ends the child process — over one possibly-transient
  exception. The parent has no idea; the next tool call either hangs until the dead process is
  noticed or raises a confusing "Voice agent process has stopped".

Fix: respond `{"error": str(e)}` and keep looping, so one bad command doesn't end the session and
errors surface as MCP tool errors rather than transcripts.

## B5 — A disconnect is reported as a sentence the bot spoke

When the browser's audio WebSocket drops, `agent.py` does this:

```python
@self._transport.event_handler("on_client_disconnected")
async def on_disconnected(transport, client):
    ...
    await self._user_speech_queue.put("I just disconnected, but I might come back.")
```

`_user_speech_queue` is the queue that `listen()` reads transcripts from. So this English sentence
comes out of `listen()` looking *exactly* like something the bot said over the call. The intent is
good — wake up a blocked `listen()` and tell Claude something happened — but the channel is wrong:
a testing agent would faithfully log "the app said: I just disconnected, but I might come back",
which the app never said. It belongs in a structured side-channel, e.g.
`{"event": "client_disconnected"}` — which is exactly what the Stage 2 event-stream design gives
us; until then a distinguishable sentinel + marker string is the interim fix.

## The common thread

**B1 is a wrong default inherited from pipecat's "polite assistant" worldview, and B2–B5 are all
symptoms of the IPC protocol being too thin** — one in-flight command, no IDs, no event channel,
strings doing double duty. That's why the roadmap fixes B1 + the cheap ones in Stage 0, then
rebuilds the protocol (Stage 1) before adding any features.
