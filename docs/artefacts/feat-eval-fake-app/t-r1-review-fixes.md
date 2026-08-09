# R1 evidence — code-review fixes on the fake app

*2026-08-09. Commit `a328c81`. Review: `/code-review medium` over `main..HEAD` (8 verified
findings).*

Success criteria: every review finding either fixed with fresh verification, or explicitly
skipped with the reason recorded. Spec under review:
`docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md`.

## Fixed (6)

| Finding | Fix |
|---|---|
| `SystemExit` for missing key fired per-connection, not at startup (contradicting `brain.py`'s docstring) | `create_brain()` called under `__main__` before `main()` — `bot.py:183` |
| `load_dotenv(override=True)` let `.env` silently override the documented shell-export quickstart | `load_dotenv()` (no override) — shell env wins |
| `sys.path.insert` created a duplicate top-level `brain` module beside `tests.eval.fake_app.brain` (verified empirically by the reviewer: two distinct module objects) | insert dropped; script mode imports the sibling bare via `sys.path[0]` |
| Ground-truth file opened in `__init__`, never closed — one leaked fd per connect/disconnect cycle | open-append-close per record in `_write` (a few writes per turn) |
| `GROUND_TRUTH_PATH` was CWD-relative — launching outside the repo root scattered stray `temp/` trees | anchored: `Path(__file__).resolve().parents[3] / "temp/fake_app/ground_truth.jsonl"` |
| `interrupted` records indistinguishable from pipecat's per-turn `InterruptionFrame` broadcast — every future eval scorer would re-implement span filtering | observer tracks `_bot_speaking`; records carry `during_bot_speech: true/false` |

## Skipped, with reasons (2)

- **Per-connection model construction blocking the event loop on first connect.** pipecat's own
  runner examples construct services inside the per-connection `bot()` — keeping that shape keeps
  the fake representative of a real third-party app (a spec goal), and Phase 2 ran three live
  sessions through it without a connect timeout. Cost of fixing: module-level model state and a
  bigger diff for a single-session dev fixture. Revisit if cold-connect timeouts appear.
- **`server.py:126` default `url="http://localhost:3000"` now points at nothing.** `src/` is out
  of scope for this branch by explicit constraint, and changing an MCP tool signature has a
  client-cache consequence (tool descriptions are cached at connect). Surfaced to Isabelle as a
  follow-up decision: change the default to `:7860`, or make `url` required.

## Verification (verbatim)

Fail-fast at startup — no key, default provider:

```
$ uv run python tests/eval/fake_app/bot.py 2>&1 | tail -2
2026-08-09 17:06:01.076 | DEBUG | pipecat.transports.smallwebrtc.connection:<module>:67 - [SCTP] USERDATA_MAX_LENGTH set to 1100
ANTHROPIC_API_KEY is required for VOICEBOX_FAKE_APP_LLM_PROVIDER=anthropic (the default). Export it, or use VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted for a no-key canned brain.

$ VOICEBOX_FAKE_APP_LLM_PROVIDER=bogus uv run python tests/eval/fake_app/bot.py 2>&1 | tail -1
Unknown VOICEBOX_FAKE_APP_LLM_PROVIDER='bogus' (expected anthropic | openai | scripted)
```

Scripted boot from a foreign working directory (`/tmp`) — bare `brain` import and repo-root
anchor both hold:

```
$ cd /tmp && VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted uv run --project ~/src/voicebox \
    python ~/src/voicebox/tests/eval/fake_app/bot.py &   # then:
GET / -> 200          # curl -L http://localhost:7860/
grep -c "Bot ready" r1-boot.log → 1
7860 closed           # after pkill
no stray /tmp/temp    # no artifacts outside the repo's temp/
```

Gates after fixes:

```
$ uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
All checks passed!  /  28 files already formatted
$ uv run pyright tests/eval/fake_app/   → exit 0, 0 errors
$ uv run pytest -q                      → 92 passed
```

## Not covered

- The fixed `during_bot_speech` flag and per-record file appends are not yet exercised by a live
  session (Phase 2 ran against the pre-R1 observer); next dogfood run covers them incidentally.
- The two skipped findings above.
