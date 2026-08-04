# BUILDLOG

Append-only decision record for voicebox. One numbered entry (D1, D2, …) per decision,
written **at the moment the decision is made** — never reconstructed, never rewritten.
Reversing a decision gets a *new* entry that points back at the old one.

Walkthroughs, specs and evidence artefacts cite entries by number (`BUILDLOG.md D3`).

**Before D1:** this repo predates the practice. Decisions taken before 2026-08-01 are not
numbered here; they live in `docs/specs/` (design + execution specs), `docs/design/`
(architecture reviews) and the "Non-obvious facts & traps" section of `CLAUDE.md`.

---

## D1 — Adopt walkthrough discipline mid-branch rather than at the next branch

*2026-08-01. Branch: `fix/audio-path-and-reporting`.*

**Decided:** start `BUILDLOG.md`, `docs/walkthroughs/fix-audio-path-and-reporting.md` and
per-task evidence artefacts now, on a branch that is already three commits in, instead of
waiting for a clean branch start.

**Why:** this branch is exactly the case the practice exists for — eight tasks (A–H), five of
them touching `agent.py` serially, each changing numbers that later tasks depend on
(`docs/specs/2026-07-31-fix-plan-execution.md`). Without a living walkthrough the review
surface is a 1500-line diff.

**Rejected:** back-dating BUILDLOG entries for Tasks A–C so the log looks complete. A
reconstructed rationale is a guess wearing a timestamp; the pre-D1 note above says plainly
where the earlier reasoning lives instead.

**Consequence:** Task A's evidence artefact is written retrospectively and labelled as such —
its contents are test output captured on 2026-08-01, not at the time A landed.

---

## D2 — Transcript→interval matching stays arrival-ordered; the test moves instead

*2026-08-01. Task B, branch `fix/audio-path-and-reporting`.*

**Context:** `test_turns_count_matches_utterances` asserted that a session with a dropped
middle transcript puts `"three"` on the *third* app-bot turn. `_match_app_transcripts`
(`metrics.py:210-229`) puts it on the second, and the test failed.

**Decided:** keep the implementation's rule — a transcript is claimed by the earliest
still-unclaimed interval that had already finished when it arrived — and correct the test.

**Why:** the expected list `["one", None, "three", None]` is not produced by *any* coherent
matching rule. Claiming from the left gives `["one", "three", None, None]`; claiming from the
right (latest finished interval) gives `["one", None, None, "three"]`. It was hand-written, and
its own explanatory comment contradicts it. The test also drifted from the spec story it cites
(B1 says four utterances, three transcripts, **one** flagged missing; the fixture had two).

**Rejected:** content- or lag-based matching, which would assign `"three"` correctly. Whisper's
per-turn lag is not constant and nothing here can read the audio, so any such rule would be a
heuristic dressed as a fact. Arrival order is the only thing the log actually knows.

**Consequence:** the failure mode is real and stays — a dropped transcript shifts text onto a
neighbouring turn. It is documented in `metrics._BIAS_NOTES` and now *pinned by a test*
(`test_dropped_transcript_shifts_text_onto_the_next_turn`) so it cannot change silently. Turn
*counts* still reconcile, which is what Task B's criteria actually require: the shift moves text
between turns, it never loses one.

---

## D3 — The VAD moves upstream as a pipeline stage, not as a transport parameter

*2026-08-01. Task D, branch `fix/audio-path-and-reporting`.*

**Context:** the execution spec's criterion 1 says `WebsocketServerParams` must carry
`vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=VAD_STOP_SECS))`. It does not: in
pipecat 1.3.0 `WebsocketServerParams.model_fields` has no `vad_analyzer` at all (checked, not
assumed — the check is in the Task D artefact). The VAD became a pipeline processor,
`pipecat.processors.audio.vad_processor.VADProcessor`. Confirmed afterwards against the latest
release too (1.6.0, see D4): transports still carry no VAD, and `vad_processor.py` is
byte-identical to 1.3.0's. The spec was written against the pre-1.0 `TransportParams` API, not
against a version we could upgrade to.

