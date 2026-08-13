"""Kokoro configuration and generated-utterance playout contracts."""

from pipecat.services.kokoro.tts import KokoroTTSService

from voicebox.agent import PipecatMCPAgent, _Playout


async def test_voicebox_uses_stock_kokoro_once_per_speak():
    agent = PipecatMCPAgent(None)  # type: ignore[arg-type]
    service = agent._create_tts_service()

    assert type(service) is KokoroTTSService
    texts = [
        aggregation.text
        async for aggregation in service._text_aggregator.aggregate(
            "Hello Ember, lovely to meet you. Please read me the story. Give me choices."
        )
    ]
    assert texts == ["Hello Ember, lovely to meet you. Please read me the story. Give me choices."]


async def test_playout_resolves_on_bot_stopped_after_tts_stopped():
    playout = _Playout()
    playout.on_started(1.0)
    playout.on_stopped(2.0)
    assert not playout.future.done()

    playout.on_tts_stopped()
    assert not playout.future.done()

    playout.on_stopped(5.0)
    assert playout.future.done()
    assert playout.future.result() == {
        "started_at": 1.0,
        "finished_at": 5.0,
        "interrupted": False,
    }


async def test_interruption_resolves_playout_immediately():
    playout = _Playout()
    playout.on_started(1.0)
    playout.on_interrupted(3.0)

    assert playout.future.done()
    result = playout.future.result()
    assert result["interrupted"] is True
    assert result["finished_at"] == 3.0
