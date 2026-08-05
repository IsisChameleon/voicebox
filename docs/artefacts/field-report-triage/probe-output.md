# Probe output — captured 2026-07-29

Reproduce with `uv run python docs/artefacts/field-report-triage/<script>.py` from the repo root.
Evidence for [../../specs/2026-07-29-field-report-triage.md](../../specs/2026-07-29-field-report-triage.md).

## 1. `probe_stt_blocking.py` — a slow `run_stt` stalls every later frame

Pipeline `[SlowSTT] -> [Probe]`, `run_stt` sleeps 5 s.

```
[  0.00] STT: run_stt START (32044 bytes)
[  0.00] PROBE saw VADUserStartedSpeakingFrame#0
[  0.30] speak(): queueing LLM triplet
[  0.30] speak(): queue_frames returned
[  0.50] VAD frame constructed, timestamp=0.50
[  5.00] STT: run_stt DONE
[  5.01] PROBE saw VADUserStartedSpeakingFrame#1
[  5.01] PROBE saw TranscriptionFrame#0
[  5.01] PROBE saw LLMTextFrame#0
```

* The `speak()` frame triplet queued at 0.30 s reached the downstream processor at 5.01 s —
  **4.7 s late**, exactly the STT stall.
* A `VADUserStartedSpeakingFrame` **constructed at 0.50 s** (its `timestamp` field) was observed
  downstream at 5.01 s — a 4.5 s gap between the value that becomes the event's `t` and the moment
  the event is appended to the log.
* `SystemFrame`s are stalled too: `run_stt` is awaited from
  `FrameProcessor.__input_frame_task_handler`, which is the task that handles system frames inline.

## 2. `probe_audio_trim.py` — audio arriving during the stall is discarded

Same pipeline; a 2 s segment triggers the stall, then 10 s of speech arrives while it is stalled,
with the `VADUserStartedSpeakingFrame` arriving *after* the audio (voicebox's topology, where the
VAD lives downstream of the STT).

```
[  0.00] run_stt got 2.00s of audio
[  5.90] run_stt got 1.00s of audio

segment 1 fed 2.0s of speech  -> run_stt saw 2.00s
segment 2 fed 10.0s of speech -> run_stt saw 1.00s
AUDIO LOST FROM SEGMENT 2: 9.00s (90%)
```

## 3. `probe_vad_upstream.py` — moving the VAD upstream of the STT fixes it

Identical, except the `VADUserStartedSpeakingFrame` is queued *ahead of* the audio, as a transport-
level VAD would produce it.

```
[  0.00] run_stt got 2.00s of audio
[  5.90] run_stt got 10.00s of audio

segment 1 fed 2.0s of speech  -> run_stt saw 2.00s
segment 2 fed 10.0s of speech -> run_stt saw 10.00s
AUDIO LOST FROM SEGMENT 2 (VAD upstream of STT): 0.00s (0%)
```

`FrameProcessorQueue` gives `SystemFrame`s priority, so the queued VAD start frame is dequeued
before the backlogged audio and `_user_speaking` is `True` before any of it is appended.

## 4. `bench_whisper.py` — Whisper CPU int8 throughput

voicebox's exact settings (`Systran/faster-distil-whisper-large-v3`, `device="cpu"`,
`compute_type="int8"`), transcribing `ember_voice.wav` from the 12:10:27 session.

```
source: 48000 Hz, 217.8s
model load: 13.5s
audio= 10s  transcribe=  12.4s  ratio=1.24x realtime   (cold)
audio= 30s  transcribe=  12.1s  ratio=0.40x realtime
audio= 60s  transcribe=  23.7s  ratio=0.40x realtime
```

Warm throughput is **2.5× faster than realtime**, so raw Whisper speed is not the bottleneck.
The one-off costs are the 13.5 s model load and a ~12 s first inference.
