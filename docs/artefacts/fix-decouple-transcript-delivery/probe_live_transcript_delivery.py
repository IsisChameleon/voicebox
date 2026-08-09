"""Live probe: the app bot's transcript reaches listen() without a turn timer.

Runs the REAL pipecat child (real Silero VAD, real faster-whisper) and plays the
part of the browser shim directly: a raw WebSocket client on :9091 streaming
16 kHz PCM in real time, which is exactly what shim.js sends. No browser and no
voice app are involved, so this exercises Phases 1-3 of
docs/specs/2026-08-06-decouple-transcripts-from-turn-closure.md on any machine.

Input is a real recording of the app bot from an earlier dogfood round
(temp/verify-round-7b/ember_voice.wav, 48 kHz mono), decimated to 16 kHz.

Checks, in order:
  1. app_bot_speech_started/stopped arrive from the VAD.
  2. app_bot_transcript arrives with text, WITHOUT any turn-stop timer having
     to expire, and is stamped with the VAD start rather than arrival time.
  3. the whole event log and the measured decode lag are printed, so the
     artefact records real numbers rather than assertions about them.

Run: uv run python docs/artefacts/fix-decouple-transcript-delivery/probe_live_transcript_delivery.py
"""

import asyncio
import json
import sys
import time
import wave
from datetime import datetime

import numpy as np
import websockets
from loguru import logger
from scipy.signal import resample_poly

logger.remove()
logger.add(sys.stderr, level="INFO")

# Round 7b's ember_voice.wav is 20 s of pure silence (measured RMS 0/s); round
# 6's carries the app bot actually talking, from ~7 s in.
WAV = "temp/verify-round-6/ember_voice.wav"
START_SECS = 7.0
AUDIO_PORT = 9091
TAP_RATE = 16000
CHUNK_SECS = 0.02  # what the shim's ScriptProcessor sends per callback
PLAY_SECS = 20.0  # how much of the recording to stream
SETTLE_SECS = 180.0  # generous ceiling; the probe stops as soon as text lands


def load_tap_pcm(path: str, secs: float, start: float = 0.0) -> bytes:
    """Read the recording and return `secs` of 16 kHz 16-bit mono PCM."""
    with wave.open(path) as w:
        rate = w.getframerate()
        w.setpos(int(rate * start))
        frames = w.readframes(int(rate * secs))
    samples = np.frombuffer(frames, dtype=np.int16)
    if rate != TAP_RATE:
        samples = resample_poly(samples, TAP_RATE, rate).astype(np.int16)
    return samples.tobytes()


async def connect_when_ready(timeout: float = 120.0):
    """Wait for pipecat to bind :9091 — model load makes that take ~20 s."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return await websockets.connect(f"ws://localhost:{AUDIO_PORT}")
        except OSError as e:
            if time.monotonic() > deadline:
                raise
            logger.debug(f"tap waiting for the WS server ({e})")
            await asyncio.sleep(1.0)


async def stream_to_pipecat(pcm: bytes):
    """Feed the PCM to pipecat's WS server at wall-clock speed, like the shim."""
    chunk = int(TAP_RATE * CHUNK_SECS) * 2
    async with await connect_when_ready() as ws:
        logger.info(f"tap connected; streaming {len(pcm) / (TAP_RATE * 2):.1f}s of speech")
        started = time.monotonic()
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i : i + chunk])
            due = started + (i + chunk) / (TAP_RATE * 2)
            await asyncio.sleep(max(0.0, due - time.monotonic()))
        logger.info("stream finished; holding the socket open while Whisper decodes")
        await asyncio.sleep(SETTLE_SECS)


async def main():
    """Run the probe and print the event log it produced."""
    from voicebox.agent_ipc import send_command, start_pipecat_process, stop_pipecat_process
    from voicebox.runner_args import BrowserShimRunnerArguments

    pcm = load_tap_pcm(WAV, PLAY_SECS, START_SECS)

    logger.info("=== starting the real pipecat child (Whisper + Silero load here) ===")
    start_pipecat_process(BrowserShimRunnerArguments(host="localhost", port=AUDIO_PORT))
    await asyncio.sleep(5)

    tap = asyncio.create_task(stream_to_pipecat(pcm))
    speech_started_at = time.time()

    events, cursor, deadline = [], 0, time.time() + SETTLE_SECS
    transcript = None
    while time.time() < deadline and transcript is None:
        if tap.done() and not tap.cancelled() and tap.exception() is not None:
            logger.error(f"tap died: {tap.exception()!r}")
            break
        envelope = await send_command("listen", timeout=10, cursor=cursor, deadline=30.0)
        cursor = envelope["cursor"]
        for e in envelope["events"]:
            events.append(e)
            logger.info(f"  event {e['type']:26} t=+{e['t'] - speech_started_at:7.2f}s")
            if e["type"] == "app_bot_transcript":
                transcript = e
        if envelope["events"]:
            logger.info(f"  (transcription_lag_secs={envelope['transcription_lag_secs']})")

    tap.cancel()
    await send_command("stop", deadline=240.0)
    stop_pipecat_process()

    print("\n=== EVENT LOG ===")
    print(json.dumps(events, indent=2))

    if transcript is None:
        print("\n✗ no app_bot_transcript arrived")
        sys.exit(1)

    vad_start = next(e for e in events if e["type"] == "app_bot_speech_started")
    claimed = datetime.fromisoformat(transcript["turn_started_at"]).timestamp()
    print("\n=== RESULT ===")
    print(f"transcript text          : {transcript['text']!r}")
    print(f"transcription_empty      : {transcript['transcription_empty']}")
    print(f"VAD start t              : +{vad_start['t'] - speech_started_at:.2f}s")
    print(f"turn_started_at (claimed): +{claimed - speech_started_at:.2f}s")
    print(f"transcript arrived at t  : +{transcript['t'] - speech_started_at:.2f}s")
    print(f"decode lag (arrival-VAD) : {transcript['t'] - vad_start['t']:.2f}s")
    print(f"claimed vs VAD start     : {abs(claimed - vad_start['t']):.3f}s  (must be ~0)")


if __name__ == "__main__":
    asyncio.run(main())
