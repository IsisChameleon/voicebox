# STT / TTS speed — measurements and candidate levers

*2026-08-10. Investigation only — **no code changed**. This records what was measured on
one Linux CPU box, what the measurements rule out, and the ordered list of things still to
try. Picking up another day; read "Outstanding" first, then the measurements that justify
the ordering.*

---

## What "speed" means here — two independent axes

They are worth separating because the levers differ and one of them stopped mattering in D24.

**Axis A — decode/synthesis throughput.** How long Whisper takes per segment, how long
Kokoro takes per utterance.

**Axis B — perceived latency.** How long after the app bot stops talking a transcript
appears in `listen()`; how long after `speak()` the synthetic mic makes a sound.

Since D24, **slow STT no longer stalls the session**. Decode runs on a background worker
(`nonblocking_whisper_stt.py:195`) and no voicebox constant is sized against it
(CLAUDE.md, "the app bot's transcript is delivered on frame arrival"). A slow machine now
reports itself through `listen()`'s `transcription_lag_secs` and through a longer teardown
drain. So STT throughput buys *transcript freshness*, not *session liveness* — which lowers
its priority relative to TTS, where synthesis time is directly in front of every `speak()`.

---

## Machine under measurement

| | |
|---|---|
| CPU | Intel i7-10750H, 6 cores / 12 threads, AVX2 (no AVX-512) |
| RAM | 15 GB |
| GPU | NVIDIA GTX 1650, 4 GB — visible to WSL2 (`ctranslate2.get_cuda_device_count()` → `1`) |
| CUDA libs | **absent** — `ldconfig` shows no cuBLAS, no cuDNN |
| onnxruntime | 1.24.4, providers `['AzureExecutionProvider', 'CPUExecutionProvider']` — **no CUDA EP** |
| STT config today | `Systran/faster-distil-whisper-large-v3`, `device="cpu"`, `compute_type="int8"` (`agent.py:946-950`) |
| TTS config today | `kokoro-v1.0.onnx` (fp32), CPU EP, `voice_id="af_heart"` (`kokoro_tts.py:146-151`) |

Numbers below are from this box only. Everything here needs re-measuring on Apple Silicon
(MLX path) before it generalises.

---

## Measurements (2026-08-10)

Method: synthesize one 8.68 s utterance with the production Kokoro config, resample to
16 kHz, feed it to faster-whisper. Best of 2–3 warm runs each, model load excluded.
Script in the appendix.

### TTS

| Config | Audio | Synthesis | RTF |
|---|---|---|---|
| Kokoro fp32, CPU EP (current) | 8.68 s | **3.83 s** | **0.44×** |

### STT

| Model (int8, CPU) | Params | Decode | RTF | Transcript |
|---|---|---|---|---|
| `faster-distil-whisper-large-v3` (current) | pipecat defaults | 9.88 s | **1.14×** | correct |
| `faster-distil-whisper-large-v3` | `beam_size=1`, `condition_on_previous_text=False` | 9.46 s | 1.09× | correct |
| `faster-distil-whisper-medium.en` | pipecat defaults | **5.33 s** | **0.61×** | correct, identical content |
| `faster-distil-whisper-medium.en` | `beam_size=1`, `condition_on_previous_text=False` | 5.50 s | 0.63× | correct |

Two things fall out of this table.

**1. The current STT config decodes slower than real time (1.14×).** On a long app-bot turn
the backlog grows without bound while the bot is talking; it only drains during silence.
That is exactly the condition `transcription_lag_secs` was built to report.

**2. Decode-parameter tuning is a dead end — measured, not assumed.** The standard advice
(`beam_size=5` → `1`) bought **4 %**, i.e. noise. The likely reason is that distil-whisper
has a 2-layer decoder while the encoder always runs over a zero-padded 30 s mel window, so
beam width barely touches the dominant cost. *The encoder-dominance explanation is a
hypothesis — the length-scaling run that would confirm it is in Outstanding below.* The
4 % number itself is measured and is enough to drop this lever regardless of the reason.

---

## Candidate levers, ranked

Status is either **measured** (a number in this doc backs it) or **unverified** (plausible,
no evidence yet — do not cite it as fact).

### L1 — Swap the STT model to `faster-distil-whisper-medium.en` · measured, 1.85×

