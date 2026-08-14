"""Nova — the fake voice app voicebox dogfoods and (later) evaluates itself against.

Pipeline assembly, runner entry, and the ground-truth observer. What it is and
how to run it: ``README.md`` beside this file; design:
``docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md``.

Run: ``uv run python tests/eval/fake_app/bot.py``.
"""

import json
import os
import time
from pathlib import Path

# Script mode only (`python tests/eval/fake_app/bot.py`): the script dir is
# sys.path[0], so the sibling module imports bare.
from brain import create_brain
from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

load_dotenv()

STT_MODEL = os.environ.get("VOICEBOX_FAKE_APP_STT_MODEL", "base")
PROMPT_PATH = Path(__file__).resolve().parent / "prompt.md"
# Repo convention: all run artifacts under temp/ (gitignored) — anchored to the
# repo root so any launch directory works.
GROUND_TRUTH_PATH = Path(__file__).resolve().parents[3] / "temp/fake_app/ground_truth.jsonl"


class GroundTruthObserver(BaseObserver):
    """Appends the app's own view of the conversation to a JSONL file on disk.

    One record per line: ``{"t": <epoch secs>, "event": ..., ...}`` for the
    bot's speech start/stop, what its Whisper heard from the tester, what the
    brain replied, and interruptions. Note pipecat broadcasts
    ``InterruptionFrame`` at every user turn start, so ``interrupted`` records
    carry ``during_bot_speech`` — only ``true`` ones are real barge-ins.
    Disk only, never exposed over the network — voicebox must not be able to
    read it mid-session.
    """

    _WATCHED = (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        TranscriptionFrame,
        LLMTextFrame,
        LLMFullResponseEndFrame,
        InterruptionFrame,
    )

    def __init__(self, path: Path, session_id: str | None):
        """Open ``path`` for line-buffered appending and record the session start."""
        super().__init__()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", buffering=1)
        self._seen_frame_ids: set[int] = set()
        self._reply_parts: list[str] = []
        self._bot_speaking = False
        self._write("session_started", session_id=session_id)

    def _write(self, event: str, **fields):
        self._file.write(json.dumps({"t": time.time(), "event": event, **fields}) + "\n")

    async def cleanup(self):
        """Record the session end and close the file.

        ``PipelineWorker._cleanup`` forwards ``cleanup()`` to every observer on
        teardown, so a missing ``session_ended`` line marks a crashed session.
        """
        await super().cleanup()
        self._write("session_ended")
        self._file.close()

    async def on_push_frame(self, data: FramePushed):
        """Record each watched frame once, at its first downstream hop."""
        frame = data.frame
        if data.direction != FrameDirection.DOWNSTREAM or not isinstance(frame, self._WATCHED):
            return
        if frame.id in self._seen_frame_ids:
            return
        self._seen_frame_ids.add(frame.id)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._write("bot_speech_started")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._write("bot_speech_stopped")
        elif isinstance(frame, TranscriptionFrame):
            self._write("heard_user", text=frame.text)
        elif isinstance(frame, LLMTextFrame):
            self._reply_parts.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._write("brain_reply", text="".join(self._reply_parts))
            self._reply_parts = []
        elif isinstance(frame, InterruptionFrame):
            if self._reply_parts:
                # A streaming reply was cut mid-generation: record what was
                # produced so the parts don't leak into the next reply.
                self._write("brain_reply", text="".join(self._reply_parts), interrupted=True)
                self._reply_parts = []
            self._write("interrupted", during_bot_speech=self._bot_speaking)


async def bot(runner_args: RunnerArguments):
    """Run one Nova session on an incoming WebRTC connection (runner entry point)."""
    if not isinstance(runner_args, SmallWebRTCRunnerArguments):
        raise ValueError(f"Unsupported runner_args type: {type(runner_args).__name__}")

    transport = SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    context = LLMContext(
        messages=[{"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")}]
    )
    aggregators = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=SileroVADAnalyzer()),
            WhisperSTTService(
                settings=WhisperSTTService.Settings(model=STT_MODEL),
                # "auto" picks CUDA whenever ctranslate2 sees a GPU; on machines
                # without CUDA libs (e.g. WSL2) that dies with "libcublas.so.12
                # not found" on the first segment. CPU is the portable choice.
                device="cpu",
            ),
            aggregators.user(),
            create_brain(),
            KokoroTTSService(settings=KokoroTTSService.Settings(voice="am_michael")),
            transport.output(),
            aggregators.assistant(),
        ]
    )
    worker = PipelineWorker(
        pipeline,
        observers=[GroundTruthObserver(GROUND_TRUTH_PATH, runner_args.session_id)],
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected — kicking off Nova's greeting")
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(
        handle_sigint=runner_args.handle_sigint,
        handle_sigterm=runner_args.handle_sigterm,
    )
    await runner.add_workers(worker)
    await runner.run()


if __name__ == "__main__":
    from pipecat.runner.run import main

    # Fail fast on an unknown provider / missing API key, before serving —
    # otherwise the error only surfaces when a client connects.
    create_brain()
    main()
