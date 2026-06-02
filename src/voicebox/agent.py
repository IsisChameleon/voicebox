#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pipecat agent for the browser-shim audio path.

Builds the STT → aggregator → TTS pipeline behind a WebSocket transport
that an in-browser shim connects to. Exposes ``listen()`` and ``speak()``
that the MCP server drives over IPC.
"""

import asyncio
import sys
from typing import Any, Optional

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.runner.types import RunnerArguments
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.services.whisper.stt import WhisperSTTService, WhisperSTTServiceMLX
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from voicebox.processors.kokoro_tts import KokoroTTSService
from voicebox.raw_pcm_serializer import RawPCMSerializer
from voicebox.runner_args import BrowserShimRunnerArguments

load_dotenv(override=True)


class PipecatMCPAgent:
    """Voice pipeline exposing listen()/speak() over the configured transport."""

    def __init__(self, transport: BaseTransport, record_dir: Optional[str] = None):
        """Initialize the agent.

        Args:
            transport: Pipecat transport (WebSocket server for the browser shim).
            record_dir: If set, audio is buffered via ``AudioBufferProcessor``
                and ``stop()`` writes user/bot/merged WAVs into this directory.

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
        """Build the pipeline and run it in the background until ``stop()``."""
        if self._started:
            return

        logger.info("Starting Pipecat MCP Agent pipeline...")

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
                # 1.0s captures complete utterances over WebRTC with natural
                # pauses; 0.2s (pipecat's default for clean TTS sources) chops
                # remote speech mid-sentence and produces single-word transcripts.
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=1.0)),
            ),
        )

        if self._record_dir:
            from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

            self._audio_buffer = AudioBufferProcessor(
                sample_rate=48000,
                num_channels=1,
                buffer_size=0,  # accumulate everything
            )

        stages = [
            self._transport.input(),
            stt,
            user_aggregator,
            tts,
            assistant_aggregator,
            self._transport.output(),
        ]
        # AudioBufferProcessor at the END catches both InputAudioRawFrame
        # (from transport.input downstream) and OutputAudioRawFrame (TTS →
        # transport.output) as they continue past the output — neither is
        # destructively consumed by transport.output.
        if self._audio_buffer:
            stages.append(self._audio_buffer)
        pipeline = Pipeline(stages)

        # enable_rtvi=False: we are a headless synthetic user, not an RTVI
        # client. Transcripts reach Claude via listen()'s return value, not
        # via data-channel notifications.
        self._pipeline_task = PipelineTask(
            pipeline,
            cancel_on_idle_timeout=False,
            enable_rtvi=False,
        )

        self._pipeline_runner = PipelineRunner(handle_sigterm=True)

        @self._transport.event_handler("on_client_connected")
        async def on_connected(transport, client):
            logger.info("Client connected")
            self._connected.set()

        @self._transport.event_handler("on_client_disconnected")
        async def on_disconnected(transport, client):
            logger.info("Client disconnected")
            if not self._pipeline_task:
                return
            await self._user_speech_queue.put("I just disconnected, but I might come back.")

        @user_aggregator.event_handler("on_user_turn_stopped")
        async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):
            if message.content:
                await self._user_speech_queue.put(message.content)

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
        """Flush recordings, send ``EndFrame``, await the runner."""
        if not self._started:
            return

        logger.info("Stopping Pipecat MCP agent...")

        # Flush recordings BEFORE EndFrame propagates — the audio buffer
        # processor is closed after EndFrame.
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

        AudioBufferProcessor's two buffers map to:
          * ``_user_audio_buffer`` — input from the transport, i.e. the BOT's
            voice arriving via the shim's WebRTC tap.
          * ``_bot_audio_buffer`` — output from the pipeline, i.e. our Kokoro
            TTS that the shim feeds into the synthetic mic.
        """
        import os
        import wave

        os.makedirs(self._record_dir, exist_ok=True)
        sr = self._audio_buffer.sample_rate

        # IMPORTANT: snapshot the buffers BEFORE stop_recording() — the
        # processor's stop_recording() internally calls _reset_recording()
        # which clears both buffers.
        bot_audio = bytes(self._audio_buffer._user_audio_buffer)
        kokoro_audio = bytes(self._audio_buffer._bot_audio_buffer)
        merged = self._audio_buffer.merge_audio_buffers()

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
        """Block until the next utterance and return its transcription."""
        if not self._started:
            await self.start()

        if not self._pipeline_task:
            raise RuntimeError("Pipecat MCP Agent not initialized")

        await self._connected.wait()
        return await self._user_speech_queue.get()

    async def speak(self, text: str):
        """Queue an LLM response so TTS speaks ``text`` into the transport."""
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

    def _create_stt_service(self) -> STTService:
        if sys.platform == "darwin":
            return WhisperSTTServiceMLX(model="mlx-community/whisper-large-v3-turbo")
        return WhisperSTTService(model="Systran/faster-distil-whisper-large-v3")

    def _create_tts_service(self) -> TTSService:
        return KokoroTTSService(voice_id="af_heart")


async def create_agent(runner_args: RunnerArguments) -> PipecatMCPAgent:
    """Create a ``PipecatMCPAgent`` wired to the browser-shim transport."""
    if not isinstance(runner_args, BrowserShimRunnerArguments):
        raise ValueError(f"Unsupported runner_args type: {type(runner_args).__name__}")

    from pipecat.transports.websocket.server import (
        WebsocketServerParams,
        WebsocketServerTransport,
    )

    # Asymmetric rates: incoming bytes (browser tap → us) arrive at 16 kHz
    # because Whisper-MLX requires it (mlx_whisper.transcribe has no
    # sample_rate parameter); outgoing bytes (Kokoro → browser mic) stay at
    # 48 kHz so the page's AudioContext consumes them natively.
    params = WebsocketServerParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=runner_args.tap_rate,
        audio_out_sample_rate=runner_args.mic_rate,
        add_wav_header=False,
        serializer=RawPCMSerializer(sample_rate=runner_args.tap_rate),
    )
    transport = WebsocketServerTransport(
        params=params,
        host=runner_args.host,
        port=runner_args.port,
    )
    return PipecatMCPAgent(transport, record_dir=runner_args.record_dir)
