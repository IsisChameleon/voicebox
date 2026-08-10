# Delta: in-repo fake voice app for dogfooding and eval

Branch: `feat/eval-fake-app`  
Base: `origin/main` at `d07988ba0a46aaf0926faf7b328278dd8c426b89`  
Status: branch merged as PR #18 (`d203bb1`) without this fold; folding into
`docs/architecture/4plus1.md` is still outstanding — see the last row of Gaps.

This change adds Nova, an independent local WebRTC voice application used as voicebox's
dogfood target and as the future source of eval ground truth. It does not change the voicebox MCP
surface, event vocabulary, or metrics (`docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md:110-124`).

## Scenarios

### S1: A contributor starts Nova and connects with a browser

Given the eval extra is installed and a valid brain is selected, when the contributor starts the
script and clicks Connect in the prebuilt UI, then Nova opens a WebRTC session and greets them.

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | `uv sync --extra eval` installs the WebRTC and Anthropic extras without adding them to the core dependency set | development | `pyproject.toml:34-37` |
| 2 | Script startup validates the selected brain before handing control to pipecat's runner | logical | `tests/eval/fake_app/bot.py:189-195`, `tests/eval/fake_app/brain.py:79-117` |
| 3 | Pipecat's development runner serves the prebuilt browser UI on localhost port 7860 | physical | `tests/eval/fake_app/README.md:13-27`, `:37` |
| 4 | A SmallWebRTC connection invokes `bot()` and constructs its transport and pipeline worker | process | `tests/eval/fake_app/bot.py:134-169` |
| 5 | Client connection queues `LLMRunFrame`, causing the selected brain to produce Nova's greeting | process | `tests/eval/fake_app/bot.py:171-175`, `tests/eval/fake_app/brain.py:54-76` |
| 6 | Nova's response flows through Kokoro and the WebRTC transport to the browser | logical | `tests/eval/fake_app/bot.py:148-164` |

Acceptance evidence: scripted startup and HTTP serving were captured at
`docs/artefacts/feat-eval-fake-app/t1-fake-app-phase1.md:72-127`. The spec's real-microphone
variant remains unverified (`docs/artefacts/feat-eval-fake-app/t1-fake-app-phase1.md:170-176`).

### S2: voicebox conducts a deterministic trivia turn against Nova

Given Nova is running with the scripted brain and voicebox has opened its page, when the external
CDP client clicks Connect and the tester speaks, then Nova transcribes the tester, selects the next
canned response, speaks it, and voicebox observes an `app_bot_transcript`.

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | voicebox targets the independent application at `http://localhost:7860` and the CDP client clicks Connect | physical | `tests/eval/fake_app/README.md:59-63` |
| 2 | Browser microphone audio enters Nova through `SmallWebRTCTransport` | process | `tests/eval/fake_app/bot.py:139-142`, `:148-152` |
| 3 | Silero VAD segments the turn and CPU Whisper produces a `TranscriptionFrame` | logical | `tests/eval/fake_app/bot.py:150-158` |
| 4 | The user aggregator emits an `LLMContextFrame`; `ScriptedBrain` advances its cyclic script and emits one response triplet | logical | `tests/eval/fake_app/bot.py:159-160`, `tests/eval/fake_app/brain.py:54-76` |
| 5 | Kokoro synthesizes Nova with a voice distinct from the tester and WebRTC returns it to the browser | logical | `tests/eval/fake_app/bot.py:161-164` |
| 6 | voicebox's existing shim/audio/STT path observes the returned audio as an app-bot transcript | process | `tests/eval/fake_app/README.md:59-63`, `docs/architecture/4plus1.md` scenario S2 |

Acceptance evidence: a live scripted exchange, non-zero shim counters, and captured transcripts are
at `docs/artefacts/feat-eval-fake-app/t2-dogfood-s2-s4.md:32-55`, `:83-144`.

### S3: Nova writes independent ground truth for a completed or interrupted turn

