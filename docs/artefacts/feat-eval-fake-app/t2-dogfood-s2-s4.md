# Phase 2 evidence — live dogfood of the fake voice app (Nova, scripted brain)

*2026-08-09. Commits: `751c4b9` (the Whisper-CPU fix this dogfood surfaced); Phase-1 app under
test at `38184f4`.*

Success criteria evidenced (spec: `docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md`, § "Scenarios → acceptance"):

- **S2** Dogfood round-trip: `app_bot_transcript` of Nova's reply appears in `listen()` events; `__voiceShim` counters (`inboundChunks`, `outboundChunks`) non-zero.
- **S3** Turn-taking: `metrics.json` turn latencies present and sane; WAVs contain both voices.
- **S4** Barge-in: `tester_barge_in_fired` then `app_bot_speech_stopped` shortly after.

Environment: no API keys; app run with `VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted`. voicebox MCP server pre-running on :9090.

## Preflight

```
$ ss -tlnp | grep -E ':(7860|9090|9091|9222)\b'
LISTEN 0      2048        127.0.0.1:9090       0.0.0.0:*    users:(("voicebox",pid=1165501,fd=6))
```

7860 free before app start; 9090 = voicebox MCP server.

## App start

```
$ VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted uv run python tests/eval/fake_app/bot.py > temp/fake_app/app.log 2>&1 &
$ tail temp/fake_app/app.log
2026-08-09 16:26:54.463 | INFO     | pipecat:<module>:14 - ᓚᘏᗢ Pipecat 1.3.0 (Python 3.12.3 ...) ᓚᘏᗢ
INFO:     Uvicorn running on http://localhost:7860 (Press CTRL+C to quit)
```

## Session A — S2 (round-trip) + S3 (turn-taking)

`start_browser_session(url="http://localhost:7860", record_dir="temp/dogfood-fake-app/session-a")` →

```json
{"cdp_endpoint": "http://localhost:9222", "audio_ws_url": "ws://localhost:9091", "attach_hint": "playwright-cli attach --cdp http://localhost:9222"}
```

CDP click of the prebuilt UI's Connect button (existing tab, `browser.contexts[0].pages[0]`, page url `http://localhost:7860/client/`): button found by role name "Connect", clicked. Shim diag right after click showed `wsReady: False` + 15 "WS error event" entries (pre-server-ready retries), then `listen()` confirmed the link:

```json
{"type": "client_connected", "t": 1786256867.5853236}
```

Nova's scripted line 1 arrived (S2 transcript evidence, VERBATIM):

```json
{"type": "app_bot_speech_started", "t": 1786256903.580347}
{"type": "app_bot_speech_stopped", "t": 1786256910.7307932}
{"type": "app_bot_transcript", "t": 1786256924.5515296, "text": " Welcome to Nova's space trivia.  First question, which planet in our solar system has the most moons? ", "turn_started_at": "2026-08-09T06:28:23.580+00:00", "transcription_empty": false}
```

Note: greeting playout began ~36 s after client_connected (first-use model load on the app side).
transcription_lag_secs so far: 0.0 / 0.051 / 0.0.

### Session A, attempt 1 — FAILED (app-side bug found, root-caused)

Tester spoke `"I think it's Saturn."` (played 1786256946.40–1786256947.51, `interrupted: false`), but Nova never advanced to line 2. `temp/fake_app/app.log` (VERBATIM):

```
2026-08-09 16:29:06.832 | DEBUG    | pipecat.processors.frame_processor:broadcast_interruption:720 - LLMUserAggregator#0: broadcasting interruption
2026-08-09 16:29:08.534 | ERROR    | pipecat.processors.frame_processor:push_error_frame:699 - WhisperSTTService#0 exception (/home/isischameleon/src/voicebox/.venv/lib/python3.12/site-packages/faster_whisper/transcribe.py:1400): Error processing frame: Library libcublas.so.12 is not found or cannot be loaded
2026-08-09 16:29:08.539 | WARNING  | pipecat.pipeline.worker:_source_push_frame:1116 - PipelineWorker#0: Something went wrong: ErrorFrame#0(error: Error processing frame: Library libcublas.so.12 is not found or cannot be loaded, fatal: False)
```

