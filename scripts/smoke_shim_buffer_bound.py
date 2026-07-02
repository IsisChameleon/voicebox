"""Smoke test for T6: the shim's pre-connection inbound buffer is bounded.

Exercises ``pendingInbound`` in isolation — no pipecat child, no Kokoro/Whisper
models (Kokoro's model host 403s in this sandbox; this test needs neither).
It only needs Chromium (via Playwright) and a tiny in-test WebSocket server
that plays the part of the pipecat audio child:

  1. Serve a minimal page on ``http://localhost:<port>`` (a real HTTP origin
     so the shim's secure-context check passes and Hook 1 installs) with the
     shim injected via ``add_init_script``, pointed at our fake WS server.
  2. Once the shim's WS connects, push more fake PCM audio than
     ``PENDING_INBOUND_MAX_SECS`` (shim.js) worth of frames — i.e. more than
     the shim is willing to buffer before any ``getUserMedia`` call.
  3. Assert the shim's drop counters
     (``window.__voiceShim.droppedInboundFrames/droppedInboundSamples``) rose
     — proving the bound is enforced and overflow frames were evicted rather
     than accumulating forever.
  4. Call ``navigator.mediaDevices.getUserMedia({audio: true})`` from the page
     and assert the backlog handed to the new synthetic mic track
     (``lastMicHandoffSamples``) is at most the documented bound
     (``pendingInboundMaxSamples``) and strictly less than what was sent —
     i.e. the track starts near-live, not replaying the whole burst.

Run: ``uv run python scripts/smoke_shim_buffer_bound.py``
"""

import asyncio
import http.server
import os
import sys
import tempfile
import threading
from pathlib import Path

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")

SHIM_PATH = Path(__file__).resolve().parent.parent / "src" / "voicebox" / "shim.js"

HTTP_PORT = 8079
WS_PORT = 8081

MIC_RATE = 48000
# Chunk size for our fake inbound frames: 100 ms of mono 16-bit PCM.
CHUNK_SECS = 0.1
CHUNK_SAMPLES = int(MIC_RATE * CHUNK_SECS)
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 16-bit signed PCM

# Send enough chunks to cover ~2x shim.js's PENDING_INBOUND_MAX_SECS (3s), so
# we exercise real overflow/eviction rather than just filling the buffer.
BURST_SECS = 6.0
NUM_CHUNKS = int(BURST_SECS / CHUNK_SECS)


def _make_page_dir() -> Path:
    """Write a minimal static page to a temp dir for the HTTP server to serve."""
    d = Path(tempfile.mkdtemp(prefix="voicebox-shim-smoke-"))
    (d / "index.html").write_text(
        "<!doctype html><title>shim buffer bound smoke</title><body>ready</body>",
        encoding="utf-8",
    )
    return d


