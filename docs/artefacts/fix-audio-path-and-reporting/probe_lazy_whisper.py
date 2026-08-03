"""Probe: faster-whisper lazy decode vs eager-in-thread decode, on real speech.

Phase 1: find a speech-dense 40 s slice of ember_voice.wav by RMS energy.
Phase 2 (pipecat's shape): to_thread(transcribe) then iterate on the loop.
Phase 3 (fix's shape): materialize the segments inside to_thread.
A heartbeat task measures the worst event-loop stall in each phase (with an
await before reading, so a starved heartbeat gets one wake-up to record it).
"""

import asyncio
import time
import wave

import numpy as np
from faster_whisper import WhisperModel

WAV = "/home/isischameleon/src/voicebox/temp/verify-round-1/ember_voice.wav"
SLICE_SECS = 40


class Heartbeat:
    def __init__(self):
        self.max_gap = 0.0
        self._task = None

    async def _run(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            self.max_gap = max(self.max_gap, now - last)
            last = now

    def start(self):
        self.max_gap = 0.0
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> float:
        await asyncio.sleep(0.06)  # let a starved heartbeat wake once and record the gap
        self._task.cancel()
        return self.max_gap


def load_speech_slice() -> np.ndarray:
    with wave.open(WAV, "rb") as w:
        rate, ch = w.getframerate(), w.getnchannels()
        n = w.getnframes()
        raw = w.readframes(n)
    pcm = np.frombuffer(raw, dtype=np.int16)
    if ch == 2:
        pcm = pcm[::2]
    win = rate  # 1 s windows
    n_win = len(pcm) // win
    rms = np.sqrt(np.mean(np.square(pcm[: n_win * win].astype(np.float64)).reshape(n_win, win), axis=1))
    # densest 40 s window of energy
    kernel = np.ones(SLICE_SECS)
    density = np.convolve(rms, kernel, mode="valid")
    start_s = int(np.argmax(density))
    print(f"speech-densest {SLICE_SECS}s slice starts at {start_s}s (mean RMS {density.max()/SLICE_SECS:.0f})")
    seg = pcm[start_s * rate : (start_s + SLICE_SECS) * rate]
    return (seg[:: rate // 16000]).astype(np.float32) / 32768.0


async def main():
    audio = load_speech_slice()
    t0 = time.monotonic()
    model = WhisperModel("Systran/faster-distil-whisper-large-v3", device="cpu", compute_type="int8")
    print(f"model load: {time.monotonic() - t0:.2f}s")

    hb = Heartbeat()

    # pipecat's shape: lazy call in thread, decode during on-loop iteration
    hb.start()
    t0 = time.monotonic()
    segments, _ = await asyncio.to_thread(model.transcribe, audio, language="en")
    call_secs = time.monotonic() - t0
    t0 = time.monotonic()
    texts = [s.text for s in segments]  # sync decode on the loop thread
    iter_secs = time.monotonic() - t0
    stall = await hb.stop()
    print(f"LAZY : call {call_secs:6.3f}s, iterate {iter_secs:6.3f}s, max loop stall {stall:6.3f}s")
    print(f"       text: {' '.join(t.strip() for t in texts)[:200]}")

    # the fix's shape: materialize inside the thread
    def eager():
        segs, info = model.transcribe(audio, language="en")
        return list(segs), info

    hb.start()
    t0 = time.monotonic()
    segs, _ = await asyncio.to_thread(eager)
    eager_secs = time.monotonic() - t0
    stall = await hb.stop()
    print(f"EAGER: to_thread {eager_secs:6.3f}s total, max loop stall {stall:6.3f}s")
    print(f"       text: {' '.join(s.text.strip() for s in segs)[:200]}")


asyncio.run(main())
