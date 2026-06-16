so the CDP driving client is actually possibly playwright-cli used by Claude as well ? i.e. the start_brwoser_session starts the chrome, we hand that cdp port back to caller (ie.. claude) claude can then call    
  playwwright-cli to hook on that port? correct?                                                                                                                                                                           
  
  /btw by controlling client you mean, claude that is calling that MCP ?
  /btw so the CDP driving client is actually possibly playwright-cli used by Claude as well ? i.e. the start_brwoser_session starts the chrome, we hand that cdp port back to caller (ie.. claude) claude can then call pl…

    Correct — that's exactly the flow, and your mental model is right.

    Step by step:

    1. Claude calls start_browser_session (voicebox MCP tool). voicebox launches Chromium with the shim injected and --remote-debugging-port=9222, then returns {cdp_endpoint: "http://localhost:9222", audio_ws_url: ...}
    to Claude.
    2. Claude takes that cdp_endpoint and hands it to a Playwright client to attach to that running browser. That client does the login/navigate/click.
    3. Claude then uses voicebox's speak/listen for the audio half, and stop to tear down.

    The one nuance is which Playwright client. You said "playwright-cli" — that's the part to pin down:

    - @playwright/mcp (Microsoft's Playwright MCP server) — this is the clean fit. It has a --cdp-endpoint flag, so Claude can drive it as a second MCP server pointed at http://localhost:9222. This is the path the
    README now documents (npx @playwright/mcp@latest --cdp-endpoint=http://localhost:9222).
    - The deprecated microsoft/playwright-cli package — that one is codegen/record-only and not an attach-and-drive client, so it's not the right tool.
    - The newer playwright agent CLI has an attach --cdp=... command that can drive an existing browser, and your own connect_over_cdp script works too (that's what scripts/e2e_readme_call.py does).

    So: yes — voicebox owns the browser and just hands back the CDP port; Claude is the orchestrator that then connects a Playwright client (ideally @playwright/mcp --cdp-endpoint, or a connect_over_cdp script) onto
    that same port to drive the UI. voicebox deliberately ships no navigate/click tools, which is precisely why that second client is needed.

    One caveat worth remembering from earlier in the conversation: the CDP-attached client sees the page but its cookie read/save (storage_state()) hits the wrong context — which is why session reuse goes through
    voicebox's user_data_dir, not the Playwright client's storage_state.