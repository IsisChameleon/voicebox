# S1 real-microphone evidence — passed

- This artefact evidences Scenario S1 from
  [`docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md`](../../specs/2026-08-09-demo-voice-app-for-dogfooding.md):
  fresh clone, `uv sync --extra eval`, start `bot.py`, open the browser at `:7860`, click
  Connect, and talk to Nova through a real microphone.
- Result: **passed**. Isabelle completed a real-microphone conversation with Nova on
  2026-08-10 after lowering the physical speaker volume.
- Passing session: `af03f39b-3344-4ea1-804f-2022bbd4f4c5`.
- Failed high-speaker-volume attempt: `121aded8-d344-479a-a9f1-ea043b3caa26`.

## Browser and brain

- The supplied console log confirms the prebuilt User Interface loaded and Firefox connected
  from Windows:

```text
INFO:     127.0.0.1:49884 - "GET /client/ HTTP/1.1" 200 OK
2026-08-10 18:06:40.512 | DEBUG    | pipecat.transports.smallwebrtc.transport:on_connected:254 - Peer connection established.
2026-08-10 18:06:40.512 | INFO     | __main__:on_client_connected:173 - Client connected — kicking off Nova's greeting
2026-08-10 18:06:40.521 | DEBUG    | pipecat.processors.frameworks.rtvi.processor:_handle_client_ready:375 - Client Details: library='@pipecat-ai/client-react' library_version='1.6.0' platform='Windows' platform_version='NT 10.0' platform_details={'browser': 'Firefox', 'browser_version': '153.0', 'platform_type': 'desktop', 'engine': 'Gecko'}
```

- The console pipeline link identifies the Anthropic brain:

```text
2026-08-10 18:06:40.283 | DEBUG    | pipecat.processors.frame_processor:link:544 - Linking LLMUserAggregator#0 -> AnthropicLLMService#1
```

- The varied `brain_reply` records do not match the five canned lines in the scripted brain.

## Greeting and question-and-answer turns

- Nova greeted the player and asked a question. The observer recorded complete speech spans:

```json
{"t": 1786349259.4983494, "event": "brain_reply", "text": "Welcome to space exploration trivia! Here's your first question: which planet in our solar system has the most moons?"}
{"t": 1786349259.7026174, "event": "bot_speech_started"}
{"t": 1786349260.583877, "event": "bot_speech_stopped"}
{"t": 1786349260.5839846, "event": "bot_speech_started"}
{"t": 1786349261.5431519, "event": "bot_speech_stopped"}
{"t": 1786349262.9867494, "event": "bot_speech_started"}
{"t": 1786349269.544322, "event": "bot_speech_stopped"}
```

- Whisper heard a plausible human answer. Nova confirmed it and asked the next question:

```json
{"t": 1786349277.5242696, "event": "heard_user", "text": " Saturn "}
{"t": 1786349278.9169877, "event": "brain_reply", "text": "Correct! Saturn has 146 known moons, though Jupiter actually ties it now with recent discoveries. Here's the next one: what was the name of the first human-made object to leave our solar system?"}
{"t": 1786349278.95539, "event": "bot_speech_started"}
{"t": 1786349279.756083, "event": "bot_speech_stopped"}
{"t": 1786349282.3843138, "event": "bot_speech_started"}
{"t": 1786349290.654116, "event": "bot_speech_stopped"}
{"t": 1786349290.6546164, "event": "bot_speech_started"}
{"t": 1786349295.213711, "event": "bot_speech_stopped"}
```

- Whisper heard a plausible but incorrect answer. Nova corrected it and continued:

```json
{"t": 1786349310.3640497, "event": "heard_user", "text": " This is Apollo Probe. "}
{"t": 1786349314.5578556, "event": "brain_reply", "text": "Not quite — you're thinking of a real mission, but the answer is Voyager 1, which crossed the heliopause in 2012. Apollo missions stayed within Earth orbit and the Moon. What year did humans first land on the Moon?"}
```

- Whisper heard another correct human answer. Nova confirmed it and asked another question:

```json
{"t": 1786349336.0864286, "event": "heard_user", "text": " 1969 "}
{"t": 1786349338.2275836, "event": "brain_reply", "text": "Exactly right! Apollo 11 landed on July 20th, 1969, and Buzz Aldrin and Neil Armstrong became the first humans to walk on the lunar surface. Next question: what is the name of the rover currently exploring Mars for NASA?"}
{"t": 1786349338.268346, "event": "bot_speech_started"}
{"t": 1786349339.3040512, "event": "bot_speech_stopped"}
{"t": 1786349339.3041515, "event": "bot_speech_started"}
{"t": 1786349340.5838385, "event": "bot_speech_stopped"}
{"t": 1786349340.583945, "event": "bot_speech_started"}
{"t": 1786349342.3846123, "event": "bot_speech_stopped"}
{"t": 1786349342.384718, "event": "bot_speech_started"}
{"t": 1786349343.224056, "event": "bot_speech_stopped"}
{"t": 1786349343.2241306, "event": "bot_speech_started"}
{"t": 1786349345.2640224, "event": "bot_speech_stopped"}
{"t": 1786349346.4347157, "event": "bot_speech_started"}
{"t": 1786349357.9852905, "event": "bot_speech_stopped"}
```

## Anomalies

- With normal physical speaker volume, Nova's output reached the microphone. Whisper classified
  assistant fragments such as `Sorry, you're experiencing it`, `Welcome to the`, and
  `Here's your first question` as user turns. Six interruptions occurred during bot speech in
  session `121aded8-d344-479a-a9f1-ea043b3caa26`.
- Lowering the speaker volume allowed the passing exchange, confirming acoustic feedback as the
  cause of the first attempt. The passing session still began with two garbled transcripts:
  `We've got a spades` and `Yes?`.
- The passing session contains one `interrupted` record during bot speech at
  `1786349213.789027`; subsequent human turns have `during_bot_speech: false`.
- Both console runs include a failed Web Real-Time Communication (WebRTC) candidate patch followed
  by a successful connection. The failure did not prevent either conversation.
- The passing session has no `session_ended` record because the browser remained connected when
  this evidence was captured. Clean disconnect was not assessed.

## Not covered

- Headphones were not tested.
- OpenAI was not tested; the passing conversation used Anthropic.
- Clean disconnect after the passing conversation was not observed.
