"""A throwaway localhost page for the smoke scripts to open.

The shim only installs its hooks on a SECURE origin — ``about:blank`` and plain
http on a non-localhost host are both skipped (``shim.js``). The smoke tests
therefore need an origin, but they do not need an *app*: they exercise the audio
plumbing, not any page behaviour. Serving one empty page here keeps them
self-contained and runnable anywhere, instead of depending on whatever happens
to be listening on a well-known port.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

_PAGE = b"<!doctype html><title>voicebox smoke</title><body></body>"


class _BlankPage(BaseHTTPRequestHandler):
    # do_GET is the name BaseHTTPRequestHandler dispatches to; not ours to choose.
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *args):
        """Stay quiet; the smoke scripts own the console."""


def serve_blank_page() -> str:
    """Serve an empty page on an ephemeral localhost port.

    The server runs on a daemon thread and needs no teardown: it dies with the
    script that started it.

    The URL names ``127.0.0.1`` rather than ``localhost`` so it matches the
    bound address exactly — on a dual-stack host ``localhost`` can resolve to
    ``::1`` first, which nothing is listening on. Chromium treats 127.0.0.1 as a
    secure origin, so ``navigator.mediaDevices`` still exists and the shim's
    hooks install (`shim.js:174`).

    Returns:
        The URL to open, e.g. ``http://127.0.0.1:41337``.

    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BlankPage)
    Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"
