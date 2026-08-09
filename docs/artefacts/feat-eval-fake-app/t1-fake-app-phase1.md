# Phase 1 evidence — fake voice app ("Nova"), branch feat/eval-fake-app

*2026-08-09. Commit `38184f4`. Decision record: `BUILDLOG.md` D27.*

Spec: `docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md`, execution plan Phase 1.
Success criteria evidenced: `uv sync --extra eval` resolves; the app's code loads; the app starts
and serves the prebuilt UI on :7860 with `VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted` (no API key);
all repo quality gates green. S1-with-a-real-mic and S2–S4 are 🔴 live-only (Phase 2).

Files:

- `pyproject.toml` — new `[project.optional-dependencies]` `eval` extra (+5 lines)
- `tests/eval/fake_app/bot.py` (172 lines) — pipeline + runner entry + ground-truth observer
- `tests/eval/fake_app/brain.py` (121 lines) — provider factory + `ScriptedBrain`
- `tests/eval/fake_app/prompt.md` (15 lines) — Nova's system prompt
- `uv.lock` — re-locked, insertions only (169+, 0−)

## 1. `uv sync --extra eval` resolves cleanly

Command: `uv sync --extra eval` — tail of first run:

```
 + opencv-python==4.14.0.94
 - pyaml==26.2.1
 + pylibsrtp==1.0.0
 + pyopenssl==26.2.0
 - safetensors==0.7.0
 - torch==2.10.0
 - torchaudio==2.11.0
 - transformers==5.2.0
 - triton==3.6.0
 - voicebox==0.0.0.dev103 (from file:///home/isischameleon/src/voicebox)
 + voicebox==0.0.0.dev104 (from file:///home/isischameleon/src/voicebox)
```

Second run (idempotent):

```
Resolved 157 packages in 1ms
Checked 145 packages in 1ms
```

Packages the extra added to `uv.lock` (from `git diff uv.lock | grep '^+name'`):
`aioice, aiortc, anthropic, google-crc32c, ifaddr, opencv-python, pylibsrtp, pyopenssl`
— i.e. the spec-predicted aiortc (webrtc) + anthropic.

NOTE — pre-existing venv drift surfaced by the sync: the `-` removals (torch, torchaudio,
transformers, safetensors, triton, nvidia-*, pyaml) were ad-hoc venv packages NOT present in
`uv.lock` (the lock diff is insertions-only), so ANY `uv sync` would have pruned them. Verified
harmless for the core package:

```
$ uv run python -c "import voicebox.agent; print('voicebox.agent OK')"
2026-08-09 16:18:09.341 | INFO | pipecat:<module>:14 - ᓚᘏᗢ Pipecat 1.3.0 (Python 3.12.3 ...) ᓚᘏᗢ
voicebox.agent OK
$ grep -n "SmartTurn\|smart_turn" src/voicebox/agent.py
(no matches — nothing in src/ imports the torch-backed analyzer)
```

## 2. Imports resolve

```
$ uv run python -c "import tests.eval.fake_app.bot as m; print('import OK:', m.__file__)"
2026-08-09 16:18:18.179 | INFO | pipecat:<module>:14 - ᓚᘏᗢ Pipecat 1.3.0 (Python 3.12.3 ...) ᓚᘏᗢ
2026-08-09 16:18:21.144 | DEBUG | pipecat.transports.smallwebrtc.connection:<module>:67 - [SCTP] USERDATA_MAX_LENGTH set to 1100
import OK: /home/isischameleon/src/voicebox/tests/eval/fake_app/bot.py
```

(Works both as `import tests.eval.fake_app.bot` — namespace packages, no `__init__.py` added under
`tests/` so pytest collection is untouched — and as a script, which is how the runner runs it.)

## 3. App starts and serves the prebuilt UI (scripted brain, no API key)

```
$ VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted uv run python tests/eval/fake_app/bot.py   # backgrounded
```

Startup log (verbatim):

```
INFO:     Started server process [1678552]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://localhost:7860 (Press CTRL+C to quit)

🚀 Bot ready!
   → Open: http://localhost:7860
   → Enabled transports: webrtc, telephony, websocket
   → Disabled transports: daily (install pipecat-ai[daily])

Looking for dist directory at: /home/isischameleon/src/voicebox/.venv/lib/python3.12/site-packages/pipecat_ai_prebuilt/client/dist
```

`GET /` returns **307** (the runner redirects to the prebuilt client), following it:

```
$ curl -s -o /dev/null -w "final %{http_code} url %{url_effective}\n" -L http://localhost:7860/
final 200 url http://localhost:7860/client/
$ curl -sL http://localhost:7860/ | head -20
<!DOCTYPE html>
<html lang="en">

<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Pipecat UI</title>
  <link rel="icon" href="./favicon.svg" type="image/svg+xml">
  <script type="module" crossorigin src="./assets/index-DLCNnfbP.js"></script>
  <link rel="stylesheet" crossorigin href="./assets/index-Dh0JBWtF.css">
</head>

<body>
  <div id="root"></div>
</body>

</html>
```

App then killed; confirmed down:

```
$ curl -s -o /dev/null --max-time 2 http://localhost:7860/ && echo "port open" || echo "port 7860 closed"
port 7860 closed
```

(No `temp/fake_app/ground_truth.jsonl` was written during this probe — expected: `bot()` and its
observer only run when a WebRTC client connects, which is Phase 2's live dogfood.)

## 4. Quality gates

```
$ uv run pytest -q
92 passed, 3 warnings in 63.20s (0:01:03)

$ uv run ruff check src/ tests/
All checks passed!

$ uv run ruff format src/ tests/
1 file reformatted, 27 files left unchanged      # brain.py: final raise wrapped to 100 cols
$ uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
All checks passed!
28 files already formatted

$ uv run pyright tests/eval/fake_app/
0 errors, 0 warnings, 0 informations

$ uv run pyright src/
  src/voicebox/agent.py:543:42 - error: "start_recording" is not a known attribute of "None" (reportOptionalMemberAccess)
  src/voicebox/browser_session.py:39:23 - error: Variable not allowed in type expression (reportInvalidTypeForm)
2 errors, 0 warnings, 0 informations
```

The two `pyright src/` errors are PRE-EXISTING: this diff contains zero `src/` changes
(`git status`: only `pyproject.toml`, `uv.lock`, `tests/eval/`), and both errors are unrelated to
the dependency changes (closure over `self._audio_buffer` defeating None-narrowing at
`agent.py:543`; `multiprocessing.Event` — a factory function — used in a type annotation at
`browser_session.py:39`). Not fixed here: `src/` is out of scope for this task ("Do NOT modify
anything under src/voicebox/").

## 5. Brain factory behavior (key-less environment)

```
$ uv run python -c "...create_brain()..."          # provider unset → anthropic default, no key
SystemExit: ANTHROPIC_API_KEY is required for VOICEBOX_FAKE_APP_LLM_PROVIDER=anthropic (the default). Export it, or use VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted for a no-key canned brain.

$ VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted uv run python -c "...create_brain()..."
scripted brain: ScriptedBrain
```

## Not covered (deferred to Phase 2 live dogfood)

- S1 real-mic conversation via the prebuilt UI (needs a human + mic).
- S2–S4 voicebox round-trip, turn-taking metrics, barge-in.
- `ground_truth.jsonl` contents (observer only runs once a client connects).
- `anthropic`/`openai` provider paths at runtime (no API keys in this environment by design;
  the missing-key failure message is code-reviewable in `brain.py:create_brain`).
