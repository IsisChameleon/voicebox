# How Cekura, Coval, Hamming & friends engineered voice-agent testing

*2026-06-11. Competitive/engineering research informing the voicebox upgrade. Companion docs:
[architecture-review.md](architecture-review.md), [upgrade-roadmap.md](upgrade-roadmap.md).
The full source-by-source research report is appended at the bottom.*

## The common architecture

Every serious tool uses the same shape: **the synthetic caller is itself a full voice-agent
pipeline** — TTS + STT + VAD + turn-detection — running continuously and full-duplex, connected to
the agent under test over real audio. fixa (open source) is the clearest blueprint: a Pipecat-based
test agent with Cartesia TTS + Deepgram STT, calling the target over Twilio, with an LLM judging
the transcript afterwards [1]. ServiceNow's EVA uses an ElevenLabs agent as the simulated user
speaking live audio over WebSocket to a Pipecat agent under test [2]. Cekura's testers join
Pipecat/WebRTC sessions or dial real phone numbers [3]; Hamming joins LiveKit rooms natively, no
SIP [4].

**Nobody polls.** Turn-taking lives inside the synthetic caller's own streaming pipeline (its VAD
decides when to talk), not in an outer LLM loop. So voicebox's pipecat-child design is the right
architecture — the gap is only that the child's real-time intelligence is currently zero: every
turn decision round-trips through Claude via blocking `listen`/`speak`.

## How they handle interruptions — the key lesson