`agent.py:947`. Already in the local HF cache, and it is pipecat's *own* default
(`whisper/stt.py`, `Model.DISTIL_MEDIUM_EN`). The tester only ever transcribes an English
app bot, so the multilingual large model is paying for capability voicebox does not use.
Moves the CPU path from 1.14× (slower than real time) to 0.61×.

Cost: one line. Risk: accuracy on accented / noisy app-bot audio was **not** tested — the
one clip measured was clean Kokoro speech, which is an easy case. See Outstanding T2.

### L2 — Kokoro quantized model variants · unverified

The release voicebox already downloads from ships `kokoro-v1.0.int8.onnx` and
`kokoro-v1.0.fp16.onnx` next to the fp32 file (confirmed present in the GitHub release
assets; **not** benchmarked). Swap point is `kokoro_tts.py:146` / the `KOKORO_MODEL_URL`
constant. This is the lever that acts directly on `speak()` latency.

### L3 — CUDA for faster-whisper · unverified, real setup cost

`ctranslate2` sees the GPU, but the CUDA libraries are not installed — which is exactly what
the pinned `device="cpu"` comment at `agent.py:942-945` says. Enabling it means installing
`nvidia-cublas-cu12` + `nvidia-cudnn-cu12` and setting `LD_LIBRARY_PATH`. 4 GB VRAM should
fit distil-large-v3 in `float16` / `int8_float16`.

Biggest potential win *and* the only lever that makes the deployment story worse — it adds a
host requirement to a project whose selling point is "local models, no API keys, it just
runs". If it lands it should be opt-in with clean CPU fallback, never auto-detected.

### L4 — `cpu_threads` on `WhisperModel` · unverified

