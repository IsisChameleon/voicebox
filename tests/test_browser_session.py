import pytest

from voicebox import browser_session


@pytest.fixture(autouse=True)
def no_leftover_browser():
    # Every test in this module must leave the module globals clean and no
    # Chromium behind, whether it launched one or stubbed the process out.
    yield
    browser_session.stop_browser()


def test_navigation_failure_propagates():
    # A1. Launches a real headless Chromium — that is the only honest way to
    # prove the child's navigation error reaches the parent. Port 1 binds
    # nothing; Chromium refuses it as an unsafe port, which is a navigation
    # failure like any other (a refused connection gives ERR_CONNECTION_REFUSED
    # through the same path).
    with pytest.raises(RuntimeError) as excinfo:
        browser_session.start_browser(
            url="http://localhost:1/",
            audio_ws_url="ws://localhost:9091",
            cdp_port=9333,
            headless=True,
            startup_timeout=60.0,
        )

    message = str(excinfo.value)
    assert "startup failed" in message
    # The child's own error text, not a generic timeout.
    assert "http://localhost:1/" in message


def test_navigation_failure_leaves_no_process(monkeypatch):
    # Criterion 6: teardown after a failed startup. Keeps a handle on the child
    # so we can prove it exited rather than being orphaned with a Chromium.
    children = []
    real_process = browser_session.multiprocessing.Process

    def recording_process(*args, **kwargs):
        child = real_process(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(browser_session.multiprocessing, "Process", recording_process)

    with pytest.raises(RuntimeError):
        browser_session.start_browser(
            url="http://localhost:1/",
            audio_ws_url="ws://localhost:9091",
            cdp_port=9334,
            headless=True,
            startup_timeout=60.0,
        )

    assert len(children) == 1
    assert not children[0].is_alive()
    assert browser_session._browser_process is None
    assert browser_session._stop_event is None


class _FakeProcess:
    """Stands in for the browser child: reports startup success, launches nothing."""

    def __init__(self, target=None, args=(), **kwargs):
        self._startup_queue = next(a for a in args if hasattr(a, "put"))
        self.ident = -1

    def start(self):
        self._startup_queue.put({"ok": True})

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass


def test_attach_hint_does_not_navigate(monkeypatch):
    # A2. No browser here — the fake child completes the startup handshake so
    # the real parent-side payload is what we assert on.
    monkeypatch.setattr(browser_session.multiprocessing, "Process", _FakeProcess)

    info = browser_session.start_browser(
        url="http://localhost:3000",
        audio_ws_url="ws://localhost:9091",
        cdp_port=9222,
        headless=True,
    )

    assert info["attach_hint"] == "playwright-cli attach --cdp http://localhost:9222"
    assert "close-all" not in info["attach_hint"]
    assert "PLAYWRIGHT_MCP_ISOLATED" not in info["attach_hint"]
    # The env-var recipe is gone from the payload entirely, not just the hint.
    assert "playwright_mcp_env" not in info
    assert "PLAYWRIGHT_MCP_ISOLATED" not in str(info)