**Decided:** implement the task's *intent* — put a `VADProcessor` stage between
`transport.input()` and the STT — rather than stop and ask. Recorded here and surfaced in the
commit message and the task report instead.

**Why:** the spec's title is "Move the VAD upstream of the STT" and its whole rationale is
about frame ordering, not about which object owns the analyzer. In this pipecat version there
is exactly one way to place a VAD upstream of a processor, so there was no judgement call to
delegate. `VADProcessor` broadcasts the same VAD frames both directions that the aggregator
did, so nothing downstream changes shape.

**Rejected:** pinning an older pipecat that still had the transport parameter. The parameter
was a convenience wrapper over the same `VADController`; downgrading to satisfy the letter of a
spec is not a fix.

**Consequence:** the spec's criterion 1 can never be satisfied as written; the artefact records
it as *adapted* with the API check attached, rather than as met. Three factories were extracted
from `start()` (`_create_vad_processor`, `_create_context_aggregators`, `_build_stages`) so the
stage order and the 1.0 s `stop_secs` are assertable without booting a pipeline — Tasks E–G all
touch `start()` and now touch a shorter one.

---

## D4 — Stay on pipecat 1.3.0 until Task H lands; upgrade to 1.6.0 on its own branch

*2026-08-01. Branch: `fix/audio-path-and-reporting`.*

**Context:** we are on 1.3.0 (released 2026-05-29); 1.6.0 is out (2026-07-21). `pyproject.toml`
asks for `>=1.3.0` and `uv.lock` pinned the floor. Noticed while verifying D3.

**Decided:** finish Tasks E–H on 1.3.0. Upgrade afterwards, as a branch whose diff is the
upgrade and nothing else.

**Why:** Tasks E, F and G all *override methods inside* pipecat classes, and those are exactly
the files that churned — `services/stt_service.py` 150 changed lines, `services/tts_service.py`
247, `processors/aggregators/llm_response_universal.py` 598. Writing overrides against a
version we are about to replace means writing them twice; upgrading mid-chain would invalidate
the evidence artefacts for tasks already landed. Checked before deciding that the upgrade is
not a *substitute* for the remaining work: in 1.6.0 `SegmentedSTTService._handle_user_stopped_speaking`
still awaits `process_generator(self.run_stt(audio))` inline (Task E's bug is unfixed upstream),
the 1 s buffer trim is still at `stt_service.py:842-843`, and `BOT_VAD_STOP_SECS = 0.35` is
still at `base_output.py:55` (Task G).

**Rejected:** upgrading now to "get it over with". Nothing in E–H depends on a 1.4–1.6 feature,
and voicebox has never been run against 1.6.0 — establishing that is a task, not a side effect.

**Consequence:** the upgrade branch must look at `wants_wav_segments`, new in 1.6.0's
`SegmentedSTTService`: it decides whether `run_stt` receives a WAV container or raw PCM, and
both our STT wrapper and Task E's queueing override assume the WAV framing 1.3.0 always used.

---

## D5 — Task E intercepts `run_stt` instead of overriding `_handle_user_stopped_speaking`

*2026-08-01. Task E, branch `fix/audio-path-and-reporting`.*

**Context:** the execution spec's criterion 1 says the new processor "overrides
`_handle_user_stopped_speaking` to enqueue the segment and return immediately". That method does
two things: it frames the buffered audio into a WAV container, and it awaits transcription.
Overriding it means copying the framing.

**Decided:** override `run_stt` instead. On the frame task it queues the segment and yields
nothing; the worker calls `super().run_stt` for the real transcription. `_handle_user_stopped_speaking`
is left untouched and still does the framing.

**Why:** the copied framing would be a silent time bomb. pipecat 1.6.0 has *already* changed
exactly those lines — `wants_wav_segments` now decides whether `run_stt` receives a WAV
container or raw PCM (D4) — so a copy made today would keep building WAVs for a local model
that, on upgrade, expects PCM, and the failure would look like bad transcription rather than
like a merge problem. Intercepting `run_stt` leaves the framing in pipecat where it belongs;
the upgrade branch has one less landmine.

**Rejected:** copying the ~12 lines with a "mirrors stt_service.py:765-780" comment. Comments
do not fail a test run.

**Consequence:** `run_stt` now means two things in this class hierarchy — "hand off" at the
front of the MRO, "transcribe" further along. The composition order is load-bearing
(`NonBlockingSegmentedSTT, TimedSTTMixin, WhisperSTTService`) and is asserted in the artefact
rather than left to a reader's MRO arithmetic.

---

## D6 — The non-blocking STT applies to both platforms, not just the non-Darwin path

*2026-08-01. Task E, branch `fix/audio-path-and-reporting`.*

**Decided:** wire `NonBlockingSegmentedSTT` into the MLX (Darwin) service as well as
faster-whisper, which the spec left to the implementer's judgement.

**Why:** the inline await is in `SegmentedSTTService`, which both Whisper services inherit. The
bug is identical on both. Fixing one would create exactly the parallel-flow split that hides
defects: each path stays internally consistent, per-path tests pass, and the Mac path keeps a
51-second `speak()` delay that nothing on CI can see.

**Consequence:** the MLX path is wired but unexercised — this machine is Linux, and no test in
the suite constructs the MLX service. Stated here so it is not mistaken for verified.

---

## D7 — Remove `scripts/e2e_readme_call.py`

*2026-08-03. Branch: `fix/audio-path-and-reporting`. User-directed.*

**Context:** the first blind verification round was blocked at login: the script's hardcoded
test-account password had drifted from the app's local database (the checked-in value had a
lowercase first letter). The user corrected the password and asked for the script itself to be
removed from the repo.

