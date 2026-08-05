"""Measure faster-whisper CPU int8 throughput with voicebox's exact settings."""
import time, wave, numpy as np
from faster_whisper import WhisperModel

SRC = "/home/isischameleon/src/readme/temp/test-session-20260728-121027/ember_voice.wav"
with wave.open(SRC) as w:
    sr, n = w.getframerate(), w.getnframes()
    print(f"source: {sr} Hz, {n/sr:.1f}s")
    raw = w.readframes(n)

pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
# voicebox feeds whisper 16 kHz mono; the wav is 48 kHz -> decimate by 3
pcm16k = pcm[::3]

t0 = time.time()
model = WhisperModel("Systran/faster-distil-whisper-large-v3", device="cpu", compute_type="int8")
print(f"model load: {time.time()-t0:.1f}s")

for secs in (10, 30, 60):
    chunk = pcm16k[: secs * 16000]
    t0 = time.time()
    segments, _ = model.transcribe(chunk, language="en")
    text = " ".join(s.text for s in segments)
    dt = time.time() - t0
    print(f"audio={secs:3d}s  transcribe={dt:6.1f}s  ratio={dt/secs:4.2f}x realtime")
    print(f"   -> {text[:120]!r}")