Given a WebRTC session is active, when frames cross Nova's pipeline, then the observer appends each
watched frame once, records whether an interruption overlaps bot speech, flushes partial replies,
and marks clean session teardown.

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | Each session creates an observer with a repo-anchored, line-buffered JSONL handle and a session id | development | `tests/eval/fake_app/bot.py:53-57`, `:81-89`, `:166-169` |
| 2 | The observer accepts only downstream watched frames and deduplicates them by frame id | logical | `tests/eval/fake_app/bot.py:72-79`, `:104-111` |
| 3 | Speech boundaries, heard tester text, completed/partial brain replies, and interruption state become timestamped records | logical | `tests/eval/fake_app/bot.py:112-131` |
| 4 | Line buffering makes records observable during the session while keeping the channel disk-only and outside voicebox | process | `tests/eval/fake_app/bot.py:60-70`, `:84-92` |
| 5 | Worker cleanup writes `session_ended` and closes the handle; its absence identifies a crashed session | process | `tests/eval/fake_app/bot.py:94-102` |
| 6 | The append-only file lives under the repository's ignored `temp/fake_app/` run-artifact area | physical | `tests/eval/fake_app/bot.py:53-57`, `tests/eval/fake_app/README.md:49-57` |

Acceptance evidence: the original live session captured starts, speech, replies, heard-user events,
and interruptions (`docs/artefacts/feat-eval-fake-app/t2-dogfood-s2-s4.md:69-79`). Cleanup was
subsequently proven with a small pipeline probe (`docs/artefacts/feat-eval-fake-app/t-r2-simplify-fixes.md:45-55`),
but the final observer behavior has not been rerun end-to-end (`:70-73`).

### S4: A timed barge-in is observable from both sides

Given Nova is speaking its deliberately long third scripted response, when voicebox fires a timed
barge-in, then Nova receives an interruption, stops playout, and its ground truth distinguishes a
real barge-in from an ordinary new user turn.

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | The scripted brain's third response supplies a stable multi-sentence interruption target | logical | `tests/eval/fake_app/brain.py:32-50` |
| 2 | Nova's TTS begins bot playout and the observer sets its bot-speaking state | process | `tests/eval/fake_app/bot.py:112-117`, `:160-162` |
| 3 | voicebox injects tester audio through the page microphone while Nova is speaking | physical | `tests/eval/fake_app/README.md:59-63`, `docs/architecture/4plus1.md` scenario S3 |
| 4 | Pipecat broadcasts `InterruptionFrame`; the observer records `during_bot_speech` and flushes any partial streamed reply | logical | `tests/eval/fake_app/bot.py:60-68`, `:125-131` |
| 5 | The app-bot stop is independently visible in voicebox's event log and Nova's JSONL timeline | process | `tests/eval/fake_app/bot.py:112-117`, `tests/eval/fake_app/README.md:49-57` |

Acceptance evidence: voicebox's live timeline showed the barge-in firing and app-bot speech stopping
(`docs/artefacts/feat-eval-fake-app/t2-dogfood-s2-s4.md:6-12`). The `during_bot_speech` field was
corrected after that run and is not yet live-verified
(`docs/artefacts/feat-eval-fake-app/t-r1-review-fixes.md:67-70`).

## View impact

| View | Changed? | What |
|---|---|---|
| Logical | YES | Adds Nova's voice pipeline, provider-selected brain, deterministic scripted responder, and ground-truth observer. The voicebox product domain is unchanged. |
| Process | YES | Adds an independent pipecat/SmallWebRTC application process; its frame pipeline and line-buffered ground-truth writer run concurrently with voicebox's existing three-process topology. |
| Development | YES | Adds `tests/eval/fake_app/`, an `eval` optional dependency extra, documentation, and live evidence artifacts. |
| Physical | YES | Adds a local service on port 7860 and a persistent run-artifact file at `temp/fake_app/ground_truth.jsonl`; browser WebRTC connects voicebox's existing Chromium process to it. |

Process and physical views changed; these topology and lifecycle claims require explicit reviewer
attention.

## Invariants introduced or checked