**Decided:** delete the script and scrub the living docs that pointed at it (`CLAUDE.md` file
map + dev workflow + quality-checks note, `README.md` examples, the upgrade roadmap's exit
criteria). Historical documents (`notes.md`, `docs/superpowers/specs/`) keep their mentions —
they record what was true when written.

**Why:** the script was a hardcoded-credential, single-app driver whose job — a full
login → call → conversation → teardown pass — is now done by live dogfood/blind-verification
sessions driving the MCP tools directly. Stale credentials in a checked-in file are worse than
no file: they fail closed and block a run that would otherwise proceed.

**Consequence:** `scripts/smoke_browser_shim.py` is the one remaining scripted driver (audio
path only, no app needed). Live e2e coverage is session-driven; test-account credentials no
longer live in the repo.

---

## D8 — Whisper's lazy decode is made eager inside the thread; this closes C3

*2026-08-03. Round-1 blind verification, branch `fix/audio-path-and-reporting`.*

**Context:** the round-1 live session showed app-bot transcripts arriving 25–59 s after speech
stopped, `transcription_lag_secs` pinned at 0.0 throughout, and `speak()` audio starting 4–12 s
after the call. faster-whisper's `WhisperModel.transcribe` returns a **lazy** generator;
pipecat's `WhisperSTTService.run_stt` wraps only the `transcribe()` call in `asyncio.to_thread`
and then iterates the segments on the event loop — so the entire decode runs ON the loop.
Measured on a real 40 s Ember narration from the session recording
(`probe_lazy_whisper.py`, output in the round-1 artefact): the call returned in 35 ms,
iteration took 21.2 s with a 21.24 s max loop stall; the eager shape took the same 22.8 s but
inside the thread, max loop stall 55 ms.

**Decided:** wrap the loaded model in `EagerSegmentsWhisperModel`
(`processors/nonblocking_whisper_stt.py`), whose `transcribe()` materializes the segments
before returning — so the decode happens inside pipecat's own `to_thread` call. Wired via a
`_load()` override in `_NonBlockingWhisperSTTService`.

