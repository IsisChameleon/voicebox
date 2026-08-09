# Walkthrough — `feat/eval-fake-app`

*Status: **in progress**. Started 2026-08-09. Branched from `d07988b` (main, after PR #16 + #17
merged).*

Adds a self-contained fake web voice app ("Nova", a space-exploration trivia host) under
`tests/eval/fake_app/`, so anyone cloning the repo has a live conversational target for voicebox —
plus a ground-truth event log and a scripted-brain seam, making the fake a reference instrument for
future voicebox evals. Spec (the contract, all decisions resolved):
[`docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md`](../specs/2026-08-09-demo-voice-app-for-dogfooding.md).

The app is a plain pipecat pipeline behind pipecat's development runner (SmallWebRTC prebuilt
User Interface on `:7860`). It knows nothing about voicebox; ground truth goes to disk only.

## Status

| Phase | What it changes | Commits | Evidence | Done |
|---|---|---|---|---|
| **0** | Spec committed; walkthrough opened | `15d223b` | — | ✅ |
| **1** | `pyproject` `eval` extra; `tests/eval/fake_app/{bot,brain}.py` + `prompt.md`; ground-truth JSONL observer; app serves prebuilt UI on `:7860` | | | |
| **2** | Dogfood S2–S4 live with voicebox against `http://localhost:7860` (round-trip events, turn-taking metrics with `record_dir`, barge-in) | | | |
| **3** | `tests/eval/fake_app/README.md`; stale readme-app / `localhost:3000` reference cleanup in `README.md` + `CLAUDE.md` | | | |

Scenario S1 (fresh clone → real-mic conversation with Nova) is 🔴 manual — it needs Isabelle at
the microphone and is **not** claimed verified by this branch until she runs it.

## Try it

```bash
uv sync --extra eval
export ANTHROPIC_API_KEY=...   # from this repo's .env
uv run python tests/eval/fake_app/bot.py
# then open http://localhost:7860 and click Connect
```

## Not covered (running list)

- S1 real-mic run — pending Isabelle.