`WhisperModel.__init__` takes `cpu_threads` (default `0` → ctranslate2's own default) and
pipecat's `_load()` never sets it (`whisper/stt.py`). With 6 physical cores there may be
headroom. Note the injection point already exists: `EagerSegmentsWhisperModel`
(`nonblocking_whisper_stt.py:65`) already wraps the model, and `_NonBlockingWhisperSTTService`
already reaches into `self._model` (`agent.py:146`).

### L5 — TTS time-to-first-audio is a design choice, not a speed problem · measured context

`run_tts` buffers the **entire** utterance before yielding any audio
(`kokoro_tts.py:204-209`, Task G). So time-to-first-audio ≈ full synthesis ≈ 0.44 × the
utterance length — ~3.8 s for the 20-word test sentence. Halving Kokoro's RTF only halves
that.

The structural fix is to pipeline with a lead-time watermark: start playing chunk 1 once the
remaining synthesis is provably ahead of playout. That preserves the gap-free property Task G
was introduced to guarantee (the per-chunk yield it replaced produced 1.6–4.2 s of real
silence in the synthetic mic). **Design work, not a config change** — needs its own spec if
we take it.

Note also `kokoro-onnx`'s GPU auto-detection is broken: it calls
`find_spec("onnxruntime-gpu")` (`kokoro_onnx/__init__.py:40-42`), which is not a valid module
name and always returns `None`. Installing `onnxruntime-gpu` alone would therefore change
nothing; the only working switch is the `ONNX_PROVIDER` env var (`:45-48`).

### L6 — `VAD_STOP_SECS = 1.0` · out of scope here, noted for completeness

`agent.py:100` puts a fixed 1 s floor under every utterance-end timestamp. That is axis B,
not throughput, and CLAUDE.md records that 0.2 s was tried and chopped sentences into
single-word transcripts. Not a speed lever; listed so nobody re-derives it.

---

## Outstanding

Ordered by dependency. Each has a verify step; none has been started.

**T1 — Confirm or kill the encoder-dominance hypothesis.**
Time the same model against 2 s / 8.7 s / 26 s clips. If a 2 s clip costs about what an 8.7 s
clip costs, cost is per-30 s-window and the only STT lever that matters is model size — which
would close off a whole family of micro-optimisations. *Verify:* three timings in one table.

**T2 — Accuracy check before adopting L1.**
The 1.85× win is only real if `medium.en` holds up on harder audio than clean Kokoro speech.
Run both models over recorded app-bot WAVs from a real dogfood session (`record_dir` output,
`temp/`) and diff the transcripts. *Verify:* per-utterance diff; adopt only if no material
regression.

**T3 — Land L1 if T2 passes.** One line at `agent.py:947`. *Verify:* `uv run pytest -q`,
plus one live session showing `transcription_lag_secs` staying near 0 where it previously grew.

**T4 — Benchmark L2 (Kokoro int8 / fp16).**
Download both variants, measure RTF and listen to the output — quantization artifacts in TTS
are audible in a way a number will not show. *Verify:* RTF table + a listening pass on the
same sentence at all three precisions.

**T5 — Measure L4 (`cpu_threads=6` vs default).** Cheap; fold into the T1 run.

**T6 — Spike L3 (CUDA) only if T1–T5 leave a gap.** Time-boxed. If it works, the open design
question is how it gets selected — see Decisions below.

**T7 — Re-measure the whole table on Apple Silicon / MLX.** Every number here is from one
Linux CPU box; the MLX path (`_NonBlockingWhisperSTTServiceMLX`, `agent.py:939-941`) has not
been measured at all.

---

## Decisions still open

**D-a — Is STT throughput worth optimising at all, given D24?** Slow STT is now visible
(`transcription_lag_secs`) rather than fatal. The case for L1 is that it is a one-line change
with a measured 1.85×; the case against doing anything *beyond* L1 is that transcript freshness
may simply not be a problem worth a dependency. Decide before spending on L3.

**D-b — If CUDA lands, how is it selected?** Auto-detect is what the current comment warns
against (`agent.py:942-945`): it picks CUDA whenever a GPU is visible and then fails on a host
without the libraries. Opt-in env var with CPU fallback is the obvious shape, but it is a
config-surface decision and belongs in a BUILDLOG entry when made.

**D-c — Does L5 (pipelined TTS playout) get its own spec?** It is the only lever that
materially improves `speak()` latency, and it is the only one that can regress gap-free
playout — the failure Task G exists to prevent.

---

## Not being done

* Decode-parameter tuning (`beam_size`, `condition_on_previous_text`, temperature fallback) —
  measured at 4 %, dropped.
* Parallel STT workers. The single ordered worker is deliberate
  (`nonblocking_whisper_stt.py:112`): segments must stay in spoken order, and on a CPU box two
  concurrent decodes only contend for the same cores.
* Touching `VAD_STOP_SECS` — see L6.

---

## Appendix — benchmark method

Reproduces the tables above. Read-only; writes nothing into the repo. Run with
`.venv/bin/python`. (Original run lived in the session scratchpad, not committed.)

```python
import time
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel
from kokoro_onnx import Kokoro

CACHE = Path.home() / ".cache/kokoro-onnx"
TEXT = ("Hi, I'd like to book a table for four people on Friday evening, "
        "somewhere around seven o'clock if that works, and we have one vegetarian in the group.")

k = Kokoro(str(CACHE / "kokoro-v1.0.onnx"), str(CACHE / "voices-v1.0.bin"))
k.create("Ready.", voice="af_heart", lang="en-us")          # warm-up (the ~5 s cold ONNX cost)
t0 = time.perf_counter()
samples, sr = k.create(TEXT, voice="af_heart", lang="en-us")
print(f"TTS {time.perf_counter() - t0:.2f}s for {len(samples) / sr:.2f}s audio")

n = int(len(samples) * 16000 / sr)                           # 16 kHz is what Whisper assumes
audio = np.interp(np.linspace(0, len(samples) - 1, n),
                  np.arange(len(samples)), samples).astype(np.float32)

for model in ("Systran/faster-distil-whisper-large-v3",
              "Systran/faster-distil-whisper-medium.en"):
    m = WhisperModel(model, device="cpu", compute_type="int8")
    list(m.transcribe(audio, language="en")[0])              # warm-up
    t0 = time.perf_counter()
    segs, _ = m.transcribe(audio, language="en")             # pipecat's exact call shape
    text = " ".join(s.text for s in segs)
    print(f"STT {model} {time.perf_counter() - t0:.2f}s -> {text[:80]}")
```

`m.transcribe(audio, language=...)` with no other kwargs is what pipecat actually calls
(`whisper/stt.py`, `run_stt`), so the "pipecat defaults" rows are faithful: `beam_size=5`,
`best_of=5`, `condition_on_previous_text=True`, temperature fallback `[0.0 … 1.0]`.
