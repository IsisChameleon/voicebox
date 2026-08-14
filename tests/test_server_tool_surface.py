"""Contracts of the MCP tool signatures themselves."""

import inspect

from voicebox.server import start_browser_session


def test_the_app_under_test_must_be_named():
    # voicebox exists to test YOUR app, so `url` has no sensible default. The
    # previous default (`http://localhost:3000`) outlived the app it pointed at
    # by three PRs and silently pointed every caller at nothing. A default here
    # is a decision to re-litigate, not to re-add.
    url = inspect.signature(start_browser_session).parameters["url"]

    assert url.default is inspect.Parameter.empty, (
        "start_browser_session(url) must stay required — see BUILDLOG and the "
        "walkthrough for generated-utterance-audio-buffer"
    )
