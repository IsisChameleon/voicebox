r"""DEBUG-level wall-clock instrumentation for the audio path.

Measures the STT's ``run_stt`` call so a live session log attributes
transcription lag to the decode rather than leaving it unexplained.

Every line is greppable by ``TIMING_PREFIX`` and carries the duration as a
plain float, so a log can be split with one grep:

    grep -o 'voicebox\.timing name=[a-z_]* secs=[0-9.]*' session.log

Nothing here changes behaviour. The mixins delegate to the next class in the
MRO and only bracket the call with a ``time.perf_counter()`` read; they emit no
frames and add no awaits that can block.
"""

import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager

from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.services.stt_service import STTService

# Single stable prefix for every timing line. Grep this and nothing else.
TIMING_PREFIX = "voicebox.timing"


@contextmanager
def log_duration(name: str) -> Iterator[None]:
    """Log the wall-clock duration of the wrapped block at DEBUG.

    Args:
        name: Identifier for the measured call, logged as ``name=<name>``.

    Yields:
        Nothing; the block runs unchanged and exceptions propagate untouched.

    """
    started = time.perf_counter()
    try:
        yield
    finally:
        logger.debug(f"{TIMING_PREFIX} name={name} secs={time.perf_counter() - started:.3f}")


class TimedSTTMixin(STTService):
    """Times ``run_stt`` on whatever STT service it is mixed into.

    List it FIRST in the bases so it precedes the concrete service in the MRO
    (``class X(TimedSTTMixin, WhisperSTTService)``). Because ``super()`` follows
    the MRO rather than a fixed base, the same mixin composes with a *subclass*
    of a concrete service without changing.

    The measured span covers the full consumption of the generator, i.e. how
    long the STT held its frame task -- which is the number this phase is
    trying to attribute.
    """

    # pipecat annotates the abstract STTService.run_stt as `async def -> AsyncGenerator`
    # with no yield in its body, so a type checker reads the base as a coroutine that
    # returns a generator rather than as an async generator itself. Every concrete
    # implementation -- and SegmentedSTTService's own call site -- is an async generator.
    # The ignores below cover that annotation mismatch and nothing else.
    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:  # type: ignore
        """Delegate transcription to the wrapped service, timing the call.

        Args:
            audio: Raw audio bytes to transcribe.

        Yields:
            Whatever the wrapped service yields, unchanged and in order.

        """
        with log_duration("run_stt"):
            async for frame in super().run_stt(audio):  # type: ignore
                yield frame
