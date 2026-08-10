# T4 — Architecture diagram contract evidence

Success criteria: the living diagram names the supported browser attach command,
event-stream listen result, and artifact-producing stop behavior.

## Red

- Command, before diagram edits:

```bash
UV_CACHE_DIR=/tmp/voicebox-uv-cache uv run pytest -q tests/test_ci_docs_contract.py
```

- Three diagram assertions failed.
- The diagram called `playwright-cli` deprecated, described `listen()` as a
  transcript string, and omitted `record_dir` artifacts.

## Green

- Combined Continuous Integration (CI) and diagram contract: `9 passed`.
- Exact attach command: `playwright-cli attach --cdp http://localhost:9222`.
- Listen shape: `{events, cursor}`.
- Stop artifacts: `events.json`, `metrics.json`, and stereo WAV.

## Not covered

- No screenshot comparison; the change updates existing text nodes only.