Plus a continuous stream of `pipecat.transports.smallwebrtc.transport:read_audio_frame:387 - Timeout: No audio frame received` starting 16:29:57.

Ground truth up to the failure (VERBATIM, tail):

```json
{"t": 1786256899.0046697, "event": "session_started", "session_id": "6ca77cff-6cc1-4d52-9043-6d8ab87428be"}
{"t": 1786256899.042256, "event": "brain_reply", "text": "Welcome to Nova's space trivia! First question: which planet in our solar system has the most moons?"}
{"t": 1786256902.8976214, "event": "bot_speech_started"}
{"t": 1786256909.6505897, "event": "bot_speech_stopped"}
{"t": 1786256946.832609, "event": "interrupted"}
```

(The `interrupted` record here is pipecat's per-user-turn-start InterruptionFrame broadcast, matching our speech start at 1786256946.40 — expected per the observer docstring, not a barge-in.)

**Root cause (verified in pipecat source):** `bot.py:141` builds `WhisperSTTService(settings=...)` without `device`; the init default is `device: str = "auto"` (`pipecat/services/whisper/stt.py:221`), and faster-whisper's auto picked CUDA on a machine without CUDA libs (WSL2, no libcublas). **Fix applied to the working tree (not committed):** pass `device="cpu"`.

### Session A, attempt 2 — after `device="cpu"` fix (app restarted, fresh session)

`start_browser_session` again (same args); CDP-clicked Connect. `listen()`:

```json
{"type": "session_started", "t": 1786257202.7207296, "vad_stop_secs": 1.0}
{"type": "client_connected", "t": 1786257206.0048308}
```

**Quirk:** with the Whisper model now cached+CPU the app pipeline came up instantly and Nova's greeting (line 1) played at ~16:33:14-24 (app log: `Generating TTS [Welcome to Nova's space trivia! ...]` at 16:33:13.944), BEFORE the shim's audio WS finished connecting — so voicebox never saw line-1 events in this session. The scripted brain advances one line per completed tester turn regardless of content, so the exchange proceeded from line 2.

Tester turn 1: `speak("I think it's Saturn.", wait_for_turn=True, wait_for_playout=True)` →

```json
{"queued": true, "played": true, "started_at": 1786257369.1579983, "finished_at": 1786257370.240644, "interrupted": false, "waited_for_turn_secs": 0.0}
```

Nova line 2 (S2 transcript evidence, VERBATIM):

```json
{"type": "app_bot_speech_started", "t": 1786257383.498985}
{"type": "app_bot_speech_stopped", "t": 1786257397.4772308}
{"type": "app_bot_transcript", "t": 1786257408.3594286, "text": " Nice try. The current champion is Saturn with well over 100 confirmed moons.  Next question, or ask me to explain in detail, what was the first spacecraft to land softly on the moon? ", "turn_started_at": "2026-08-09T06:36:23.498+00:00", "transcription_empty": false}
```

Response latency turn 1: tester_speech_stopped 1786257370.24 → app_bot_speech_started 1786257383.50 = **13.26 s** (CPU Whisper decode + Kokoro synth on a shared machine).

Shim counters mid-session (CDP `window.__voiceShim`, VERBATIM — S2 counter evidence, first read before tester spoke):

```
{'installed': True, 'micHookInstalled': True, 'pcHookInstalled': True, 'wsReady': True, 'inboundChunks': 0, 'outboundChunks': 18420, ..., 'audioTrackCount': 1, 'micTrackCount': 1, 'outboundSampleRate': 16000, 'outboundNumChannels': 1, 'outboundFormat': 'webaudio-f32', 'perTrackBytes': {'1c67e3ca-d6db-4fef-93cf-61624be6be8d': 4715520}}
```

(inboundChunks was 0 at that point because the tester had not yet spoken; re-read after the exchange below.)

### Session A — remaining exchange (turns 2 and 3)

Tester turn 2: `speak("Was it Luna 9? Please explain that in detail.", wait_for_turn=True, wait_for_playout=True)` → played 1786257437.14–1786257439.95. Nova line 3 (the 8-sentence monologue), VERBATIM:

```json
{"type": "app_bot_speech_started", "t": 1786257463.3767824}
{"type": "app_bot_speech_stopped", "t": 1786257512.3001926}
{"type": "app_bot_transcript", "t": 1786257536.1551685, "text": " Great question. Let me explain in detail.  The first spacecraft to soft land on the moon was the Soviet probe Luna 9,  which touched down in the ocean of storms on the 3rd of February,  1996. Before Luna 9, every attempt had either crashed or missed the moon entirely,  and many scientists genuinely feared a lander would sink into a deep layer of dust.  Luna 9 settled that debate by transmitting the first photographs ever taken from the lunar surface,  proving the ground could bear a spacecraft's weight.  Four months later, the American Surveyor 1 repeated the feat with a far more capable camera,  paving the way for Apollo.  It is remarkable that barely three years after those robotic landings,  human beings were walking on the very same surface. ", "turn_started_at": "2026-08-09T06:37:43.376+00:00", "transcription_empty": false}
```

(Mis-transcription note: voicebox's Whisper heard "3rd of February, 1996" — the script says 1966. Utterance-level WER artifact, expected class of error.)

Tester turn 3: `speak("Olympus Mons!", ...)` → played 1786257547.96–1786257549.04. Nova line 4, VERBATIM:

```json
{"type": "app_bot_speech_started", "t": 1786257555.9096143}
{"type": "app_bot_speech_stopped", "t": 1786257563.7794898}
{"type": "app_bot_transcript", "t": 1786257575.1754518, "text": " You're on a roll. Last one for now. What is the name of the largest volcano in the solar system found on Mars? ", "turn_started_at": "2026-08-09T06:39:15.909+00:00", "transcription_empty": false}
```

### S2 counter evidence — final shim diag (CDP `window.__voiceShim`, VERBATIM)

```
{'installed': True, 'micHookInstalled': True, 'pcHookInstalled': True, 'wsReady': True, 'inboundChunks': 127, 'outboundChunks': 47064, 'audioWsUrl': 'ws://localhost:9091', 'pcCount': 1, 'audioTrackCount': 1, 'micTrackCount': 1, 'outboundSampleRate': 16000, 'outboundNumChannels': 1, 'outboundFormat': 'webaudio-f32', 'perTrackBytes': {'1c67e3ca-d6db-4fef-93cf-61624be6be8d': 12048384}, 'errors': [15x 'WS error event: [object Event]' — pre-ready retries], 'hasMediaDevices': True, 'hasWebCodecs': True}
```

**inboundChunks = 127 > 0 (tester Kokoro audio into the page mic) and outboundChunks = 47064 > 0 (bot audio tapped out to Whisper) — S2 accepted.**

### S3 evidence — stop() artifacts

`stop()` returned artifact paths under `/home/isischameleon/src/voicebox/temp/dogfood-fake-app/session-a/`.

metrics.json (VERBATIM, key parts — full file on disk):

```json
"turns": [
  {"speaker": "tester", "t": 1786257369.1579983, "text": "I think it's Saturn."},
  {"speaker": "app_bot", "t": 1786257383.498985, "text": " Nice try. The current champion is Saturn with well over 100 confirmed moons. ...", "response_latency_secs": 13.258},
  {"speaker": "tester", "t": 1786257437.1441715, "text": "Was it Luna 9? Please explain that in detail."},
  {"speaker": "app_bot", "t": 1786257463.3767824, "text": " Great question. Let me explain in detail. ...", "response_latency_secs": 23.431},
  {"speaker": "tester", "t": 1786257547.9564462, "text": "Olympus Mons!"},
  {"speaker": "app_bot", "t": 1786257555.9096143, "text": " You're on a roll. Last one for now. ...", "response_latency_secs": 6.872}
],
"app_response_latencies_secs": [13.258, 23.431, 6.872],
"talk_over_windows": [],
"talk_time": {"tester_secs": 4.965, "app_bot_secs": 70.772, "ratio_tester_over_app_bot": 0.07},
"utterances": {"tester": 3, "app_bot": 3},
"summary": {"mean_app_response_latency_secs": 14.52, "max_app_response_latency_secs": 23.431, "total_talk_over_secs": 0, "total_dead_air_secs": 43.561, "total_tester_think_time_secs": 75.323, "total_outage_secs": 0}
```

WAVs:

```
kokoro_voice.wav 37039484 bytes, 1 ch, 48000 Hz, 385.83 s
ember_voice.wav 37046084 bytes, 1 ch, 48000 Hz, 385.9 s
merged.wav 74078924 bytes, 2 ch, 48000 Hz, 385.83 s
```

**Turn latencies present (3 of 3 app turns, 6.9–23.4 s — sane for double CPU Whisper+Kokoro on one box), both voices in per-party WAVs — S3 accepted.**

## Session B — S4 (barge-in), attempt 1 — FAILED (mid-session client renegotiation killed the mic; root-caused)

`start_browser_session(url="http://localhost:7860", record_dir="temp/dogfood-fake-app/session-b")`; CDP-clicked Connect. Greeting caught live this time:

```json
{"type": "session_started", "t": 1786257647.0619836, "vad_stop_secs": 1.0}
{"type": "client_connected", "t": 1786257648.3799105}
{"type": "app_bot_speech_started", "t": 1786257648.7835908}
{"type": "app_bot_speech_stopped", "t": 1786257654.4256608}
{"type": "app_bot_transcript", "t": 1786257666.372719, "text": " Face trivia. First question, which planet in our solar system has the most moons? ", ...}
```

(Transcript is the TAIL of line 1 — the shim WS connected mid-greeting, so the first words were not tapped. transcription_lag_secs hit 9.096 on this batch.)

Q1 answered; Nova line 2 played 1786257696.62–1786257710.56. Barge-in armed and monologue triggered:

```json
{"type": "tester_barge_in_armed", "t": 1786257720.5736067, "when": "app_bot_speech_started", "timer_secs": 2.0, "text": "Wait, wait — let me stop you right there!"}
{"type": "app_bot_speech_started", "t": 1786257773.668451}
{"type": "tester_barge_in_fired", "t": 1786257775.669747, "when": "app_bot_speech_started", "triggered_by_t": 1786257773.668451}
{"type": "tester_speech_started", "t": 1786257777.0044787}
{"type": "tester_speech_stopped", "t": 1786257779.0863338}
{"type": "app_bot_speech_stopped", "t": 1786257822.5286725}
```

The voicebox side worked perfectly (fired 2.001 s after speech start, audio played) — but `app_bot_speech_stopped` came 46.9 s after fired = the FULL monologue length; Nova was NOT interrupted, and her full 8-sentence transcript arrived intact.

**Root cause (from `temp/fake_app/app.log` + `temp/dogfood-fake-app/session-b/shim.log` + shim diag):**

1. Our previous utterance "Please explain that in detail." played 16:42:06.9–16:42:08.5, but Nova's VAD heard only 0.15 s of it — the outgoing mic RTP died mid-utterance:
```
2026-08-09 16:42:06.908 | DEBUG | ...vad_processor:on_speech_started:71 - VADProcessor#1: User started speaking
2026-08-09 16:42:07.061 | DEBUG | ...vad_processor:on_speech_stopped:79 - VADProcessor#1: User stopped speaking
```
(no heard_user record in ground truth for this turn; the scripted brain advanced anyway — it advances per completed turn regardless.)

2. 15 s later the prebuilt client itself renegotiated the connection:
```
2026-08-09 16:42:22.911 | INFO  | ...request_handler:handle_web_request:200 - Reusing existing connection for pc_id: SmallWebRTCConnection#1-6db80670d88a422dbaabcb7394cc07c5
2026-08-09 16:42:22.912 | DEBUG | ...connection:renegotiate:444 - Renegotiating ...
2026-08-09 16:42:22.912 | DEBUG | ...connection:renegotiate:448 - Closing old peer connection
```

3. Shim diag after the failure (VERBATIM): `pcCount: 2, audioTrackCount: 2, micTrackCount: 0` (was `pcCount: 1, micTrackCount: 1` right after Connect); shim.log shows only ONE `intercepting getUserMedia(audio)` (1786257634.489) and a second remote-track tee at 1786257743.259 — i.e. the client's new RTCPeerConnection never re-acquired the synthetic mic; the original mic track was stopped and gone. Nova was deaf from 16:42:07 onward; continuous `read_audio_frame ... Timeout` warnings for the rest of the session confirm zero incoming mic RTP.

Not a voicebox barge-in defect: the trigger armed, fired on time, and the audio played into the page. The failure is the app/prebuilt-client mic path dying on mid-session renegotiation. Session A (same stack, no renegotiation) heard all 3 tester turns — the renegotiation looks flaky, so per procedure ONE fresh retry.

## Session B — S4 (barge-in), attempt 2 — PASS

Fresh session (`record_dir="temp/dogfood-fake-app/session-b"`, attempt-1 artifacts archived to `session-b-attempt1/`). Greeting was MISSED entirely this time: the app played line 1 at 16:47:49–56 while the shim's audio WS was still connecting (connected 16:47:55.4 = t 1786258075.38) — see "Notable observations". Script advanced via turns as designed.

Q1 answered ("I think it's Saturn.", played 1786258297.02–1786258298.12); Nova line 2 played 1786258312.32–1786258326.25 and its transcript arrived verbatim. Then, S4 sequence (VERBATIM, in arrival order):

```json
{"type": "tester_barge_in_armed", "t": 1786258332.7640734, "when": "app_bot_speech_started", "timer_secs": 2.0, "text": "Wait, wait — let me stop you right there!"}
{"type": "tester_transcript", "t": 1786258341.9306521, "text": "Please explain that in detail."}
{"type": "tester_speech_started", "t": 1786258343.0576684}
{"type": "tester_speech_stopped", "t": 1786258344.660767}
{"type": "app_bot_speech_started", "t": 1786258381.079924}
{"type": "tester_barge_in_fired", "t": 1786258383.0814505, "when": "app_bot_speech_started", "triggered_by_t": 1786258381.079924}
{"type": "tester_transcript", "t": 1786258383.0816991, "text": "Wait, wait — let me stop you right there!"}
{"type": "tester_speech_started", "t": 1786258384.3969893}
{"type": "app_bot_speech_stopped", "t": 1786258385.6785789}
{"type": "tester_speech_stopped", "t": 1786258386.4788418}
```

**Acceptance line: fired at 1786258383.081 (2.0015 s after `app_bot_speech_started` 1786258381.080), `app_bot_speech_stopped` at 1786258385.679 — 2.598 s after fired — S4 accepted.** (True speech end ~1 s earlier still, per vad_stop_secs bias.)

The monologue transcript WAS cut short (VERBATIM — compare session A's full 8 sentences):

```json
{"type": "app_bot_transcript", "t": 1786258402.4023268, "text": " Great question. Let me explain in detail. The first spacecraft... ", "turn_started_at": "2026-08-09T06:53:01.079+00:00", "transcription_empty": false}
```

Nova then treated the barge-in utterance as a turn and advanced to line 4:

```json
{"type": "app_bot_speech_started", "t": 1786258397.5711615}
{"type": "app_bot_transcript", "t": 1786258417.4536228, "text": " You're on a roll. Last one for now. What is the name of the largest volcano in the solar system found on Mars? ", ...}
```

transcription_lag_secs peaked at 14.1 in this session (both Whisper stacks decoding simultaneously right after the barge-in).

`stop()` artifacts written under `/home/isischameleon/src/voicebox/temp/dogfood-fake-app/session-b/`.

## Ground truth (`temp/fake_app/ground_truth.jsonl`) — Phase-2 proof the observer works

```
$ wc -l temp/fake_app/ground_truth.jsonl
53 temp/fake_app/ground_truth.jsonl
```

First 5 lines (VERBATIM — session A attempt 2 start):

```json
{"t": 1786257193.8114922, "event": "session_started", "session_id": "6d10d779-0cd5-4fad-a6ba-ed7527f6557c"}
{"t": 1786257193.905189, "event": "brain_reply", "text": "Welcome to Nova's space trivia! First question: which planet in our solar system has the most moons?"}
{"t": 1786257197.7289784, "event": "bot_speech_started"}
{"t": 1786257204.4837904, "event": "bot_speech_stopped"}
{"t": 1786257369.5671604, "event": "interrupted"}
```

Last 12 lines (VERBATIM — session B attempt 2, the barge-in):

```json
{"t": 1786258325.273405, "event": "bot_speech_stopped"}
{"t": 1786258343.072379, "event": "interrupted"}
{"t": 1786258349.2536888, "event": "interrupted"}
{"t": 1786258351.6182544, "event": "heard_user", "text": " Please explain. "}
{"t": 1786258356.6196327, "event": "brain_reply", "text": "Great question, let me explain in detail. The first spacecraft to soft-land on the Moon was the Soviet probe Luna 9, ... walking on the very same surface."}
{"t": 1786258380.3475027, "event": "bot_speech_started"}
{"t": 1786258384.4462569, "event": "interrupted"}
{"t": 1786258384.452566, "event": "bot_speech_stopped"}
{"t": 1786258389.443629, "event": "heard_user", "text": " Wait, wait, let me stop you right there. "}
{"t": 1786258389.4569247, "event": "brain_reply", "text": "You're on a roll! Last one for now: what is the name of the largest volcano in the solar system, found on Mars?"}
{"t": 1786258396.3216162, "event": "bot_speech_started"}
{"t": 1786258404.0234258, "event": "bot_speech_stopped"}
```

All designed record types present: `session_started`, `brain_reply`, `bot_speech_started/stopped`, `heard_user`, `interrupted`. The barge-in shows as a REAL interruption: `interrupted` at 1786258384.446 falls inside the bot speech span (started 1786258380.35), with `bot_speech_stopped` 6 ms later — Nova's TTS genuinely stopped. Her Whisper heard our barge-in as " Wait, wait, let me stop you right there. " (near-verbatim) and our earlier "Please explain that in detail." as " Please explain. " (truncated — the app's own STT quality, fine/interesting).

Cross-instrument alignment (voicebox observed vs fake app emitted, the eval seed):
- monologue start: voicebox 1786258381.080 vs ground truth 1786258380.348 → +0.73 s observation lag (WebRTC + tap + VAD onset).
- monologue stop: voicebox 1786258385.679 vs ground truth 1786258384.453 → +1.23 s ≈ the documented vad_stop_secs (1.0 s) bias.

## Teardown

```
$ pkill -f "tests/eval/fake_app/bot.py"; ss -tlnp | grep 7860 || echo "7860 closed"
7860 closed
LISTEN 0      2048        127.0.0.1:9090 ... ("voicebox",pid=1165501)   # MCP server untouched
```

## transcription_lag_secs observed (spec's resource-load question)

Mostly 0.0–0.5 across both sessions; spikes: 9.096 (session B a1 greeting), 9.808 / **14.1** / 3.305 (session B a2, right after the barge-in — both Whisper stacks decoding at once). The metric surfaced exactly when the two stacks contended, as designed.

## Not covered / caveats

- **S1 (real-mic onboarding)**: pending human — cannot be exercised by an agent.
- **Anthropic/OpenAI brain modes**: UNVERIFIED — no API key in this environment (by rule); only `scripted` exercised.
- **`uv sync --extra eval` resolution**: not re-verified here (Phase-1 scope); the app imported and ran from the existing venv.
- **Working-tree fix applied, NOT committed**: `tests/eval/fake_app/bot.py` now passes `device="cpu"` to `WhisperSTTService` — without it the app is broken on non-CUDA machines (`libcublas.so.12` error, session A attempt 1). Needs to be committed in Phase 2/3.
- **Flaky app/client behaviour (2 of 4 connects)**: (a) Nova's greeting can play before the shim's audio WS is up (model cache warm → instant greeting; WS takes ~15-20 s from page load because the pipecat child warms Kokoro/Whisper first) — greeting missed entirely (session B a2) or caught partially (" Face trivia. ...", session B a1). Any turn-1 assertion on line 1 would be flaky. (b) Session B a1: prebuilt client renegotiated mid-session and never re-acquired the synthetic mic (`pcCount` 1→2, `micTrackCount` 1→0) — Nova deaf from then on; barge-in couldn't land. Fresh session passed. Root-caused above; watch for it in future runs.
- Session A attempt 1 artifacts (`temp/dogfood-fake-app/session-a-attempt1/`, `temp/fake_app/app-attempt1.log`, `ground_truth-attempt1.jsonl`) and session B attempt 1 (`temp/dogfood-fake-app/session-b-attempt1/`) kept for reference.
