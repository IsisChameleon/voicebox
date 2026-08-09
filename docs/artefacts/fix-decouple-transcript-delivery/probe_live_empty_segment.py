"""Live probe: can a real Whisper segment come back with no text at all?

Companion to probe_live_transcript_delivery.py. That one shows the spoken path;
this one goes after the silent one — the Phase 2 `on_empty_segment` signal.

To exercise it live we need audio that Silero calls speech (so a segment is cut)
but Whisper finds no words in (so `run_stt` yields nothing —
pipecat/services/whisper/stt.py:377, `if text:`). Three candidates are streamed
through ONE pipecat session, and the probe reports what each produced. The
result is recorded whether or not any of them works.

Run: uv run python docs/artefacts/fix-decouple-transcript-delivery/probe_live_empty_segment.py
"""

import asyncio
import json
import sys
import time

import numpy as np
from loguru import logger

sys.path.insert(0, "docs/artefacts/fix-decouple-transcript-delivery")
from probe_live_transcript_delivery import (  # noqa: E402
    AUDIO_PORT,
    CHUNK_SECS,
    START_SECS,
    TAP_RATE,
    WAV,
    connect_when_ready,
    load_tap_pcm,
)

logger.remove()
logger.add(sys.stderr, level="INFO")

CLIP_SECS = 4.0
TRAILING_SILENCE_SECS = 3.0  # > VAD_STOP_SECS (1.0), so the segment closes
PER_CLIP_WAIT = 45.0


def _with_silence(samples: np.ndarray) -> bytes:
    silence = np.zeros(int(TAP_RATE * TRAILING_SILENCE_SECS), dtype=np.int16)
    return np.concatenate([samples, silence]).tobytes()


def speech_band_noise() -> bytes:
    """Noise shaped toward the speech band, with a slow amplitude wobble."""
    rng = np.random.default_rng(7)
    n = int(TAP_RATE * CLIP_SECS)
    t = np.arange(n) / TAP_RATE
    noise = rng.normal(0, 1, n)
    band = np.convolve(noise, np.ones(5) / 5, mode="same") - np.convolve(
        noise, np.ones(40) / 40, mode="same"
    )
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)
    return _with_silence((band * envelope * 9000).clip(-32000, 32000).astype(np.int16))


def reversed_speech() -> bytes:
    """Real app-bot speech played backwards: speech-like, word-free."""
    forward = np.frombuffer(load_tap_pcm(WAV, CLIP_SECS, START_SECS + 3.0), dtype=np.int16)
    return _with_silence(forward[::-1])


def voiced_buzz() -> bytes:
    """A 120 Hz sawtooth gated at syllable rate: harmonics, no formants."""
    n = int(TAP_RATE * CLIP_SECS)
    t = np.arange(n) / TAP_RATE
    buzz = 2.0 * (t * 120.0 % 1.0) - 1.0
    gate = (np.sin(2 * np.pi * 4.0 * t) > -0.3).astype(np.float32)
    return _with_silence((buzz * gate * 11000).astype(np.int16))


CANDIDATES = [
    ("speech-band noise", speech_band_noise),
    ("reversed real speech", reversed_speech),
    ("voiced buzz (120 Hz sawtooth)", voiced_buzz),
]


async def send_clip(ws, pcm: bytes):
    """Feed one clip at wall-clock speed."""
    chunk = int(TAP_RATE * CHUNK_SECS) * 2
    started = time.monotonic()
    for i in range(0, len(pcm), chunk):
        await ws.send(pcm[i : i + chunk])
        due = started + (i + chunk) / (TAP_RATE * 2)
        await asyncio.sleep(max(0.0, due - time.monotonic()))


async def drain_events(send_command, cursor: int, secs: float) -> tuple[list, int]:
    """Collect whatever events arrive over the next `secs`."""
    out, deadline = [], time.time() + secs
    while time.time() < deadline:
        envelope = await send_command(
            "listen", timeout=min(10, max(1, deadline - time.time())), cursor=cursor, deadline=30.0
        )
        cursor = envelope["cursor"]
        out.extend(envelope["events"])
    return out, cursor


async def main():
    """Stream each candidate through one session and report what it produced."""
    from voicebox.agent_ipc import send_command, start_pipecat_process, stop_pipecat_process
    from voicebox.runner_args import BrowserShimRunnerArguments

    clips = [(name, make()) for name, make in CANDIDATES]

    logger.info("=== starting the real pipecat child ===")
    start_pipecat_process(BrowserShimRunnerArguments(host="localhost", port=AUDIO_PORT))
    await asyncio.sleep(5)

    ws = await connect_when_ready()
    results, cursor, all_events = [], 0, []
    _, cursor = await drain_events(send_command, cursor, 2.0)  # session_started etc.

    for name, pcm in clips:
        logger.info(f"=== streaming: {name} ===")
        await send_clip(ws, pcm)
        events, cursor = await drain_events(send_command, cursor, PER_CLIP_WAIT)
        all_events.extend(events)
        vad = any(e["type"] == "app_bot_speech_started" for e in events)
        transcripts = [e for e in events if e["type"] == "app_bot_transcript"]
        results.append((name, vad, transcripts))
        logger.info(f"  vad_said_speech={vad} transcripts={len(transcripts)}")

    await ws.close()
    await send_command("stop", deadline=240.0)
    stop_pipecat_process()

    print("\n=== EVENT LOG ===")
    print(json.dumps(all_events, indent=2))

    print("\n=== RESULT ===")
    print(f"{'candidate':32} {'VAD=speech':>11} {'transcripts':>12}  text / empty flag")
    for name, vad, transcripts in results:
        detail = "—"
        if transcripts:
            t = transcripts[0]
            detail = f"empty={t['transcription_empty']} text={t['text']!r}"
        print(f"{name:32} {str(vad):>11} {len(transcripts):>12}  {detail}")

    empty = [t for _, _, ts in results for t in ts if t["transcription_empty"]]
    print(f"\nempty-flagged transcripts observed: {len(empty)}")


if __name__ == "__main__":
    asyncio.run(main())
