# T3 — Continuous integration contract evidence

Success criteria: use supported Python 3.11 in every workflow and require the
Python tests, shim test, and production-source type check on pull requests.

## Red

- Initial command, before workflow edits:

```bash
UV_CACHE_DIR=/tmp/voicebox-uv-cache uv run pytest -q tests/test_ci_docs_contract.py
```

- Result: `8 failed`.
- Failures covered three Python 3.10 workflow pins, missing `pytest`, missing
  Pyright, and three stale diagram contracts.
- Adding the shim-test contract separately produced `1 failed, 8 passed` until
  the Node test became a required build step.

## Green

- Contract test: `9 passed`.
- `uv run pyright src/voicebox`: `0 errors, 0 warnings, 0 informations`.
- `uv run ruff check`: passed.
- `uv run ruff format --check`: `32 files already formatted`.

## Not covered

- The workflow was not executed inside GitHub Actions before push.
- Pyright intentionally checks `src/voicebox`; 26 unrelated errors in historical
  probes, scripts, and tests remain outside this PR.