**Why this closes C3:** the ~24 s per-turn lag the triage could not attribute is Whisper decode
(~0.5× realtime on this CPU for long narrations), not `LocalSmartTurnAnalyzerV3`. The
conditional follow-up in the execution spec ("drop `TurnAnalyzerUserTurnStopStrategy` if the
24 s is smart-turn inference") is therefore **not triggered**.

**Why the loop freeze mattered beyond lag:** it re-broke Task E live (the worker task still
froze the loop, so `speak()` stalled behind decode), and it made `transcription_lag_secs`
unreadable (nothing could observe the queue mid-freeze — every `listen()` reported 0.0).

**Rejected:** overriding `run_stt` to do the whole transcription differently — copies pipecat's
post-processing (D5's precedent applies). Also rejected: wrapping the MLX path; checked —
`mlx_whisper.transcribe` returns a plain dict, already eager.

---

## D9 — The aggregator's turn-stop watchdog is raised from 5 s to 90 s

*2026-08-03. Round-1 blind verification, branch `fix/audio-path-and-reporting`.*

**Context:** round 1 showed `app_bot_transcript.turn_started_at` reporting transcript-*arrival*
time, off by 25–34 s from the acoustic turn start. Traced in pipecat source
(`turns/user_turn_controller.py`): the `UserTurnController` watchdog force-stops a turn after
`user_turn_stop_timeout` (default **5 s**) of frame inactivity. With batch Whisper the
transcript arrives long after the VAD stop, so the watchdog closed every long turn *empty*
(the empty `UserTurnStoppedMessage` is then swallowed by the `if message.content:` gate — Task
F's F2 hole, independently confirmed); the late transcript then triggered
`TranscriptionUserTurnStartStrategy`, opening a fresh turn stamped at arrival time, which
stopped seconds later with the text. Hence `turn_started_at` ≈ event `t`.

**Decided:** `user_turn_stop_timeout=TURN_STOP_TIMEOUT_SECS` (90 s) on
`LLMUserAggregatorParams`. 90 s covers the decode of a ~170 s narration at the measured ~0.5×
realtime; a genuinely transcript-less turn still closes, just later.

**Rejected:** stamping `turn_started_at` ourselves from observed VAD events — re-implements the
aggregator's bookkeeping and drifts from it; the parameter fixes the cause. Also rejected:
removing `TranscriptionUserTurnStartStrategy` — with the watchdog fixed it is again the
harmless fallback pipecat documents.

---

## D10 — `turn_started_at` is stamped from voicebox's own VAD log after all (reverses part of D9's rejection)

*2026-08-03. Round-2 blind verification, branch `fix/audio-path-and-reporting`.*

**Context:** D9 fixed the watchdog case but round 2 showed 7/14 transcripts still carrying
arrival-time turn starts (off by up to 103 s). The remaining case: during a bot monologue,
chunk N+1 VAD-starts while chunk N's transcript is still in Whisper. The turn is then still
open, so the VAD start cannot open a new one; when chunk N's transcript closes the turn,
chunk N+1's *transcript arrival* is what re-opens it — stamped at arrival. First-of-monologue
transcripts were correct, later ones were not; the tester's report showed exactly that split.

**Decided:** the transcript event claims the earliest unclaimed VAD start from the agent's own
observer log (`_unclaimed_bot_speech_starts`, arrival-ordered) — the very thing D9 rejected.
D9's rejection assumed the aggregator's stamp was fixable by configuration; round 2 proved the
overlap case is structural under batch STT, so voicebox's log is the only correct source.
The aggregator's stamp remains as fallback when the deque is empty.

**Consequence:** the claim rule and its bias are the same as `metrics._match_app_transcripts`
(D2): a segment Whisper returns nothing for shifts later claims by one interval. Task F's
empty-transcript events will make those segments claim their own start, shrinking the bias.

---

## D11 — `listen()` settles 50 ms before sampling `transcription_lag_secs`

*2026-08-03. Round-2 blind verification, branch `fix/audio-path-and-reporting`.*

**Context:** every `listen()` woken by an `app_bot_speech_stopped` event read
`transcription_lag_secs: 0.0` — deterministically, because the observer appends the event at
the VADProcessor→STT hop, waking the listener before the STT has processed that same frame and
queued the segment. The caller reads "nothing pending" at the exact moment a transcript is
guaranteed to be pending.

**Decided:** when the returned batch contains a speech stop, sleep 50 ms before sampling the
lag.

**Rejected:** recomputing the lag from the event log (age of the oldest speech stop without a
matching transcript). More truthful in principle, but until Task F emits empty-transcript
events, a segment Whisper returns nothing for would inflate that lag for the rest of the
session — lying in the opposite direction. Revisit after F if the 50 ms window ever bites.

---

## D12 — Gaps spanning a disconnect are outages, not conversation metrics

*2026-08-03. Round-3 blind verification, branch `fix/audio-path-and-reporting`.*

**Context:** twice now an app outage has been averaged into the conversational numbers: round 1
booked a 431 s outage as an app "response latency", round 3 booked a 107 s wedge (bot goodbye →
client stuck → page reload) as **both** a dead-air gap and a response latency, dragging
`mean_app_response_latency_secs` to 17.776 s when the honest per-turn values were 2.9–7.0 s.

**Decided:** a silent gap containing a `client_disconnected` event is quarantined into a new
`outage_gaps` bucket (`total_outage_secs` in the summary), and a pending tester utterance is
disarmed by a disconnect so the app's first words after reconnecting record no latency. Bias
note added. Replayed on the round-3 log: mean latency 17.776 → 5.022 s, the 107.051 s span
quarantined.

**Rejected:** an outlier threshold (e.g. drop latencies > 60 s) — a genuinely slow app is
exactly what voicebox exists to report; only a link-down span is categorically not the app
being slow. The `client_disconnected` event is the ground truth for that.

---

## D13 — Task F's drain budget scales with the backlog instead of the spec's flat ~15 s

*2026-08-03. Task F, branch `fix/audio-path-and-reporting`.*

**Context:** the execution spec's criterion 1 says `stop()` awaits the STT drain "bounded
(~15 s)". Round 3 measured a real transcript backlog of **140 s** (long narrations, ~0.55×
realtime CPU decode, serial queue) — a 15 s bound would have drained one segment and lost the
other three, defeating F1's whole point on exactly the sessions that need it.

**Decided:** budget = 15 s base + 1 s per second of queued/in-flight audio (bytes tracked in
`NonBlockingSegmentedSTT`), capped at 180 s. 1 s/audio-second is ~2× the measured decode rate;
the cap keeps a wedged Whisper from stalling teardown (F3). Spec criterion adapted, not met as
written — D3/D5 precedent.

**Rejected:** a bigger flat bound (e.g. 180 s always) — it makes the *pathological* case (a
hung transcription) stall every teardown the full window, while the adaptive budget only waits
long when there is real work to wait for.

---

## D14 — `transcription_lag_secs` stays queue-age-based; the D11 revisit is closed

*2026-08-03. Task F, branch `fix/audio-path-and-reporting`.*

**Context:** D11 deferred a possible switch to event-log-based lag (age of the oldest speech
stop without a matching transcript) until F's empty-transcript events made it safe. Round 3
then showed the aggregator can **merge** two speech intervals into one transcript event (a
1.2 s and a 127.6 s interval, one event). Count-based matching of stops to transcript events
therefore under-counts events and would report a permanently-growing lag after any merge —
strictly worse than the queue-age proxy's known blind spot (decoded text held by an open
turn).

**Decided:** keep the queue-age lag. The blind spot is documented; the F1 drain moots the
"safe to stop?" use of the field, which was the strongest reason to change it.

---

## D15 — Every timeout above the drain must outlive the drain

*2026-08-03. Round-4 blind verification, branch `fix/audio-path-and-reporting`.*

**Context:** round 4 lost its entire artifact set. Task F's drain made the child's `stop()`
legitimately slow (up to the 180 s cap), but two timeouts written before F still assumed a
fast path: `server.py`'s IPC stop deadline (30 s — the parent timed out, **reaped the child
mid-drain before `_dump_artifacts` ran**, and returned `{"stopped": true}` with no artifacts),
and the 90 s turn watchdog (a 116 s narration decoded for 108 s under load — ~0.93× realtime,
not the probe's idle 0.53× — so the turn closed empty at 90 s and the text re-emitted as an
orphan event at arrival time).

