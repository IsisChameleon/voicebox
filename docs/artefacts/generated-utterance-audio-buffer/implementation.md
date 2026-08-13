# Generated utterance audio buffer — implementation evidence

Issue #19 is implemented without a Voicebox Kokoro subclass.

## Landed

- pipecat stock Kokoro, configured canonically with `Settings(voice="af_heart")`
- TOKEN aggregation: one `speak()` becomes one synthesis context
- `GeneratedUtteranceAudioBuffer` immediately after TTS
- generic stream-consuming startup warm-up
- stock, unbuffered Kokoro in Nova so the reference app does not share tester-side TTS policy
- deletion of the 220-line fork-by-copy module

## Executable contracts

Focused verification covers ordered withholding/flushing, pass-through traffic, partial-synthesis
failure, interruption, warm-up consumption, stock Kokoro configuration, pipeline placement, and
playout completion.

## Verification

```text
.venv/bin/pytest -q
111 passed, 3 warnings in 75.50s

.venv/bin/ruff check src/ tests/
All checks passed!

.venv/bin/ruff format --check src/ tests/
32 files already formatted
```

`scripts/smoke_browser_shim.py` could not reach its audio assertion in this environment. Its first
run found no server at the script's hard-coded `localhost:3000`. With a temporary server supplied,
browser startup succeeded, but the pipecat child could not load the absent Whisper model because
the environment's SOCKS proxy lacks `socksio`; the CDP client was also routed through that proxy
and received HTTP 400. Both failures precede TTS generation. The executable pipeline and processor
contracts above are green; live browser/dogfood verification remains external to this environment.
