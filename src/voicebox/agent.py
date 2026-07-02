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
from pipecat.pipeline.worker import PipelineWorker
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
from pipecat.workers.runner import WorkerRunner

from voicebox.events import (
    EventType,
    SessionStartedEvent,
    TesterBargeInArmedEvent,
    TesterBargeInFiredEvent,
    TesterTranscriptEvent,
    TranscriptEvent,
    VoiceboxEvent,
)
from voicebox.metrics import compute_metrics
from voicebox.processors.kokoro_tts import KokoroTTSService
from voicebox.raw_pcm_serializer import RawPCMSerializer
from voicebox.runner_args import BrowserShimRunnerArguments

load_dotenv(override=True)

# Silence the VAD must observe before declaring the app bot's utterance over.
# Consequence: every app_bot_speech_stopped event's wall-clock lands about this
# long AFTER the bot truly stopped — recorded in the session_started event so
# consumers can subtract it.
VAD_STOP_SECS = 1.0

# Max seconds speak(wait_for_playout=True) waits for our audio to finish playing out.
PLAYOUT_TIMEOUT_SECS = 120.0

# Kokoro's playout reaches the transport in segments separated by sub-second
# gaps, so pipecat emits a BotStarted/StoppedSpeakingFrame PAIR per segment.
# speak(wait_for_playout=True) treats the playout as finished only after the bot has stayed
# silent this long, so the reported span covers the whole utterance instead of
# clipping at the first segment.
PLAYOUT_SETTLE_SECS = 1.0


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
    """Tracks one in-flight ``speak(wait_for_playout=True)`` until its audio plays out.

    The tester's BotStarted/StoppedSpeakingFrame carry no utterance identity,
    so only one waited speak is tracked at a time (guarded by a lock in
    ``speak``). Because the playout arrives in segments, the future resolves
    only after the bot has been silent for ``settle_secs`` (so the span covers
    the whole utterance), reporting first-start to last-stop; an interruption
    resolves it immediately.
    """

    def __init__(self, settle_secs: float):
        self._loop = asyncio.get_event_loop()
        self._settle_secs = settle_secs
        self.future: asyncio.Future[dict] = self._loop.create_future()
        self.started_at: Optional[float] = None
        self._last_stopped_at: Optional[float] = None
        self.interrupted: bool = False
        self._settle_timer: Optional[asyncio.TimerHandle] = None

    def on_started(self, t: float):
        """Record the first segment's start and cancel any pending resolve."""
        if self.started_at is None:
            self.started_at = t
        if self._settle_timer is not None:
            self._settle_timer.cancel()
            self._settle_timer = None

    def on_stopped(self, t: float):
        """Record a segment end and (re)arm the settle timer to resolve on silence."""
        self._last_stopped_at = t
        if self._settle_timer is not None:
            self._settle_timer.cancel()
        self._settle_timer = self._loop.call_later(self._settle_secs, self._resolve)

    def on_interrupted(self, t: float):
        """Barge-in cut the playout short — resolve immediately."""
        self.interrupted = True
        if self._last_stopped_at is None:
            self._last_stopped_at = t
        self._resolve()

    def _resolve(self):
        """Resolve the future with the observed playout span (first → last)."""
        if self._settle_timer is not None:
            self._settle_timer.cancel()
            self._settle_timer = None
        if not self.future.done():
            self.future.set_result(
                {
                    "started_at": self.started_at,
                    "finished_at": self._last_stopped_at,
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
                and ``stop()`` writes the per-speaker + stereo WAVs, the
                ``events.json`` log and the ``metrics.json`` report into this
                directory, returning their absolute paths.

        """
        self._transport = transport
        self._record_dir = record_dir
        self._audio_buffer = None  # type: ignore[assignment]

        self._task: Optional[asyncio.Task] = None
        self._pipeline_task: Optional[PipelineWorker] = None
        self._pipeline_runner: Optional[WorkerRunner] = None
        self._connected = asyncio.Event()

        # Monotonic event log; listen_events() blocks on the condition until
        # the log grows past the caller's cursor.
        self._events: list[VoiceboxEvent] = []
        self._event_cond = asyncio.Condition()

        # In-flight speak(wait_for_playout=True) playout tracking — one at a time.
        self._playout: Optional[_Playout] = None
        self._playout_lock = asyncio.Lock()

        # Whether the app bot is currently speaking, toggled by the VAD frames.
        # Lets speak(wait_for_turn=True) block until the bot falls silent.
        self._app_bot_speaking: bool = False
        # Background tasks armed by speak(when=...); cancelled on stop().
        self._armed_tasks: set[asyncio.Task] = set()

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
            self._app_bot_speaking = True
            await self._emit(
                VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STARTED, t=frame.timestamp)
            )
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._app_bot_speaking = False
            await self._emit(
                VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STOPPED, t=frame.timestamp)
            )
        elif isinstance(frame, BotStartedSpeakingFrame):
            event = VoiceboxEvent(type=EventType.TESTER_SPEECH_STARTED)
            if self._playout is not None:
                self._playout.on_started(event.t)
            await self._emit(event)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            event = VoiceboxEvent(type=EventType.TESTER_SPEECH_STOPPED)
            if self._playout is not None:
                self._playout.on_stopped(event.t)
            await self._emit(event)
        elif isinstance(frame, InterruptionFrame):
            event = VoiceboxEvent(type=EventType.TESTER_SPEECH_INTERRUPTED)
            if self._playout is not None:
                self._playout.on_interrupted(event.t)
            await self._emit(event)

    async def start(self):
        """Build the pipeline and run it in the background until ``stop()``."""
        if self._started:
            return

        logger.info("Starting Pipecat MCP Agent pipeline...")

        # Constructing these services triggers the first-run model downloads
        # (Kokoro ~300 MB, Whisper ~1.5 GB). Bracket them with parent-visible
        # log lines so a long readiness wait is explainable from stderr.
        logger.info("Loading STT/TTS models (first run downloads them; cached afterward)...")
        stt = self._create_stt_service()
        tts = self._create_tts_service()
        logger.info("STT/TTS models loaded.")

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
                num_channels=2,  # stereo merge: tester left, app bot right
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
        self._pipeline_task = PipelineWorker(
            pipeline,
            cancel_on_idle_timeout=False,
            enable_rtvi=False,
            observers=[_PipelineEventObserver(self)],
        )

        self._pipeline_runner = WorkerRunner(handle_sigterm=True)

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

        # 1.3.0: register the worker via add_workers() before run() — passing it
        # to run() directly is deprecated. run() (auto_end=True) returns when the
        # worker finishes (our EndFrame in stop()).
        await self._pipeline_runner.add_workers(self._pipeline_task)
        self._task = asyncio.create_task(self._pipeline_runner.run())

        if self._audio_buffer is not None:

            async def _start_recording():
                await self._connected.wait()
                await self._audio_buffer.start_recording()
                logger.info("Audio recording started")

            asyncio.create_task(_start_recording())

        self._started = True
        logger.info("Pipecat MCP Agent started!")

    async def stop(self) -> Optional[dict]:
        """Flush artifacts, send ``EndFrame``, await the runner.

        Returns:
            A dict of absolute artifact paths when ``record_dir`` is set
            (``{events, metrics, merged_wav, tester_wav, app_bot_wav}``), else
            ``None``. ``*_wav`` keys are present only when audio was recorded.

        """
        if not self._started:
            return None

        logger.info("Stopping Pipecat MCP agent...")

        # Signal session end FIRST so a pending listen_events() returns this
        # event instead of being cancelled mid-wait when the pipeline tears down.
        await self._emit(VoiceboxEvent(type=EventType.SESSION_STOPPED))

        # Cancel any armed barge-in triggers still waiting for their event or
        # sleeping on their timer — they must not queue speech after teardown.
        for task in self._armed_tasks:
            task.cancel()
        await asyncio.gather(*self._armed_tasks, return_exceptions=True)

        # Flush artifacts BEFORE EndFrame propagates — the audio buffer
        # processor is closed after EndFrame.
        artifacts = None
        if self._record_dir:
            try:
                artifacts = await self._dump_artifacts(self._record_dir)
            except Exception as e:
                logger.warning(f"artifact dump failed: {e}")

        if self._pipeline_task:
            await self._pipeline_task.queue_frame(EndFrame())

        if self._task:
            await self._task

        self._started = False
        logger.info("Pipecat MCP Agent stopped")
        return artifacts

    async def _dump_artifacts(self, record_dir: str) -> dict:
        """Write the per-session artifacts into ``record_dir``.

        Always writes ``events.json`` (the event log) and ``metrics.json`` (the
        computed test report). When audio was recorded, also writes the
        per-speaker mono WAVs and a stereo ``merged.wav`` (tester left, app bot
        right). AudioBufferProcessor's two buffers are inverted vs. voicebox
        semantics:
          * ``_user_audio_buffer`` — transport INPUT, i.e. the APP BOT's voice
            (ember) arriving via the shim's WebRTC tap.
          * ``_bot_audio_buffer`` — pipeline OUTPUT, i.e. our Kokoro TTS (the
            tester) fed into the synthetic mic.

        Returns:
            Absolute paths of the written artifacts.

        """
        import json
        import os
        import wave

        from pipecat.audio.utils import interleave_stereo_audio

        os.makedirs(record_dir, exist_ok=True)

        def path(name: str) -> str:
            return os.path.abspath(os.path.join(record_dir, name))

        artifacts: dict = {}

        events = [e.model_dump() for e in self._events]
        events_path = path("events.json")
        with open(events_path, "w") as f:
            json.dump(events, f, indent=2)
        artifacts["events"] = events_path

        metrics_path = path("metrics.json")
        with open(metrics_path, "w") as f:
            json.dump(compute_metrics(events, VAD_STOP_SECS), f, indent=2)
        artifacts["metrics"] = metrics_path

        buffer = self._audio_buffer
        if buffer is not None:
            sr = buffer.sample_rate
            # Snapshot BEFORE stop_recording() — it calls _reset_recording()
            # which clears both buffers.
            app_bot_audio = bytes(buffer._user_audio_buffer)
            tester_audio = bytes(buffer._bot_audio_buffer)
            # Tester on the left, app bot on the right (the library's own merge
            # would invert this, so interleave explicitly).
            merged = interleave_stereo_audio(tester_audio, app_bot_audio)
            await buffer.stop_recording()

            def write_wav(name: str, audio_bytes: bytes, channels: int) -> str:
                p = path(name)
                with wave.open(p, "wb") as w:
                    w.setnchannels(channels)
                    w.setsampwidth(2)
                    w.setframerate(sr)
                    w.writeframes(audio_bytes)
                return p

            artifacts["app_bot_wav"] = write_wav("ember_voice.wav", app_bot_audio, 1)
            artifacts["tester_wav"] = write_wav("kokoro_voice.wav", tester_audio, 1)
            artifacts["merged_wav"] = write_wav("merged.wav", merged, 2)

        logger.info(f"wrote artifacts to {self._record_dir}: {sorted(artifacts)}")
        return artifacts

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

    async def speak(
        self,
        text: str,
        wait_for_playout: bool = False,
        wait_for_turn: bool = False,
        when: Optional[str] = None,
        timer_secs: float = 0.0,
    ) -> dict:
        """Queue an LLM response so TTS speaks ``text`` into the transport.

        Barge-in testing means TIMING our speech relative to the app bot's, then
        observing the bot's reaction in the event log — we never interrupt the
        bot directly (it is a black box reached only through its microphone).

        ``wait_for_turn`` and ``when`` gate WHEN we start speaking;
        ``wait_for_playout`` gates WHEN this call returns (the start gates and
        the return gate are independent and may be combined).

        Args:
            text: The text to speak.
            wait_for_playout: When False (default) the call returns as soon as
                the speech is queued. When True it returns only after OUR OWN
                audio has finished playing out, reporting the playout span
                (``started_at`` / ``finished_at`` / ``interrupted``). It says
                nothing about the app bot — it waits for our Kokoro audio to
                finish. Ignored when ``when`` is set (the call returns
                immediately with ``{"armed": True}``).
            wait_for_turn: When True, block until the app bot is NOT currently
                speaking, then speak. If already silent, speak immediately. This
                gates the START of our speech (the polite path).
            when: An ``EventType`` value. When set, arm a ONE-SHOT trigger:
                return ``{"armed": True}`` immediately, then in the background
                wait for the NEXT occurrence of that event, sleep
                ``timer_secs``, and speak ``text`` (canonical barge-in:
                ``when="app_bot_speech_started", timer_secs=1.5``).
            timer_secs: Seconds to sleep after the ``when`` event fires before
                speaking. Only meaningful with ``when``.

        Returns:
            ``{"armed": True}`` immediately when ``when`` is set;
            ``{"queued": True}`` when ``wait_for_playout`` is False; otherwise
            ``{"queued": True, "started_at", "finished_at", "interrupted"}``
            (wall-clock seconds).

        Raises:
            ValueError: If both ``when`` and ``wait_for_turn`` are set, or if
                ``when`` is not a valid ``EventType`` value.

        """
        if when is not None and wait_for_turn:
            raise ValueError("speak: 'when' and 'wait_for_turn' are mutually exclusive")
        if when is not None and when not in {e.value for e in EventType}:
            valid = ", ".join(sorted(e.value for e in EventType))
            raise ValueError(f"speak: unknown event type 'when={when}'; valid values: {valid}")

        if not self._started:
            await self.start()

        if not self._pipeline_task:
            raise RuntimeError("Pipecat MCP Agent not initialized")

        if when is not None:
            # Snapshot the log position synchronously HERE, before spawning the
            # task: the trigger must react only to events arriving after arming,
            # and capturing it inside the task would race the task's own
            # scheduling against the triggering event (and could drop it).
            start = len(self._events)
            await self._emit(TesterBargeInArmedEvent(when=when, timer_secs=timer_secs, text=text))
            task = asyncio.create_task(self._armed_speak(text, when, timer_secs, start))
            self._armed_tasks.add(task)
            task.add_done_callback(self._armed_tasks.discard)
            return {"armed": True}

        await self._connected.wait()

        if wait_for_turn:
            await self._wait_for_app_bot_silent()

        # Log what we said (ground-truth input, not STT) so the event stream is
        # a complete two-sided transcript.
        await self._emit(TesterTranscriptEvent(text=text))

        if not wait_for_playout:
            await self._queue_speak_frames(text)
            return {"queued": True}

        # One tracked playout at a time: the tester's speaking frames carry no
        # utterance identity, so overlapping waited speaks could not be told
        # apart.
        async with self._playout_lock:
            self._playout = _Playout(PLAYOUT_SETTLE_SECS)
            try:
                await self._queue_speak_frames(text)
                result = await asyncio.wait_for(self._playout.future, timeout=PLAYOUT_TIMEOUT_SECS)
            finally:
                self._playout = None
        return {"queued": True, **result}

    async def _wait_for_app_bot_silent(self):
        """Block until the app bot is not currently speaking."""
        async with self._event_cond:
            await self._event_cond.wait_for(lambda: not self._app_bot_speaking)

    async def _armed_speak(self, text: str, when: str, timer_secs: float, start: int):
        """Background trigger: wait for the next ``when`` event, then speak.

        ``start`` is the event-log length captured at arm time (in ``speak()``),
        so only events that arrive AFTER arming can trigger. Sleeps
        ``timer_secs``, then queues ``text``. Cancellation from ``stop()``
        propagates out cleanly — the ``async with`` releases the lock.
        """
        async with self._event_cond:
            await self._event_cond.wait_for(
                lambda: any(e.type == when for e in self._events[start:])
            )
            # e.type is a STRING (use_enum_values=True); ``when`` is that string.
            triggered = next(e for e in self._events[start:] if e.type == when)

        await asyncio.sleep(timer_secs)
        await self._connected.wait()

        await self._emit(TesterBargeInFiredEvent(when=when, triggered_by_t=triggered.t))
        await self._emit(TesterTranscriptEvent(text=text))
        await self._queue_speak_frames(text)

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
            return WhisperSTTServiceMLX(
                settings=WhisperSTTServiceMLX.Settings(model="mlx-community/whisper-large-v3-turbo")
            )
        return WhisperSTTService(
            settings=WhisperSTTService.Settings(model="Systran/faster-distil-whisper-large-v3")
        )

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