def _start_http_server(directory: Path, port: int) -> http.server.ThreadingHTTPServer:
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args, directory=str(directory), **kwargs
    )
    httpd = http.server.ThreadingHTTPServer(("localhost", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


async def _ws_handler(websocket):
    """Play the pipecat side: on connect, blast a burst of fake PCM frames."""
    logger.info(
        f"fake pipecat: client connected, sending {NUM_CHUNKS} chunks "
        f"({BURST_SECS}s of audio) before any getUserMedia() call"
    )
    silence_chunk = bytes(CHUNK_BYTES)  # all-zero PCM is fine; we only test buffering
    for _ in range(NUM_CHUNKS):
        await websocket.send(silence_chunk)
    logger.info("fake pipecat: burst sent")
    # Keep the connection open so the shim doesn't spin into reconnects.
    try:
        await websocket.wait_closed()
    except Exception:
        pass


async def main():
    """Run the shim inbound-buffer-bound smoke test."""
    import websockets

    page_dir = _make_page_dir()
    httpd = _start_http_server(page_dir, HTTP_PORT)
    logger.info(f"=== serving test page on http://localhost:{HTTP_PORT} ===")

    ws_server = await websockets.serve(_ws_handler, "localhost", WS_PORT)
    logger.info(f"=== fake pipecat WS server listening on ws://localhost:{WS_PORT} ===")

    ok = True
    try:
        from playwright.async_api import async_playwright

        shim_src = SHIM_PATH.read_text(encoding="utf-8")
        init_script = f"window.__VOICE_SHIM_WS_URL__ = 'ws://localhost:{WS_PORT}';\n{shim_src}"

        async with async_playwright() as p:
            launch_kwargs = {
                "headless": True,
                "args": ["--use-fake-ui-for-media-stream", "--no-first-run"],
            }
            try:
                browser = await p.chromium.launch(**launch_kwargs)
            except Exception as e:
                # Sandbox note: this environment's installed playwright pip
                # package (driver) is newer than the pre-fetched browser
                # revision under PLAYWRIGHT_BROWSERS_PATH, so the default
                # headless-shell executable it wants isn't present. Fall back
                # to the full Chromium binary that IS present (still runs
                # headless via the --headless launch arg Playwright adds).
                fallback = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")) / "chromium"
                if not fallback.exists():
                    raise
                logger.warning(f"default chromium launch failed ({e}); retrying with {fallback}")
                browser = await p.chromium.launch(**launch_kwargs, executable_path=str(fallback))
            context = await browser.new_context(permissions=["microphone"])
            await context.add_init_script(init_script)
            page = await context.new_page()
            page.on("console", lambda m: logger.debug(f"[page console] {m.text}"))
            page.on("pageerror", lambda e: logger.warning(f"[page error] {e}"))

            await page.goto(f"http://localhost:{HTTP_PORT}/", wait_until="domcontentloaded")

            logger.info("=== waiting for shim WS to connect ===")
            for _ in range(50):
                ready = await page.evaluate("() => !!window.__voiceShim?.wsReady")
                if ready:
                    break
                await asyncio.sleep(0.1)
            else:
                logger.error("✗ shim WS never connected")
                await browser.close()
                return 1

            logger.info("=== waiting for the burst to be fully received ===")
            for _ in range(100):
                inbound = await page.evaluate("() => window.__voiceShim?.inboundChunks || 0")
                if inbound >= NUM_CHUNKS:
                    break
                await asyncio.sleep(0.1)
            diag_pre = await page.evaluate(
                "() => ({"
                " inboundChunks: window.__voiceShim.inboundChunks,"
                " droppedInboundFrames: window.__voiceShim.droppedInboundFrames,"
                " droppedInboundSamples: window.__voiceShim.droppedInboundSamples,"
                " pendingInboundMaxSamples: window.__voiceShim.pendingInboundMaxSamples,"
                " errors: window.__voiceShim.errors,"
                "})"
            )
            logger.info(f"shim state after burst (pre-getUserMedia): {diag_pre}")

            if diag_pre["inboundChunks"] < NUM_CHUNKS:
                logger.error(
                    f"✗ only {diag_pre['inboundChunks']}/{NUM_CHUNKS} chunks arrived "
                    "— burst didn't fully land, results below are not conclusive"
                )
                ok = False

            if diag_pre["droppedInboundFrames"] > 0 and diag_pre["droppedInboundSamples"] > 0:
                logger.success(
                    f"✓ drop counters rose: droppedInboundFrames="
                    f"{diag_pre['droppedInboundFrames']}, droppedInboundSamples="
                    f"{diag_pre['droppedInboundSamples']} "
                    f"({diag_pre['droppedInboundSamples'] / MIC_RATE:.2f}s of audio dropped)"
                )
            else:
                logger.error("✗ drop counters did not rise — buffer bound not enforced")
                ok = False

            if diag_pre["errors"]:
                logger.error(f"✗ shim recorded errors during buffering: {diag_pre['errors']}")
                ok = False
            else:
                logger.success(
                    "✓ no shim errors during buffering/eviction (AudioData.close() didn't throw)"
                )

            logger.info("=== calling getUserMedia({audio: true}) from the page ===")
            await page.evaluate(
                "async () => { await navigator.mediaDevices.getUserMedia({audio: true}); }"
            )

            diag_post = await page.evaluate(
                "() => ({"
                " lastMicHandoffFrames: window.__voiceShim.lastMicHandoffFrames,"
                " lastMicHandoffSamples: window.__voiceShim.lastMicHandoffSamples,"
                " pendingInboundMaxSamples: window.__voiceShim.pendingInboundMaxSamples,"
                " micTrackCount: window.__voiceShim.micTrackCount,"
                "})"
            )
            logger.info(f"shim state after getUserMedia: {diag_post}")

            total_samples_sent = NUM_CHUNKS * CHUNK_SAMPLES
            bound = diag_post["pendingInboundMaxSamples"]
            handoff = diag_post["lastMicHandoffSamples"]

            if handoff <= bound:
                logger.success(
                    f"✓ backlog handed to new mic track ({handoff} samples, "
                    f"{handoff / MIC_RATE:.2f}s) is at most the bound "
                    f"({bound} samples, {bound / MIC_RATE:.2f}s)"
                )
            else:
                logger.error(
                    f"✗ backlog handed to new mic track ({handoff} samples) EXCEEDS "
                    f"the bound ({bound} samples)"
                )
                ok = False

            if handoff < total_samples_sent:
                logger.success(
                    f"✓ mic track started near-live: handed off {handoff} samples, "
                    f"not the full {total_samples_sent}-sample backlog "
                    "(i.e. it did NOT replay everything sent)"
                )
            else:
                logger.error("✗ mic track replayed the entire backlog — drop-oldest not effective")
                ok = False

            await browser.close()
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        httpd.shutdown()

    if ok:
        logger.success("=== ALL CHECKS PASSED ===")
        return 0
    else:
        logger.error("=== SOME CHECKS FAILED ===")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