**Decided:** order the timeouts by what they wait for: decode ≤ drain cap (180 s) < IPC stop
deadline (210 s) < nothing. Turn watchdog 240 s > drain cap, so any decode the session would
wait for also beats the watchdog. `server.py` keeps the literal with a comment naming
`DRAIN_CAP_SECS` rather than importing it — the parent must never load pipecat (hot-reload
contract).

**Also landed:** a per-session `agent-debug.log` (DEBUG sink next to the artifacts when
`record_dir` is set). Round 4 measured a ~25 s transcript-lag floor on even 4 s utterances;
Task C's `voicebox.timing` lines exist to attribute exactly that but died with the terminal.
Next round reads them from the artifact directory.

**Rejected:** importing `DRAIN_CAP_SECS` into `server.py` for a single literal (breaks the
parent's pipecat-free property); "fixing" the tester-reported slow barge-in arm — re-reading
its own timestamps, the arm landed ~1.5 s after the MCP call and the missed utterance was the
tester's own turnaround (it armed after Ember had already started speaking). Not a defect
until the new debug log says otherwise.

---

## D16 — Kokoro warms up at agent start; the lag floor is attributed and left to the issue

*2026-08-03. Round-5 blind verification, branch `fix/audio-path-and-reporting`.*

**Context:** round 5 passed the F1 prompt-stop test (stop blocked 32 s, drained, final
transcript present) but found the session's **first** `speak()` split by a 5.33 s mid-utterance
silence — the app took the turn and got talked over. pipecat's TTS layer synthesizes per
sentence, so Task G's per-call buffering cannot bridge a gap *between* sentences; the gap was
the first ONNX inference's one-time cost. Later utterances in rounds 4–5 (13 speaks) were all
single-span.

**Decided:** a fire-and-forget throwaway synthesis (`KokoroTTSService.warm_up`) at agent
start, concurrent with the browser child's startup. Also: `agent-debug.log` is now listed in
`stop()`'s artifacts dict (round 5 flagged the omission).

**Also recorded:** the debug log attributed the ~25 s transcript-lag floor entirely to
`model.transcribe` — `run_stt` 22–28 s regardless of segment length (6.3 s audio → 27.6 s;
93 s → 106.8 s), analyzer 0.3–0.4 s, handler ~0. C3 is closed with live numbers; the
optimization avenues (temperature-ladder capping, `beam_size=1`, smaller model) are on
issue #13, deliberately outside this branch (the spec pins the CPU config).

**Rejected:** buffering across sentence boundaries in the TTS aggregation layer — it would
re-implement pipecat's sentence segmentation to fix a cost that only ever bites once per
process, and rounds 4–5 show warm synthesis keeps up with playout.

---

## D17 — TOKEN text aggregation: one `speak()` is one synthesis call (partially reverses D16's rejection)

*2026-08-03. Round-6 blind verification, branch `fix/audio-path-and-reporting`.*

**Context:** round 6 deliberately used multi-sentence utterances and showed Task G's buffering
is per-`run_tts`-call while pipecat's default SENTENCE aggregation makes one call *per
sentence*: a 3-sentence speak split into three speech pairs (gaps up to 11.5 s under STT CPU
contention), the app answered the first fragment and was talked over by the rest, and
`wait_for_playout` resolved at the first sentence's own `TTSStoppedFrame`. D16 had rejected
"buffering across sentence boundaries" believing warm synthesis keeps up — it does for short
single-sentence texts, not for long ones competing with Whisper for CPU.

**Decided:** `text_aggregation_mode=TextAggregationMode.TOKEN` on the Kokoro service. TOKEN
mode passes each incoming `TextFrame` through whole (verified in
`SimpleTextAggregator.aggregate`), and voicebox queues exactly one `LLMTextFrame` per
`speak()` (`_queue_speak_frames`) — so the entire utterance reaches one `run_tts` call, whose
Task-G buffering then genuinely covers the utterance: one gap-free span, one
TTSStarted/Stopped pair, correct `_Playout` resolution. No pipecat code re-implemented — this
is the mode pipecat ships for exactly the "full response arrives at once" shape.

**Also closed with log evidence:** the "slow barge-in arm" (rounds 4–6): the round-6 debug log
shows `Command 'speak' received, dispatching...` → `tester_barge_in_armed` in **1 ms**. The
~8.5 s the testers measured is their own LLM turnaround before the call is issued. Guidance
(Task H docstrings): arm *early* — arming is instantaneous server-side and the trigger only
fires on events after the arm.

**Deferred with evidence (the aggregator redesign, already out of scope):** round 6's
turns-4+5 transcript merge desynced both the D10 turn-start deque and metrics' positional
matcher by one for the rest of the session (including a false `transcript_missing` on the
final turn), and transcript delivery is structurally one-turn-behind under the current
`UserTurnController`. Per-segment transcript emission (observer watching `TranscriptionFrame`
directly, no aggregator) would fix both and delete the watchdog dance — that is the
"drop `LLMContextAggregatorPair`" design task the execution spec already names.

---

## D18 — `wait_for_turn` reports how long it blocked instead of documenting the opacity

*2026-08-04. Task H, branch `fix/audio-path-and-reporting`.*

**Context:** round 1 flagged that `speak(wait_for_turn=True)` returns the same bare
`{queued: true}` as the ungated path — a caller cannot tell whether the polite gate waited
40 s or the bot was already silent, and rounds 4–6 showed testers repeatedly misattributing
their own timing to voicebox. Task H's brief left the fix open: add a small honest key or
document the opacity.

**Decided:** add `waited_for_turn_secs` (monotonic span around `_wait_for_app_bot_silent`,
rounded to ms) to every `speak(wait_for_turn=True)` result shape — ungated returns keep the
bare shape, so the key's presence itself marks the gated path. Four lines in `agent.py`, no
IPC change (the dict passes through verbatim), pinned by
`test_wait_for_turn_reports_wait_duration` / `test_ungated_speak_has_no_wait_key`.

**Rejected:** documenting the opacity only — the number already exists server-side, is
exactly what the round-4/5/6 "is voicebox slow or am I?" confusions needed, and costs less
to report than to explain away. Also rejected: a boolean `waited` — same cost, strictly
less information.

---

## D19 — The playout observation window scales with text length (round 7)

*2026-08-04. Round-7 blind verification, branch `fix/audio-path-and-reporting`.*

**Context:** round 7's very first, by-the-book speak (61 words, 3 sentences,
`wait_for_playout=True`) returned `played: false` on a perfectly healthy playout: under D17's
TOKEN aggregation the whole utterance is synthesized before any audio plays, so playout
started 17.8 s after queueing and ended at +36.2 s — past the flat
`PLAYOUT_TIMEOUT_SECS = 30`. The `reason` text was accurate (the tester verified the audio
via `listen()`), but the flag lied on exactly the long-first-utterance shape rounds 5–6 made
the recommended pattern.

**Decided:** window = `PLAYOUT_TIMEOUT_SECS + PLAYOUT_SECS_PER_WORD * words`, with
`PLAYOUT_SECS_PER_WORD = 0.8` — ~2x the measured audio rate (Kokoro af_heart ≈ 0.3 s/word;
round 7 measured queue→playout-end ≈ 2x audio duration under STT CPU contention). The
`server.py` IPC deadline mirrors the formula (`60.0 + 0.8 * words`, literal + comment, not
imported — D15's ordering rule, parent stays pipecat-free).

**Also closed with parent-side proof (extends D17's closure):** the tester again measured
"slow" speak/arm returns (4.5–9 s). The parent log shows the arm's `CallToolRequest`
processed at 11:16:56 and the child armed at 11:16:56.608 — the entire server-side path is
~10 ms; the gap is the caller's own LLM turnaround before the HTTP request lands. Not a
defect; the D17 guidance (arm early) stands.

**Rejected:** a two-phase wait (bounded wait for `tester_speech_started`, then open-ended
wait for the stop) — more precise but more machinery, and the single scaled bound already
covers the failure shape with a 2x margin. Also rejected: raising the flat timeout — it
would make the pathological case (audio truly never playing) block every diagnosis for
minutes.