None of them do reactive LLM-driven barge-in either (it's physically too slow). Instead:

- **Interruption is a persona parameter, executed by the pipeline.** Coval exposes an "interruption
  rate" knob per persona [5]; Cekura ships "Interrupter"/"Pauser" personalities among 50+ [3];
  Hamming's voice characters inject overlapping speech, interjections, and mid-turn topic changes —
  plus network impairment like jitter and packet loss [4]. The scenario says *"this caller
  interrupts"*; the audio pipeline picks the moment.
- **The measured outcome is split in two**: did the agent *detect* the barge-in, and how fast did
  its TTS *stop* (budget ≈60 ms, "interruption stoppage timing" in Cekura's metric set) — plus the
  inverse check: did the *agent* talk over the *user* (Coval flags this from recordings) [3][5][6].
- Vapi's own test suite is explicit that interruption testing requires the full audio path
  (`vapi.websocket`), not chat mode [7].

This validates the declarative design in the review doc: `speak(text, when="bot_speaking",
after_secs=1.5)` — a trigger armed in the pipecat child, fired at audio-rate. That's exactly what
an "Interrupter persona" compiles down to.

## How they evaluate — three planes, all post-hoc from the recording

1. **LLM judge over the transcript** (Vapi rubrics, Retell 0–1 scores with reasoning, LiveKit
   `.judge()`, fixa `Evaluation` prompts).
2. **Deterministic checks** on tool calls / task completion — with tool *mocking* in the harness
   (Vapi scenario mocks, EVA's deterministic Tool Executor) so error paths are reproducible.
3. **Signal metrics computed from the recording**: response latency measured as
   end-of-user-speech → start-of-agent-audio in the waveform, talk-over windows, dead air,
   WPM/pitch, time-to-first-*playable*-audio (TTFA, not TTFB — first bytes are often container
   headers) [6][8]. Coval and EVA add an audio-input LLM judge that listens to the recording itself
   (stutters, speech fidelity).

EVA's artifact convention is worth stealing directly: **one mixed stereo WAV, user on one channel,
bot on the other** [2]. voicebox's `record_dir` already writes both tracks plus a merge — switching
the merge to stereo and adding a timestamped per-turn event JSON would make latency, overlap, and
dead-air all computable offline from one artifact. Hamming goes further with production-call
*replay* (a real failed call becomes a regression test preserving original timing) — a natural
future feature given the WAVs already exist.

## Other transferable patterns

- **Two-tier testing**: text-level simulation for CI speed (Pipecat's own guidance: inject
  `TranscriptionFrame`s, skip STT/TTS entirely [9]; LiveKit's test framework is deliberately
  text-only), full-audio for pre-release. voicebox could expose a text-mode flag that bypasses
  Kokoro/Whisper for fast scenario iteration — though voicebox's whole niche is the audio path, so
  this is secondary.
- **Scenario as a first-class object**: persona + goal + behavior + success criteria, run N times
  (Vapi retries up to 5×) to absorb LLM nondeterminism. For voicebox/MCP this is naturally a
  prompt-side convention (a skill or doc telling Claude how to run a scenario), not server code.
- **Validate the simulator itself.** EVA auto-checks that the simulated user stayed faithful to its
  goal and regenerates broken runs — a flaky synthetic caller is the top source of false failures.
- **VAD-boundary timing needs calibration before it's reported as a metric.** Deepgram's
  end-of-turn evaluation work found human-labeled boundaries systematically late and recommends
  sequence-alignment scoring rather than fixed time windows [10]. With `stop_secs=1.0`, voicebox's
  utterance timestamps carry a fixed ~1 s bias — fine if documented and subtracted, misleading if
  not.

## Where voicebox is genuinely differentiated

Hamming's LiveKit room-join is the only documented commercial "join the session natively, no phone"
design — and it requires the agent platform's cooperation (room APIs). **voicebox's browser shim
reaches apps whose WebRTC internals you can't join at all** — anything that only exists as a
webpage. None of the surveyed tools cover that. So the path is: keep the shim moat, and close the
gap on what the others standardized — the event-stream `listen`, declarative barge-in, and
recording-derived metrics, in that order. The two prerequisites are the verified bugs from the
review doc: disable pipecat's default interruption-on-VAD (our own speech currently dies the moment
the bot talks) and make the IPC loop concurrent.

## References

[1] fixa — https://github.com/fixadev/fixa
[2] ServiceNow EVA — https://github.com/ServiceNow/eva
[3] Cekura: Pipecat testing & scenario guide — https://www.cekura.ai/blogs/test-pipecat-voice-agents,
https://docs.pipecat.ai/pipecat/fundamentals/evaluations/cekura
[4] Hamming: testing LiveKit agents — https://hamming.ai/blog/how-to-test-voice-agents-built-with-livekit
[5] Coval walkthrough & metrics — https://webrtc.ventures/2025/07/how-to-automate-voice-ai-agent-testing-evaluation-with-coval/,
https://docs.coval.ai/concepts/metrics/overview
[6] Hamming eval guide (barge-in budgets, latency percentiles) — https://hamming.ai/resources/voice-agent-testing-guide
[7] Vapi test suites / voice testing — https://docs.vapi.ai/test/voice-testing
[8] Voice AI primer (latency ground truth) — https://voiceaiandvoiceagents.com/
[9] Pipecat evaluations overview — https://docs.pipecat.ai/pipecat/fundamentals/evaluations/overview
[10] Deepgram: evaluating end-of-turn detection — https://deepgram.com/learn/evaluating-end-of-turn-detection-models

---
---

# Appendix: full research report (source-by-source)

*Research conducted June 2026 via vendor docs, engineering blogs, and open-source repos. Facts are
tagged with source URLs; where vendors only publish marketing-level material, that gap is noted
explicitly rather than guessed at.*

## 1. Cekura (formerly Vocera AI)

**Transport to agent under test — three paths, all "real audio":**

- **Native WebRTC session join**: Cekura's testing agents "automatically join Pipecat sessions and
  interact with the deployed voice agent" using Pipecat's WebRTC transport. For Pipecat Cloud they
  handle session creation/lifecycle/cleanup automatically given an API key + agent name; a manual
  mode accepts a user-supplied room URL + token for custom orchestration. [1][2]
- **Telephony/SIP**: dials the agent's real phone number across Vapi, Retell, ElevenLabs, Bland,
  Pipecat — explicitly positioned as validating "the full voice and network path" vs. the faster
  direct-WebRTC path. [2][3]
- **API-based execution** for CI (GitHub-triggered runs on prompt/model changes). [4]

**Synthetic caller behavior:** scenario-driven (persona + goal + behavior + success definition),
with **50+ predefined personalities** including "Interrupter" and "Pauser" specifically to stress
turn-taking; supports accents, background noise profiles, hesitation patterns, speech speeds, and
custom cloned voices (via Cartesia). [1][4]

**Interruption testing:** done via the personality system (interrupter personas) rather than
documented timed injection; they measure **"interruption stoppage timing"** as an explicit metric —
i.e., how fast the agent's TTS halts after barge-in. [1]

**Metrics (25+ predefined, three buckets):** [1][2]

- Speech quality: WPM, tone, average pitch, pronunciation accuracy, gibberish detection
- Conversational flow: response latency, interruption stoppage timing, silence/dead-air detection,
  repetition frequency, call-termination accuracy
- AI behavior: instruction adherence, response relevancy, hallucination, tool-call success
- Plus an "Infrastructure Suite" of 18+ pre-built scenarios covering latency, audio quality,
  interruption handling, failure cases. [2]

**Load testing:** parallel simulated callers; red-team suite of 10,000+ adversarial scenarios. [3][4]

**Gap:** no public low-level architecture (pipeline internals, VAD config) — their blogs are
capability-level only. [5]

Sources: [1] https://www.cekura.ai/blogs/test-pipecat-voice-agents ·
[2] https://docs.pipecat.ai/pipecat/fundamentals/evaluations/cekura ·
[3] https://www.cekura.ai/blogs/voice-load-testing ·
[4] https://www.cekura.ai/blogs/complete-cekura-scenario-testing-guide ·
[5] https://www.cekura.ai/blogs/performance-testing-voice-agents-practical-guide-cekura

## 2. Coval (coval.dev / coval.ai)

**Heritage:** founded by ex-Waymo simulation engineers; the product is explicitly modeled on
self-driving-car simulation methodology (scenario banks, regression sims, CI gating). [1]

**Transport:** four interfaces — **inbound calls** (Coval dials a phone number), **outbound calls**
(your agent calls Coval), **WebSockets** (for self-hosted agents: "expose your agent over a
WebSocket transport and point Coval at the endpoint"), and platform-native **LiveKit / Pipecat
Cloud** connections. A WebRTC.ventures walkthrough shows the inbound-call mode against a
LiveKit+Twilio bot. [2][3]

**Simulator model — three configurable objects:** [2]

- **Agent**: interface + the agent's own prompt (Coval uses your prompt to know expected behavior —
  a notable trick: the simulator/judge is grounded in the system-under-test's spec)
- **Persona**: voice, accent, background-noise environment, **interruption rate**, emotional
  progression — interruption behavior is a tunable persona parameter, not a separate test type
- **Test case**: natural-language scenario; also accepts example interactions as **text, audio, or
  graph** formats

**Metrics:** default set includes response latency, **interruption detection/frequency (including
the bot interrupting the user — talk-over)**, call resolution; audio metrics computed **from
recordings**: latency, interruption rate, speech tempo (phonemes/sec), volume/pitch misalignment,
tool-call latency; plus an **Audio LLM Judge** that evaluates the audio itself rather than the
transcript (e.g., detect stuttering). [2][4]

**Observed behavior in practice:** in the WebRTC.ventures eval, Coval surfaced "average latency
~2 s" and flagged "at least one instance of the bot interrupting the user" — i.e., talk-over is
detected post-hoc from the recording rather than asserted in-line. [2]

**Streaming vs polling:** real-audio, full-duplex calls for voice mode (it's a phone/WS call), but
published material doesn't disclose the simulator's internal turn-taking machinery (VAD model,
end-of-turn logic). Their realtime-eval blog argues for "turn-by-turn latency and overlap"
instrumentation and evaluation "without guardrails or intermediate text" for speech-to-speech
agents. [5]

Sources: [1] https://www.ycombinator.com/companies/coval ·
[2] https://webrtc.ventures/2025/07/how-to-automate-voice-ai-agent-testing-evaluation-with-coval/ ·
[3] https://docs.pipecat.ai/pipecat/fundamentals/evaluations/coval ·
[4] https://docs.coval.ai/concepts/metrics/overview ·
[5] https://www.coval.ai/blog/evaluating-realtime-voice-to-voice-ai-agents-a-practical-guide

## 3. Hamming AI

**Transport:** "works with any SIP-capable voice agent… can connect via **SIP trunk or direct
WebRTC**." For LiveKit specifically they do **native WebRTC, LiveKit-to-LiveKit sessions — "no
phone numbers, no SIP"** — joining auto-provisioned or user-controlled rooms. [1][2]

**Synthetic callers ("voice characters"):** LLM-driven characters placed on real calls; they
**inject adversarial conditions — background noise, overlapping speech, interjections, mid-turn
topic changes, and network impairment (jitter, packet loss)** — to validate context retention and
barge-in stability. This is the most explicit public statement of *active interruption injection*
(overlap/interjection) among the commercial tools. [2]

**Scale:** 1000+ concurrent calls for load testing under production-like conditions. [1]

**Metrics (50+ dimensions):** latency, **time-to-first-word**, turn control, **talk ratio**,
**barge-in stability**, confirmation clarity, hallucination scoring, tool-call argument
correctness; targets published: barge-in recovery >90%, interruption-detection accuracy 95%+,
Time-to-First-Audio <1.7 s, WER <10% (EN), task completion >85%; production data across 4M+ calls:
P50 voice-to-voice ≈1.5–1.7 s, P90 ≈3 s, P95 ≈5 s for cascaded STT→LLM→TTS over telephony. [1][3][4]

**Other engineering features:** production **call replay** — convert a production call into a
regression test preserving original audio, timing, and caller behavior; single-tenant architecture
for PCI scope avoidance. [5]

Sources: [1] https://hamming.ai/resources/voice-agent-testing-guide ·
[2] https://hamming.ai/blog/how-to-test-voice-agents-built-with-livekit ·
[3] https://hamming.ai/resources/how-to-evaluate-voice-agents-2026 ·
[4] https://hamming.ai/resources/voice-ai-latency-whats-fast-whats-slow-how-to-fix-it ·
[5] https://hamming.ai/blog/voice-agent-testing-platforms-comparison-2025

## 4. How the voice-agent platforms test their own agents

**Vapi Test Suites:** two AI agents (your assistant + an AI tester) connected on a **real phone
call**, the tester following a predefined script; outcome scored by **LLM rubric** (list of
questions). Voice tests capped at 15 min and consume call minutes; **chat tests are recommended as
the fast path**. Advanced simulations expose two transports — `vapi.webchat` (text, for CI) and
`vapi.websocket` (full audio path) — and **interruption testing explicitly requires voice mode**
("requires `vapi.websocket` to test actual audio interruptions"). Scenario-level **tool mocks**
make error paths deterministic; lifecycle webhooks (`simulation.run.started/ended`) carry
transcripts + recordings. [1][2][3]

**Retell AI Simulation/Batch Testing:** **text-level** "AI Simulated Chat" — a user prompt
(Identity / Goal / Personality format) drives a multi-turn LLM-vs-LLM simulation; transcript is
passed to an **LLM judge scoring each metric 0.0–1.0 with written reasoning**; batch mode runs many
cases at once. Notably, Retell's first-party testing skips audio entirely. [4][5]

**Bland AI:** "Tornado" testing mode runs thousands of simulated call scenarios pre-deployment,
paired with **canary rollouts** (gradual traffic shift to new agent versions) and real-time
rule-based call monitoring — i.e., a deploy-pipeline framing rather than a turn-level test
harness. [6]

**LiveKit Agents (first-party test framework):** deliberately **text-only** —
`session.run(user_input="...")` against the real agent code with LLM in text mode; assertion chain
`result.expect.next_event().is_message(role=...).judge(llm, intent="...")`, `is_function_call()`,
mocking via `unittest.mock`; prebuilt judges: `task_completion_judge` (grounds on agent's own
instructions from chat context), `safety_judge`, `accuracy_judge` (grounding against tool outputs
to catch hallucination). Docs explicitly punt full-audio testing to Bluejay/Cekura/Hamming. [7][8]

Sources: [1] https://docs.vapi.ai/test/test-suites · [2] https://docs.vapi.ai/test/voice-testing ·
[3] https://docs.vapi.ai/observability/simulations-advanced ·
[4] https://docs.retellai.com/test/llm-simulation-testing ·
[5] https://www.retellai.com/blog/retell-ai-introduces-simulation-and-batch-testing-for-ai-agents ·
[6] https://hamming.ai/blog/voice-agent-testing-platforms-comparison-2025 ·
[7] https://docs.livekit.io/agents/start/testing/ ·
[8] https://docs.livekit.io/reference/python/livekit/agents/evals/judge.html

## 5. Pipecat's own evals + open-source frameworks

**Pipecat evaluations guidance:** two-phase strategy — (1) **local text-level testing by injecting
`TranscriptionFrame`s directly into the pipeline**, bypassing STT/TTS, to validate conversational
logic + function calling in seconds in CI; (2) production-grade audio simulation via partners
(Coval, Bluejay, Cekura). Built-in observability: per-processor **TTFB and processing-time
metrics**, transcript saving, OpenTelemetry trace export. [1]

**fixa (fixadev/fixa, open source, Python):** the cleanest reference architecture for "a voice
agent calling your voice agent": **test agent built on Pipecat** (Cartesia TTS + Deepgram STT),
**Twilio places the outbound PSTN call**, ngrok tunnels the local WebSocket for Twilio media,
**OpenAI LLM judges the transcript**. Code-level API: `Agent(name, prompt)` persona,
`Scenario(prompt, evaluations=[Evaluation(name, prompt)])`, `TestRunner` runs scenarios
concurrently against phone numbers. Turn-taking falls out of the Pipecat pipeline's own VAD;
outputs are pass/fail per evaluation + recording URL + transcript + latency + interruption
detection (in their cloud UI). [2]

**voicetest (voicetestdev/voicetest):** open-source harness; **imports agent definitions from
Retell/VAPI/Bland/LiveKit/Telnyx into a unified graph IR ("AgentGraph")**, runs LLM-powered
simulated users against the agent, LLM judges score results (models mixable per role, e.g., Haiku
simulates / Sonnet judges, via LiteLLM); CLI + REST + Web UI with real-time streaming transcripts,
DuckDB result store, GitHub Actions CI. Primarily text-level with an `audio_eval` option. [3][4]

**ServiceNow EVA:** academic-grade end-to-end framework. **Audio-native bot-to-bot**: user
simulator = ElevenLabs agent with goal+persona, speaking **live audio over WebSocket** to a
Pipecat-based agent under test; **Tool Executor** gives deterministic, reproducible tool responses;
output is mixed stereo audio + transcripts + tool logs. Its standout pattern: an **automated
validation loop that checks the user simulator itself stayed faithful to its goal — invalid
conversations are auto-regenerated** so only clean runs enter evaluation. Metrics split into
Accuracy (task completion — deterministic; speech fidelity — audio LLM judge; faithfulness) and
Experience (turn-taking, conciseness, progression — LLM judges). Supports both cascaded and
speech-to-speech agents. [5]

**voice-lab (saharmor/voice-lab):** text-level only ("only supports the text part of a voice
agent"); JSON personas (mood, response_style), LLM-as-judge with JSON-defined custom metrics, cost
tracking across model variants; speech testing listed as future work. [6]

Sources: [1] https://docs.pipecat.ai/pipecat/fundamentals/evaluations/overview ·
[2] https://github.com/fixadev/fixa · [3] https://github.com/voicetestdev/voicetest ·
[4] https://voicetest.dev/ · [5] https://github.com/ServiceNow/eva ·
[6] https://github.com/saharmor/voice-lab

## 6. Technical write-ups: barge-in, time-to-first-audio, end-of-turn accuracy

**Latency definition & measurement:**

- The Voice AI primer (Daily) notes the ground truth method: **record the call, open the waveform,
  measure end-of-user-speech → start-of-agent-speech**; true voice-to-voice latency is hard
  programmatically because part of it lives inside the OS audio stack, so "most observability tools
  just measure time-to-first-(audio)-byte." [1]
- **TTFB vs TTFA**: streaming TTS first bytes are container metadata (WAV headers, Ogg pages), so
  **Time-To-First-Audio (first *playable* chunk) is the metric that correlates with perceived
  responsiveness**, not TTFB. [2]
- LiveKit decomposes per-turn latency into spans: end-of-utterance/turn-detection delay → STT
  final-transcript delay → LLM TTFT → tool-call time → TTS TTFB → network RTT, exposed as per-turn
  `e2e_latency` etc. on every ChatMessage via Agent Observability. [3]
- Hamming production percentiles (4M+ calls, cascaded + telephony): P50 1.5–1.7 s, P90 ~3 s,
  P95 ~5 s; component budgets STT 300–500 ms, LLM 400–800 ms, TTS 200–400 ms. [4]

**Barge-in:**

- Implementation guides converge on hard budgets: once barge-in fires, **TTS audio must stop within
  ~60 ms** or the agent "feels like it ignored the interruption"; testing = talk over the agent and
  measure TTS-stop + STT-restart time; track **true interruptions vs. false-positive stops from
  background noise** (target 95%+ detection accuracy). [5][6][7]

**End-of-turn detection evaluation (Deepgram, best methodology write-up found):**

- 100+ hours of real conversations with annotated, **forced-alignment-corrected** EoT timestamps
  (human labels were systematically late); renaming "End-of-Thought"→"End-of-Turn" raised annotator
  confidence 5/10→8.5/10.
- Key innovation: **evaluate EoT via sequence alignment (treat turn boundaries as tokens in the
  transcript, WER-style) instead of fixed time windows** — gave 3–5% absolute precision/recall
  changes across all models tested (incl. Pipecat Smart Turn, LiveKit EoU, AssemblyAI); a modified
  Levenshtein handles dropped turns.
- Evaluate in a **true streaming setting**, since premature EoT corrupts context for later
  predictions ("knock-on effects"); for interruption (start-of-turn) they measure STT
  speech-detection vs. groundtruth first-word timestamps (~100–200 ms, <1–2% FP rate for Flux). [8]
- Pipecat's Smart Turn ships an open benchmark harness (`benchmark.py`) and publishes accuracy on
  its `human_5_all` English set (~99%), with v3.2 cutting short-utterance misclassification
  40%. [9][10]

Sources: [1] https://voiceaiandvoiceagents.com/ ·
[2] https://gradium.ai/content/best-text-to-speech-api-voice-agents ·
[3] https://livekit.com/blog/understand-and-improve-agent-latency ·
[4] https://hamming.ai/resources/voice-agent-testing-guide ·
[5] https://futureagi.com/blog/voice-ai-barge-in-turn-taking-2026/ ·
[6] https://frejun.ai/test-voice-agent-latency-quality/ ·
[7] https://hamming.ai/resources/how-to-evaluate-voice-agents-2026 ·
[8] https://deepgram.com/learn/evaluating-end-of-turn-detection-models ·
[9] https://github.com/pipecat-ai/smart-turn/blob/main/benchmark.py ·
[10] https://www.daily.co/blog/smart-turn-v3-2-handling-noisy-environments-and-short-responses/

## Engineering patterns common across these tools

1. **The "bot-calls-bot" architecture is universal, and the synthetic caller is itself a full
   voice-agent pipeline.** fixa is the explicit blueprint (Pipecat test agent + Cartesia TTS +
   Deepgram STT + LLM persona); EVA uses an ElevenLabs agent; Cekura/Hamming/Coval describe the
   same shape. Nobody polls — the synthetic user runs a continuous full-duplex streaming pipeline
   with its own VAD, and turn-taking "falls out" of the same VAD/turn-detection machinery
   production agents use. voicebox's pipecat-child design is exactly this pattern.
2. **Two-tier testing is the consensus: text-level for CI speed, audio-level for realism.** Pipecat
   (`TranscriptionFrame` injection), LiveKit (`session.run(user_input=...)`, text-only by design),
   Vapi (chat vs. websocket modes), Retell (chat-only simulation) all push text simulation as the
   inner loop; full-audio runs are reserved for pre-release/regression because they're slow and
   cost call minutes. Audio-only behaviors (barge-in, latency, pronunciation) are explicitly
   flagged as untestable at text level.
3. **Transport is a ladder of fidelity, offered as options: direct/text → WebSocket → WebRTC
   room-join → SIP/PSTN.** Vendors let you pick how much of the real path to exercise; PSTN
   validates "the full voice and network path" (codecs, telephony jitter) while native WebRTC joins
   are faster and cheaper. Hamming even injects network impairment (jitter, packet loss) at the
   transport layer as a test dimension.
4. **Interruption testing is persona-parameterized, not separately scripted.** Coval exposes an
   "interruption rate" knob per persona; Cekura ships "Interrupter"/"Pauser" personalities; Hamming
   injects overlapping speech and mid-turn changes. The measured outcome is split into two metrics:
   detection (did the agent notice) and **stoppage timing** (how many ms until TTS halted — budget
   ~60 ms), plus the inverse check: **did the agent talk over the user** (Coval flags
   bot-interrupts-user from recordings).
5. **Scoring = LLM-judge over transcript + deterministic checks over tool calls +
   signal-processing over the recording.** Three evaluation planes everywhere: (a) rubric/intent
   LLM judges (Vapi rubrics, Retell 0–1 scores with reasoning, LiveKit `.judge()`, fixa
   `Evaluation` prompts); (b) deterministic assertions on tool-call names/args and task completion;
   (c) audio-derived metrics computed post-hoc from the recording (latency from waveform gaps,
   interruption/overlap, WPM, pitch, dead air). Coval and EVA add a fourth: an **audio-input LLM
   judge** that listens to the recording itself (stutter, speech fidelity).
6. **Latency is measured from the recording, per turn — and TTFA (first playable audio) beats
   TTFB.** Ground truth is end-of-user-speech → start-of-agent-audio in the mixed recording;
   per-component spans (EoU delay, STT final, LLM TTFT, TTS TTFB) are tracked for diagnosis
   (LiveKit/Langfuse traces), but the user-facing number is voice-to-voice. Implication for
   voicebox: write a mixed stereo WAV (user channel + bot channel) like EVA does — it makes
   latency, overlap, and dead air all computable offline from one artifact.
7. **The simulated user needs grounding and validation too.** Coval ingests the agent-under-test's
   own prompt so simulator+judge know expected behavior; EVA's validator checks the *simulator*
   stayed faithful to its goal and auto-regenerates broken runs; LiveKit's task_completion_judge
   grounds on the agent's own instructions. A flaky synthetic caller is the #1 source of false
   failures, so it gets its own QA loop.
8. **Tool mocking is part of the harness, not the agent.** Vapi scenario-level tool mocks, EVA's
   deterministic Tool Executor, LiveKit `unittest.mock` — deterministic tool responses are what
   make audio-level runs reproducible and error paths testable.
9. **Tests are first-class scenario objects: persona + goal + behavior + success criteria, runnable
   N times.** Every platform converged on the same schema (Cekura scenarios, Coval test cases,
   Retell's Identity/Goal/Personality prompt format, fixa's `Scenario`/`Agent` dataclasses), with
   repeat attempts (Vapi: up to 5) to absorb LLM nondeterminism, and CI triggers (GitHub Actions)
   on prompt/model changes. Production-call **replay** (Hamming) converts real failures into
   regression tests preserving original audio and timing.
10. **End-of-turn accuracy needs its own eval discipline.** Deepgram's findings transfer directly:
    evaluate EoT as sequence alignment against transcripts rather than fixed timing windows,
    force-align human timestamps before trusting them, and evaluate in a streaming setting because
    early EoT corrupts downstream context. For a tool like voicebox (VAD `stop_secs=1.0`), this is
    the literature to consult before reporting utterance-boundary timing as a metric.

**Most relevant references for an MCP-based tool like voicebox:** fixa
(https://github.com/fixadev/fixa) is the closest open-source analog (small Pipecat-based test
caller + LLM judge, ~same stack); ServiceNow EVA (https://github.com/ServiceNow/eva) for the
stereo-recording + simulator-validation + deterministic-tool-executor patterns; Hamming's LiveKit
room-join approach (https://hamming.ai/blog/how-to-test-voice-agents-built-with-livekit) as the
only documented "join the WebRTC session natively, no phone" commercial design — the same niche
voicebox occupies via the browser shim, except voicebox reaches apps whose WebRTC internals you
*can't* join natively.
