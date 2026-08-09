#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The fake app's "brain" seam: which processor fills the LLM slot of the pipeline.

``create_brain()`` picks by ``VOICEBOX_FAKE_APP_LLM_PROVIDER``:

- ``anthropic`` (default) / ``openai`` — pipecat's stock LLM service, needs the
  provider's API key in the environment.
- ``scripted`` — a deterministic canned responder (no API key): it plays the
  trivia-host lines below in order, one per user turn, ignoring what was said.
  Determinism is what future evals need; the third line is deliberately LONG so
  barge-in tests have a multi-sentence utterance to interrupt.
"""

import os

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

PROVIDER_ENV = "VOICEBOX_FAKE_APP_LLM_PROVIDER"
MODEL_ENV = "VOICEBOX_FAKE_APP_LLM_MODEL"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"

SCRIPT = [
    "Welcome to Nova's space trivia! First question: which planet in our solar system "
    "has the most moons?",
    "Nice try! The current champion is Saturn, with well over one hundred confirmed moons. "
    "Next question, or ask me to explain in detail: what was the first spacecraft to land "
    "softly on the Moon?",
    "Great question, let me explain in detail. The first spacecraft to soft-land on the Moon "
    "was the Soviet probe Luna 9, which touched down in the Ocean of Storms on the third of "
    "February, nineteen sixty-six. Before Luna 9, every attempt had either crashed or missed "
    "the Moon entirely, and many scientists genuinely feared a lander would sink into a deep "
    "layer of dust. Luna 9 settled that debate by transmitting the first photographs ever "
    "taken from the lunar surface, proving the ground could bear a spacecraft's weight. "
    "Four months later the American Surveyor 1 repeated the feat with a far more capable "
    "camera, paving the way for Apollo. It is remarkable that barely three years after "
    "those robotic landings, human beings were walking on the very same surface.",
    "You're on a roll! Last one for now: what is the name of the largest volcano in the "
    "solar system, found on Mars?",
    "That's Olympus Mons, a shield volcano about two and a half times the height of Everest. "
    "That's the end of my question deck, but feel free to keep chatting!",
]


class ScriptedBrain(FrameProcessor):
    """Deterministic canned responder occupying the pipeline's LLM slot.

    Each ``LLMContextFrame`` (a completed user turn, or the kickoff
    ``LLMRunFrame``) plays the next line of ``SCRIPT`` as a standard LLM
    response triplet; everything else passes through untouched.
    """

    def __init__(self, script: list[str]):
        """Initialize with the ordered lines to play (cycled when exhausted)."""
        super().__init__()
        self._script = script
        self._index = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Respond to completed user turns; pass every other frame through."""
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        line = self._script[self._index % len(self._script)]
        self._index += 1
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(line))
        await self.push_frame(LLMFullResponseEndFrame())


def create_brain() -> FrameProcessor:
    """Build the brain selected by the environment (see module docstring).

    Returns:
        The frame processor to place in the pipeline's LLM slot.

    Raises:
        SystemExit: On an unknown provider or a missing API key, so startup
            fails with a clear message instead of a mid-conversation error.

    """
    provider = os.environ.get(PROVIDER_ENV, "anthropic")
    if provider == "scripted":
        return ScriptedBrain(SCRIPT)
    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise SystemExit(
                f"ANTHROPIC_API_KEY is required for {PROVIDER_ENV}=anthropic (the default). "
                f"Export it, or use {PROVIDER_ENV}=scripted for a no-key canned brain."
            )
        from pipecat.services.anthropic.llm import AnthropicLLMService

        model = os.environ.get(MODEL_ENV, DEFAULT_ANTHROPIC_MODEL)
        return AnthropicLLMService(
            api_key=api_key, settings=AnthropicLLMService.Settings(model=model)
        )
    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit(f"OPENAI_API_KEY is required for {PROVIDER_ENV}=openai.")
        from pipecat.services.openai.llm import OpenAILLMService

        model = os.environ.get(MODEL_ENV)
        settings = OpenAILLMService.Settings(model=model) if model else None
        return OpenAILLMService(api_key=api_key, settings=settings)
    raise SystemExit(
        f"Unknown {PROVIDER_ENV}={provider!r} (expected anthropic | openai | scripted)"
    )