| # | Invariant | Consequence of violation | Evidence | Protected by |
|---|---|---|---|---|
| D-I1 | Nova remains an independent plain WebRTC application: it exposes no ground-truth network API to voicebox | An eval could read its oracle and cease to measure the real browser/audio path | `tests/eval/fake_app/bot.py:60-70`, `tests/eval/fake_app/README.md:49-63` | S2, S3 |
| D-I2 | Nova's Whisper is pinned to CPU | `device="auto"` selected unusable CUDA on WSL2, so tester turns produced no response | `tests/eval/fake_app/bot.py:152-158`; failure evidence `docs/artefacts/feat-eval-fake-app/t2-dogfood-s2-s4.md:57-83` | S2 |
| D-I3 | Ground-truth records are written once per downstream frame and a clean session ends with `session_ended` | Duplicate/mixed records or an indistinguishable truncated log corrupt future alignment and scoring | `tests/eval/fake_app/bot.py:94-131` | S3, S4 |
| D-I4 | Scripted mode produces one deterministic response triplet per completed context and cycles the declared script | Nondeterminism or malformed frame boundaries makes repeatable dogfood/eval scenarios impossible | `tests/eval/fake_app/brain.py:32-76` | S1, S2, S4 |

## Tests derived from scenarios

| Scenario | Given / when / then | Test location | Status |
|---|---|---|---|
| S1 | Given scripted mode, when `create_brain()` runs and the app boots, then no key is required and the UI serves on 7860 | startup smoke captured in `docs/artefacts/feat-eval-fake-app/t1-fake-app-phase1.md:72-127` | LIVE PASS; real-mic acceptance MISSING |
| S2 | Given the five-line script, when context frames arrive, then response triplets contain successive lines and cycle predictably; invalid/missing provider configuration fails clearly | should be `tests/eval/test_fake_app_brain.py` | MISSING TEST |
| S2 | Given a live voicebox/Nova session, when the tester speaks, then both shim directions are non-zero and an app transcript returns | `docs/artefacts/feat-eval-fake-app/t2-dogfood-s2-s4.md:32-144` | LIVE PASS |
| S3 | Given representative watched frames (including duplicate ids), when the observer receives them and cleans up, then JSONL is deduplicated, partial replies flush, interruption flags are correct, and `session_ended` is last | should be `tests/eval/test_fake_app_ground_truth.py` | MISSING TEST |
| S4 | Given Nova's long response, when a timed tester barge-in fires, then voicebox and final Nova ground truth both record the interruption during bot speech and clean teardown | live evidence artifact under `docs/artefacts/feat-eval-fake-app/` | PARTIAL: voicebox side passed; final observer version MISSING |

## Gaps

| Gap | Evidence | Consequence |
|---|---|---|
| The final `GroundTruthObserver` changes (`during_bot_speech`, interrupted reply flush, `session_ended`, SIGTERM handling) have not been exercised together in a real WebRTC session | `docs/artefacts/feat-eval-fake-app/t-r1-review-fixes.md:67-70`; `docs/artefacts/feat-eval-fake-app/t-r2-simplify-fixes.md:70-73` | S3/S4 can claim a trustworthy reference timeline while its most important final record semantics remain unverified. |
| `ScriptedBrain`, provider validation, and `GroundTruthObserver` have no automated tests; `bot.py`/`brain.py` are not pytest test modules | `pyproject.toml:87-89`; the branch adds no `test_*.py`; only manual/probe evidence is recorded at `docs/artefacts/feat-eval-fake-app/t1-fake-app-phase1.md:129-176` and `t-r2-simplify-fixes.md:45-73` | Regressions in D-I3/D-I4 are invisible to the 92-test suite and can invalidate S2-S4 before a costly live run detects them. Per delta discipline these tests should land on this branch. |
| Nova and voicebox share `voicebox.processors.kokoro_tts.KokoroTTSService` | `tests/eval/fake_app/bot.py:49`, `:161`; review finding `docs/artefacts/feat-eval-fake-app/t-r2-simplify-fixes.md:23-31` | A Kokoro regression can move both the system under test and its reference app together, making S2-S4 blind to that fault class. This requires a design decision (stock vs shared TTS), not a silent code tweak. |
| S1's specified real-microphone onboarding path has not been run | `docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md:112-117`; `docs/artefacts/feat-eval-fake-app/t1-fake-app-phase1.md:170-176` | A new contributor may install and serve Nova successfully yet still fail at browser permission, capture, or real-mic conversation—the observable onboarding outcome. |
| The core 4+1 document still describes only voicebox's three local processes and has no Nova development/physical elements or promoted scenario | `docs/architecture/4plus1.md` Physical view and Scenarios; this delta's View impact | After merge, the living architecture would omit the repository's canonical live target and fourth process. Fold this delta into core before merge. |

