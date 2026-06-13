#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pipecat agent for the browser-shim audio path.

Builds the STT → aggregator → TTS pipeline behind a WebSocket transport
that an in-browser shim connects to. Exposes ``listen_events()`` and
``speak()`` that the MCP server drives over IPC.

Stage 2: instead of returning one transcript string per call, the agent
keeps a monotonic, timestamped event log (see ``events.py``) fed by a
pipeline observer, and ``listen_events(cursor)`` streams it incrementally.

Party naming is inverted from pipecat's: pipecat's "user" is the app bot
under test (its audio is our input), and pipecat's "bot" is our synthetic
tester (our TTS is the output). The observer is the one place that mapping
lives.
"""

import asyncio
import sys
from typing import Optional

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import RunnerArguments
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.services.whisper.stt import WhisperSTTService, WhisperSTTServiceMLX
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_start import (
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from voicebox.events import EventType, SessionStartedEvent, TranscriptEvent, VoiceboxEvent
from voicebox.processors.kokoro_tts import KokoroTTSService
from voicebox.raw_pcm_serializer import RawPCMSerializer
from voicebox.runner_args import BrowserShimRunnerArguments

load_dotenv(override=True)

# Silence the VAD must observe before declaring the app bot's utterance over.
# Consequence: every app_bot_speech_stopped event's wall-clock lands about this
# long AFTER the bot truly stopped — recorded in the session_started event so
# consumers can subtract it.
VAD_STOP_SECS = 1.0

# Max seconds speak(wait=True) waits for our audio to finish playing out.
PLAYOUT_TIMEOUT_SECS = 120.0


class _PipelineEventObserver(BaseObserver):
    """Feeds the agent's event log from frames crossing the pipeline.

    Observes every processor→processor push without modifying the pipeline.
    Only downstream pushes are considered (the output transport emits paired
    upstream/downstream sibling frames), and each frame id is handled once
    (the same frame instance is observed at every hop it traverses).
    """

    _WATCHED = (
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        InterruptionFrame,
    )

    def __init__(self, agent: "PipecatMCPAgent"):
        super().__init__()
        self._agent = agent
        self._seen_frame_ids: set[int] = set()

    async def on_push_frame(self, data: FramePushed):
        if data.direction != FrameDirection.DOWNSTREAM:
            return
        frame = data.frame
        if not isinstance(frame, self._WATCHED):
            return
        if frame.id in self._seen_frame_ids:
            return
        self._seen_frame_ids.add(frame.id)
        await self._agent._on_pipeline_frame(frame)


class _Playout:
    """Tracks one in-flight ``speak(wait=True)`` until its audio plays out.

    The tester's BotStarted/StoppedSpeakingFrame carry no utterance identity,
    so only one waited speak is tracked at a time (guarded by a lock in
    ``speak``). ``future`` resolves with the playout span when TTS stops.
    """

    def __init__(self):
        self.future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self.started_at: Optional[float] = None
        self.interrupted: bool = False

    def finish(self, finished_at: float):
        """Resolve the future with the observed playout span."""
        if not self.future.done():
            self.future.set_result(
                {
                    "started_at": self.started_at,
                    "finished_at": finished_at,
                    "interrupted": self.interrupted,
                }
            )


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
        self._connected = asyncio.Event()

        # Monotonic event log; listen_events() blocks on the condition until
        # the log grows past the caller's cursor.
        self._events: list[VoiceboxEvent] = []
        self._event_cond = asyncio.Condition()

        # In-flight speak(wait=True) playout tracking — one at a time.
        self._playout: Optional[_Playout] = None
        self._playout_lock = asyncio.Lock()

        self._started = False

    async def _emit(self, event: VoiceboxEvent):
        """Append an event to the log and wake any pending listen_events()."""
        async with self._event_cond:
            self._events.append(event)
            self._event_cond.notify_all()
        logger.debug(f"event: {event.type} @ {event.t:.3f}")

    async def _on_pipeline_frame(self, frame: Frame):
        """Translate an observed pipeline frame into a log event.

        pipecat's VAD "user" frames are the APP BOT (its audio is our input);
        its "bot" speaking frames are the TESTER (our TTS playing out). VAD
        frames carry a wall-clock ``timestamp`` (the VAD's emission instant);
        the bot-speaking frames carry none, so those stamp at observation.
        """
        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self._emit(
                VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STARTED, t=frame.timestamp)
            )
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._emit(
                VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STOPPED, t=frame.timestamp)
            )
        elif isinstance(frame, BotStartedSpeakingFrame):
            event = VoiceboxEvent(type=EventType.TESTER_SPEECH_STARTED)
            if self._playout is not None:
                self._playout.started_at = event.t
            await self._emit(event)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            event = VoiceboxEvent(type=EventType.TESTER_SPEECH_STOPPED)
            if self._playout is not None:
                self._playout.finish(event.t)
            await self._emit(event)
        elif isinstance(frame, InterruptionFrame):
            if self._playout is not None:
                self._playout.interrupted = True
            await self._emit(VoiceboxEvent(type=EventType.TESTER_SPEECH_INTERRUPTED))

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
                    # The "user" of this pipeline is the REMOTE BOT (its audio
                    # is our input). The default start strategies ship with
                    # enable_interruptions=True, which cancels our in-flight
                    # Kokoro TTS the moment the bot makes a sound — a synthetic
                    # human must be able to keep talking (and talk over the bot).
                    start=[
                        VADUserTurnStartStrategy(enable_interruptions=False),
                        TranscriptionUserTurnStartStrategy(enable_interruptions=False),
                    ],
                    stop=[
                        TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())
                    ],
                ),
                # 1.0s captures complete utterances over WebRTC with natural
                # pauses; 0.2s (pipecat's default for clean TTS sources) chops
                # remote speech mid-sentence and produces single-word transcripts.
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=VAD_STOP_SECS)),
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
        # client. Conversation events reach Claude via listen_events()'s
        # return value, not via data-channel notifications.
        self._pipeline_task = PipelineTask(
            pipeline,
            cancel_on_idle_timeout=False,
            enable_rtvi=False,
            observers=[_PipelineEventObserver(self)],
        )

        self._pipeline_runner = PipelineRunner(handle_sigterm=True)

        @self._transport.event_handler("on_client_connected")
        async def on_connected(transport, client):
            logger.info("Client connected")
            self._connected.set()
            await self._emit(VoiceboxEvent(type=EventType.CLIENT_CONNECTED))

        @self._transport.event_handler("on_client_disconnected")
        async def on_disconnected(transport, client):
            logger.info("Client disconnected")
            await self._emit(VoiceboxEvent(type=EventType.CLIENT_DISCONNECTED))

        @user_aggregator.event_handler("on_user_turn_stopped")
        async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):
            if message.content:
                # message.timestamp is the ISO wall-clock at which the app
                # bot's turn STARTED; the event's t is when the batch
                # transcript became ready.
                await self._emit(
                    TranscriptEvent(text=message.content, turn_started_at=message.timestamp)
                )

        # Log header: consumers of app_bot_speech_stopped timings need the
        # built-in VAD lag to subtract it.
        await self._emit(
            SessionStartedEvent(
                vad_stop_secs=VAD_STOP_SECS,
                note=(
                    "app_bot_speech_stopped.t lands ~vad_stop_secs after true "
                    "speech end; app_bot_transcript arrives later still (batch STT)"
                ),
            )
        )

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

    async def listen_events(self, timeout: float, cursor: int = 0) -> dict:
        """Return the events past ``cursor``, blocking until at least one exists.

        Args:
            timeout: Max seconds to wait for a new event past the cursor.
                On timeout, returns an empty ``events`` list.
            cursor: Index into the session's monotonic event log; pass the
                ``cursor`` from the previous call to resume without missing
                or re-reading events. ``0`` replays the whole session.

        Returns:
            ``{"events": [...], "cursor": <next cursor>}``.

        """
        if not self._started:
            await self.start()

        cursor = max(0, min(cursor, len(self._events)))
        async with self._event_cond:
            if len(self._events) <= cursor:
                try:
                    await asyncio.wait_for(
                        self._event_cond.wait_for(lambda: len(self._events) > cursor),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    pass
            events = self._events[cursor:]
        return {"events": [e.model_dump() for e in events], "cursor": cursor + len(events)}

    async def speak(self, text: str, wait: bool = False) -> dict:
        """Queue an LLM response so TTS speaks ``text`` into the transport.

        Args:
            text: The text to speak.
            wait: When True, resolve only after our audio finished playing
                out through the transport, and report the playout span.

        Returns:
            ``{"queued": True}`` immediately when ``wait`` is False;
            otherwise ``{"queued": True, "started_at", "finished_at",
            "interrupted"}`` (wall-clock seconds).

        """
        if not self._started:
            await self.start()

        if not self._pipeline_task:
            raise RuntimeError("Pipecat MCP Agent not initialized")

        await self._connected.wait()

        if not wait:
            await self._queue_speak_frames(text)
            return {"queued": True}

        # One tracked playout at a time: the tester's speaking frames carry no
        # utterance identity, so overlapping waited speaks could not be told
        # apart.
        async with self._playout_lock:
            self._playout = _Playout()
            try:
                await self._queue_speak_frames(text)
                result = await asyncio.wait_for(self._playout.future, timeout=PLAYOUT_TIMEOUT_SECS)
            finally:
                self._playout = None
        return {"queued": True, **result}

    async def _queue_speak_frames(self, text: str):
        """Push the LLM-response frame triplet that drives TTS."""
        if not self._pipeline_task:
            raise RuntimeError("Pipecat MCP Agent not initialized")
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
