# Phase 3 evidence — feat/eval-fake-app (docs only)

*2026-08-09. Commit `1d41070`.*

Spec: docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md, Execution plan Phase 3 +
Decision 3 ("Stale readme-app references in README / CLAUDE.md: cleaned up in the same
branch (Phase 3)").

## Grep BEFORE (verbatim)

```
$ grep -n -i -E "readme app|localhost:3000" README.md CLAUDE.md
README.md:38:- A browser-based voice app to point it at (e.g. a locally-running Next.js / Svelte app on `localhost:3000`)
README.md:151:{"name": "start_browser_session", "arguments": {"url": "http://localhost:3000"}}
README.md:206:1.  Claude → MCP                   tools/call start_browser_session(url="http://localhost:3000")
README.md:250:- **`RTCPeerConnection` wrap won't catch peer connections inside cross-origin iframes or Web Workers.** Not an issue for the readme app, but a real limitation for apps using Daily Prebuilt's `<DailyIframe>` (workaround: hook `<audio>` elements via `MutationObserver` + `captureStream()`).
CLAUDE.md:53:| `scripts/smoke_browser_shim.py` | Audio-path smoke test (no readme app needed). The reference for `connect_over_cdp` + reading `__voiceShim`. |
CLAUDE.md:169:needs a real browser; end-to-end behaviour against a running voice app on `localhost:3000` is
```

Exactly the six references named in the task; no other occurrences existed.

## Edits

1. **tests/eval/fake_app/README.md** (new, 64 lines) — quickstart (`uv sync --extra eval` →
   export `ANTHROPIC_API_KEY` or `VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted` →
   `uv run python tests/eval/fake_app/bot.py` → http://localhost:7860 → Connect); what it
   is / is not; env-var table per the spec's Configuration section; Nova's behavior
   ("explain in detail" barge-in target, `prompt.md` subject knob, scripted-mode 5 canned
   lines with line 3 long); ground-truth log (`temp/fake_app/ground_truth.jsonl`, record
   types, run-from-repo-root, why evals need it); pointing voicebox at it.
2. **README.md:38** — prerequisite bullet now points at the bundled fake app
   (`tests/eval/fake_app/`, `uv sync --extra eval`, `localhost:7860`) while keeping the
   "any `getUserMedia` + WebRTC app" framing.
3. **README.md:151** — example `start_browser_session` URL → `http://localhost:7860`.
4. **README.md:206** — walkthrough diagram URL → `http://localhost:7860`.
5. **README.md:250** — "Not an issue for the readme app" → "Not an issue for main-frame
   apps (like the bundled fake app)".
6. **README.md file map** — added one row for `tests/eval/fake_app/`.
7. **CLAUDE.md:53** — "(no readme app needed)" → "(no app needed)".
8. **CLAUDE.md file map** — added one row for `tests/eval/fake_app/` (after the
   smoke-script row).
9. **CLAUDE.md:169** — "against a running voice app on `localhost:3000`" → "in live
   dogfood sessions against the bundled fake app on `localhost:7860`
   (`tests/eval/fake_app/`)".

Left alone deliberately: `docs/specs/*`, `docs/design/*`, `scripts/*`, `src/*` (historical
docs and smoke scripts stay; `server.py`'s default URL out of scope). The Ember-flavored
example dialogue text in README.md's example session (lines ~154-180, "Hi Ember!", "Start
reading") was not part of the stale-reference list — only the URL changed.

## Verification

```
$ uv run ruff check tests/
warning: `incorrect-blank-line-before-class` (D203) and `no-blank-line-before-class` (D211) are incompatible. Ignoring `incorrect-blank-line-before-class`.
warning: `multi-line-summary-first-line` (D212) and `multi-line-summary-second-line` (D213) are incompatible. Ignoring `multi-line-summary-second-line`.
All checks passed!
```

(The two warnings are pre-existing ruff config notes, not findings.)

## Grep AFTER (verbatim — empty output, exit 1 = no matches)

```
$ grep -n -i -E "readme app|localhost:3000" README.md CLAUDE.md
$ echo "grep exit code: $?"
grep exit code: 1
```

## Files touched (git)

```
 M CLAUDE.md
 M README.md
?? tests/eval/fake_app/README.md
 CLAUDE.md | 9 +++++----
 README.md | 9 +++++----
```
