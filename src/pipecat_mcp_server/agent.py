#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pipecat MCP Agent for voice I/O over MCP protocol.

This module provides the `PipecatMCPAgent` class that exposes voice input/output
capabilities through MCP tools. It manages a Pipecat pipeline with STT and TTS
services, allowing an MCP client to listen for user speech and speak responses.
"""

import asyncio
import sys
from typing import Any, Optional

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.runner.types import DailyRunnerArguments, RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.services.whisper.stt import WhisperSTTService, WhisperSTTServiceMLX
from pipecat.transports.base_transport import BaseTransport

from pipecat_mcp_server.raw_pcm_serializer import RawPCMSerializer
from pipecat_mcp_server.runner_args import BrowserShimRunnerArguments
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from pipecat_mcp_server.processors.kokoro_tts import KokoroTTSService
from pipecat_mcp_server.processors.screen_capture import ScreenCaptureProcessor
from pipecat_mcp_server.processors.vision import VisionProcessor

load_dotenv(override=True)


class PipecatMCPAgent:
    """Pipecat MCP Agent that exposes voice I/O tools.

    Tools:
    - listen(): Wait for user speech and return transcription
    - speak(text): Speak text to the user via TTS
    """

    def __init__(self, transport: BaseTransport, record_dir: Optional[str] = None):
        """Initialize the Pipecat MCP Agent.

        Args:
            transport: Daily/WebSocket transport for audio I/O.
            record_dir: If set, audio is buffered via AudioBufferProcessor and
                ``stop()`` writes user/bot/merged WAVs into this directory.

        """
        self._transport = transport
        self._record_dir = record_dir
        self._audio_buffer = None  # type: ignore[assignment]

        self._task: Optional[asyncio.Task] = None
        self._pipeline_task: Optional[PipelineTask] = None
        self._pipeline_runner: Optional[PipelineRunner] = None
        self._user_speech_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._connected = asyncio.Event()

        self._started = False

    async def start(self):
        """Start the voice pipeline.

        Initializes STT and TTS services, creates the processing pipeline,
        and starts it in the background. The pipeline remains active until
        `stop()` is called.

        Raises:
            ValueError: If required API keys are missing from environment.

        """
        if self._started:
            return

        logger.info("Starting Pipecat MCP Agent pipeline...")

        # Create services
        stt = self._create_stt_service()
        tts = self._create_tts_service()

        context = LLMContext()
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    stop=[
                        TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())
                    ]
                ),
                # stop_secs gates "user turn ended". For the Toocan flow that
                # was 0.2 s (bot's TTS-clean audio). In browser-shim mode the
                # other side is a real human or a TTS bot whose audio comes
                # via WebRTC with natural pauses — 0.2 s would chop Ember
                # mid-sentence and we'd get single-word transcripts. 1.0 s
                # is the sweet spot for capturing complete utterances.
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=1.0)),
            ),
        )

        self._screen_capture = ScreenCaptureProcessor()
        self._vision = VisionProcessor()

        if self._record_dir:
            from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

            self._audio_buffer = AudioBufferProcessor(
                sample_rate=48000,
                num_channels=1,
                buffer_size=0,  # accumulate everything
            )

        # Create pipeline with parallel branches:
        # - Main branch: audio processing (STT → aggregator → TTS)
        # - Vision branch: saves frames to disk on demand
        stages = [
            self._transport.input(),
            self._screen_capture,
            ParallelPipeline(
                [stt, user_aggregator, tts],
                [self._vision],
            ),
            # Assistant aggregator before the transport, because we want to
            # keep everyting from the client.
            assistant_aggregator,
            self._transport.output(),
        ]
        # AudioBufferProcessor must observe both InputAudioRawFrame (which
        # flows through the whole pipeline from transport.input downstream)
        # and OutputAudioRawFrame (emitted by TTS inside the ParallelPipeline,
        # then routed to transport.output). Placing it at the END of the
        # pipeline catches both as they continue downstream past the output —
        # neither is destructively consumed by transport.output.
        if self._audio_buffer:
            stages.append(self._audio_buffer)
        pipeline = Pipeline(stages)

        # enable_rtvi=False: we are a headless synthetic user, not an RTVI
        # client. Transcripts reach Claude via the listen() MCP tool's return
        # value, not via data-channel notifications. Leaving RTVI on creates a
        # validation-error storm with the bot (each side emits events without
        # an `id` field and rejects the other's events under RTVIMessage,
        # ~85 errors/sec — and the resulting event-loop load delays our
        # SmartTurn end-of-turn detection by ~100s).
        self._pipeline_task = PipelineTask(
            pipeline,
            cancel_on_idle_timeout=False,
            enable_rtvi=False,
        )

        self._pipeline_runner = PipelineRunner(handle_sigterm=True)

        @self._transport.event_handler("on_client_connected")
        async def on_connected(transport, client):
            logger.info(f"Client connected")
            self._connected.set()

        @self._transport.event_handler("on_client_disconnected")
        async def on_disconnected(transport, client):
            logger.info(f"Client disconnected")
            if not self._pipeline_task:
                return
            await self._user_speech_queue.put("I just disconnected, but I might come back.")

        @user_aggregator.event_handler("on_user_turn_stopped")
        async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):
            if message.content:
                await self._user_speech_queue.put(message.content)

        # Start pipeline in background
        self._task = asyncio.create_task(self._pipeline_runner.run(self._pipeline_task))

        if self._audio_buffer is not None:
            async def _start_recording():
                await self._connected.wait()
                await self._audio_buffer.start_recording()
                logger.info("Audio recording started")
            asyncio.create_task(_start_recording())

        self._started = True
        logger.info("Pipecat MCP Agent started!")

    async def stop(self):
        """Stop the voice pipeline.

        Sends an `EndFrame` to gracefully shut down the pipeline and waits
        for the background task to complete.
        """
        if not self._started:
            return

        logger.info("Stopping Pipecat MCP agent...")

        # Flush recordings BEFORE shutting down the pipeline — once EndFrame
        # propagates the audio buffer processor is closed.
        if self._audio_buffer is not None and self._record_dir:
            try:
                await self._dump_recordings()
            except Exception as e:
                logger.warning(f"recording dump failed: {e}")

        if self._pipeline_task:
            await self._pipeline_task.queue_frame(EndFrame())

        if self._task:
            await self._task

        self._started = False
        logger.info("Pipecat MCP Agent stopped")

    async def _dump_recordings(self):
        """Write captured audio to WAVs in ``self._record_dir``.

        Pipecat's AudioBufferProcessor names its two buffers ``user`` (input
        from the transport — in browser-shim mode this is the BOT's audio
        coming via the shim's RTCPeerConnection tap) and ``bot`` (output from
        the pipeline — Kokoro TTS that the shim feeds into the synthetic
        mic). Writing both plus a merged stereo mix.
        """
        import os
        import wave

        os.makedirs(self._record_dir, exist_ok=True)
        sr = self._audio_buffer.sample_rate

        # IMPORTANT: read the buffers BEFORE calling stop_recording(). The
        # processor's stop_recording() internally calls _reset_recording()
        # which clears both buffers — call it after we've snapshotted bytes.
        bot_audio = bytes(self._audio_buffer._user_audio_buffer)     # the remote bot's voice
        kokoro_audio = bytes(self._audio_buffer._bot_audio_buffer)   # OUR (Kokoro) voice
        merged = self._audio_buffer.merge_audio_buffers()             # mono mix

        await self._audio_buffer.stop_recording()

        def write_wav(path: str, audio_bytes: bytes):
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(audio_bytes)

        write_wav(os.path.join(self._record_dir, "ember_voice.wav"), bot_audio)
        write_wav(os.path.join(self._record_dir, "kokoro_voice.wav"), kokoro_audio)
        write_wav(os.path.join(self._record_dir, "merged.wav"), merged)
        logger.info(
            f"wrote recordings to {self._record_dir} "
            f"(ember: {len(bot_audio)} B, kokoro: {len(kokoro_audio)} B, merged: {len(merged)} B)"
        )

    async def listen(self) -> str:
        """Wait for user speech and return the transcribed text.

        Blocks until the user completes an utterance (detected via VAD).
        Starts the pipeline automatically if not already running.

        Returns:
            The transcribed text from the user's speech.

        Raises:
            RuntimeError: If the pipeline task is not initialized.

        """
        if not self._started:
            await self.start()

        if not self._pipeline_task:
            raise RuntimeError("Pipecat MCP Agent not initialized")

        await self._connected.wait()
        return await self._user_speech_queue.get()

    async def speak(self, text: str):
        """Speak text to the user using text-to-speech.

        Queues LLM response frames to synthesize and play the given text.
        Starts the pipeline automatically if not already running.

        Args:
            text: The text to speak to the user.

        Raises:
            RuntimeError: If the pipeline task is not initialized.

        """
        if not self._started:
            await self.start()

        if not self._pipeline_task:
            raise RuntimeError("Pipecat MCP Agent not initialized")

        await self._connected.wait()
        await self._pipeline_task.queue_frames(
            [
                LLMFullResponseStartFrame(),
                LLMTextFrame(text=text),
                LLMFullResponseEndFrame(),
            ]
        )

    async def list_windows(self) -> list[dict]:
        """List all open windows via the screen capture backend.

        Returns:
            A list of dicts with title, app_name, and window_id fields.

        """
        windows = await self._screen_capture._backend.list_windows()
        return [
            {"title": w.title, "app_name": w.app_name, "window_id": w.window_id} for w in windows
        ]

    async def screen_capture(self, window_id: Optional[int] = None) -> Optional[int]:
        """Switch screen capture to a different window or full screen.

        Args:
            window_id: Window ID to capture (from list_windows()), or None for full screen.

        Returns:
            The window ID if found, or None if the window was not found or capturing full screen.

        """
        return await self._screen_capture.screen_capture(window_id)

    async def capture_screenshot(self) -> str:
        """Capture a screenshot from the current screen capture stream.

        Saves the next frame to a temporary PNG file. Screen capture
        must already be started via screen_capture().

        Returns:
            The absolute path to the saved image file.

        """
        self._vision.request_capture()
        return await self._vision.get_result()

    def _create_stt_service(self) -> STTService:
        if sys.platform == "darwin":
            return WhisperSTTServiceMLX(model="mlx-community/whisper-large-v3-turbo")
        else:
            return WhisperSTTService(model="Systran/faster-distil-whisper-large-v3")

    def _create_tts_service(self) -> TTSService:
        return KokoroTTSService(voice_id="af_heart")


async def create_agent(runner_args: RunnerArguments) -> PipecatMCPAgent:
    """Create a PipecatMCPAgent wired to the appropriate transport.

    Two modes are supported:
      * ``DailyRunnerArguments`` — pipecat joins a Daily room directly as a
        synthetic peer. Used for the Toocan bot-test flow.
      * ``BrowserShimRunnerArguments`` — pipecat exposes a WebSocket
        server that an in-browser shim connects to. Used when Claude
        drives a real client UI via Playwright and the audio is
        hijacked into the browser's mic / out of the browser's WebRTC
        remote track.

    Returns:
        A configured ``PipecatMCPAgent`` ready to be started.

    """
    if isinstance(runner_args, DailyRunnerArguments):
        from pipecat.transports.daily.transport import DailyParams

        transport_params = {
            "daily": lambda: DailyParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                video_out_enabled=True,
                audio_in_filter=RNNoiseFilter(),
            )
        }
        transport = await create_transport(runner_args, transport_params)
        return PipecatMCPAgent(transport)

    if isinstance(runner_args, BrowserShimRunnerArguments):
        from pipecat.transports.websocket.server import (
            WebsocketServerParams,
            WebsocketServerTransport,
        )

        params = WebsocketServerParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=runner_args.sample_rate,
            audio_out_sample_rate=runner_args.sample_rate,
            # Browser already does AEC/AGC suppression via getUserMedia
            # constraints — leave RNNoise off here to avoid double-processing.
            add_wav_header=False,
            serializer=RawPCMSerializer(sample_rate=runner_args.sample_rate),
        )
        transport = WebsocketServerTransport(
            params=params,
            host=runner_args.host,
            port=runner_args.port,
        )
        return PipecatMCPAgent(transport, record_dir=runner_args.record_dir)

    raise ValueError(f"Unsupported runner_args type: {type(runner_args).__name__}")
