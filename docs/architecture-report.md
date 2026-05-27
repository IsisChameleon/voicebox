# Architecture report — browser-shim audio path

This is the long version of the design write-up: the *why* behind every
choice, the concepts a reader needs to follow along, the alternatives that
were tried or considered, and the assumptions that hold the whole thing up.
It pairs with the [README](../README.md) (which is more reference-style)
and the test artifacts under [`artifacts/e2e_readme_call/`](../artifacts/e2e_readme_call/).

---

## 1. What we built, in one paragraph

`this MCP server` already had one mode: it joins a Daily WebRTC room
*directly* as a synthetic peer to talk to the Toocan bot — the existing
`start_call` flow. The new mode (`start_browser_session`) lets Claude
drive a *real client UI* — for instance the EmberTales (readme) app at
`localhost:3000` — via Playwright, while the MCP server's audio (Kokoro
TTS in, Whisper STT out) is invisibly spliced into that browser's
microphone and remote-audio paths. The browser doesn't know anything has
been intercepted; the app's own Pipecat client / Daily transport runs
unmodified.

End-to-end: Claude logs in via Playwright, picks a book, clicks the
"Start reading" button. The Ember bot greets the user; the MCP server's
`listen()` returns the transcript. Claude calls `speak("Hi Ember…")`;
the bot hears Kokoro's audio as if it came from the user's mic.
Eventually Claude clicks the red-cross (or says "goodbye"); the call
tears down.

See the verified end-to-end script: [`scripts/e2e_readme_call.py`](../scripts/e2e_readme_call.py).

---

## 2. Concepts a reader needs to know

Two of these are the load-bearing ones; the rest are background. If you
already know Web Audio / WebRTC, skim to §3.

**`navigator.mediaDevices.getUserMedia({audio: true})`** — the browser
API a web app calls to get a microphone stream. It returns a
`MediaStream` containing one or more `MediaStreamTrack` objects. The
app then either plays this track, sends it to a peer over WebRTC, or
both. Replacing this function is the single biggest lever for stealing
the page's microphone.

**`RTCPeerConnection`** — the browser's primary WebRTC API. It
negotiates SRTP audio/video between two endpoints (the page and a
remote peer — in this case Daily's media server, which then bridges to
the Ember bot). When a remote track arrives, the peer connection fires
a `track` event with the new `MediaStreamTrack`. Tapping that event is
the single biggest lever for *capturing* the bot's voice.

**`MediaStreamTrack`** — the unit of streaming media in the browser. An
audio track can be read from (by `MediaStreamTrackProcessor`) or
written to (by `MediaStreamTrackGenerator`). These two APIs let you
treat a track as a `ReadableStream` / `WritableStream` of `AudioData`
frames — same idea as a Unix pipe.

**`AudioData` (WebCodecs)** — a chunk of decoded audio samples with
metadata (`sampleRate`, `numberOfChannels`, `numberOfFrames`). The unit
that flows in and out of the track-processor / track-generator pair.

**Playwright `addInitScript` / CDP `Page.addScriptToEvaluateOnNewDocument`**
— register a JS snippet that the browser will run *before any page
script* on every navigation. This is what lets us install the shim
*ahead of* the readme app's own code; otherwise we'd race the app's
caching of `getUserMedia` and `RTCPeerConnection`.

**Pipecat `WebsocketServerTransport`** — a transport that runs a
WebSocket server and exchanges audio/text frames with a single client
over it. We use this on the MCP server side to talk to the browser
shim.

**Pipecat `FrameSerializer`** — wire-format glue. `serialize(frame)`
turns Pipecat frames into bytes/strings; `deserialize(data)` does the
inverse. The default for `WebsocketServerTransport` is
`ProtobufFrameSerializer`, which is overkill for our localhost-only
PCM stream — we wrote a 20-line replacement: [`raw_pcm_serializer.py`](../src/pipecat_mcp_server/raw_pcm_serializer.py).

**Secure context** — browser security concept. APIs like
`getUserMedia`, `MediaStreamTrackGenerator`, and `crypto.subtle` only
work in secure contexts. HTTPS qualifies; `localhost` qualifies; HTTP
on other hosts doesn't; `about:blank` doesn't. The shim defends against
this so it doesn't crash on non-secure pages.

---

## 3. The story — how we got here

The conversation started with the user asking a different question
("what would it take to have a Daily room with a browser on one side
and pipecat on the other?"). The architecture we ended up with is
deliberately different from that.

### First architectural reflex (rejected): join Daily as an additional peer

The first instinct, given the existing `start_call` Toocan flow, was:
"if the readme app opens a Daily room with the bot, just have the MCP
server's pipecat join that same Daily room as a third peer."

This was rejected primarily because **it only works for apps that use
Daily**. The goal is to test *any* in-browser voice agent — and there
are many ways voice agents move audio in the browser today:

- **Daily** (what the readme app and Toocan happen to use)
- **LiveKit** (different SDK, different signalling, same underlying
  `RTCPeerConnection`)
- **Plain `RTCPeerConnection`** straight to a custom server (no
  Daily/LiveKit SDK at all — e.g. apps built on Pipecat's
  `SmallWebRTCTransport`)
- **WebSocket-streamed Opus or PCM** decoded into an `<audio>`
  element or a Web Audio graph (no WebRTC at all)
- **HTTP-streamed TTS** played by a hidden `<audio>` element
- **In-browser TTS** (e.g. Kokoro / Piper compiled to WASM running
  in the page, with no network audio at all)

Joining Daily as a 3rd peer would solve exactly the Daily case and
nothing else. The shim approach is transport-agnostic: it hooks the
*browser APIs* (`getUserMedia`, `RTCPeerConnection`, and — if we ever
add it — `HTMLMediaElement.captureStream()` + `AudioContext`), which
sit *below* whichever SDK or protocol the app chose. Whether the bot
audio is encoded Opus from a Daily peer, raw PCM from a WebSocket, or
samples emitted directly by an in-browser WASM model, the shim's
capture points see it.

The secondary reasons that also pushed against the 3rd-peer approach:

- Daily rooms in the readme app are 1:1 (user + bot). A third peer
  competes for the "user" slot and the bot would hear two
  microphones at once.
- The MCP server doesn't have visibility into the room URL the
  readme app uses — that's negotiated server-side between
  `localhost:7860` (Ember's `/start` endpoint) and the browser.
  Intercepting it would mean either patching the readme app or
  hijacking its `/start` response, both intrusive.

But the *primary* reason is generality. The shim hooks *the page's
mic and speaker abstraction*, not any one transport.

### Second reflex (rejected): bypass the browser entirely

"If we know the readme app's `/start` endpoint, we can call it
ourselves from the MCP server, get the room URL + token, and have
pipecat join Daily directly — just like the Toocan flow."

Also wrong:

- The user's stated goal was specifically to drive the *browser UI*
  end-to-end: login → book selection → click "Start reading". That
  exercises the full app, not just the audio path.
- It would only work for apps whose call-start endpoint is
  reachable without browser-context cookies — fragile and
  app-specific.

### The chosen architecture: stay outside the audio path

The audio is already in the browser. The browser already calls
`getUserMedia`. The browser already creates an `RTCPeerConnection` to
Daily. **All we need to do is splice into those two existing APIs —
not replace them.** From the page's perspective there is no MCP
server; it just got a slightly weird microphone and someone is
silently listening to the remote audio.

Concretely:

> **the simplest thing that holds: pipecat unchanged STT/TTS/VAD, swap
> the transport, inject a 200-line shim, let any Playwright client
> drive the UI. The MCP server has zero knowledge of Daily, the
> readme app, or how the bot is hosted — it only sees a WebSocket
> carrying PCM.** — final report, prior turn

This is *invariant* to how the app does WebRTC: it works for Daily,
LiveKit, plain `RTCPeerConnection`, whatever. As long as the app uses
the standard browser APIs, the shim catches them.

---

## 4. Architecture in one picture

```
   ┌─ this MCP server (parent, FastMCP on :9090) ─┐
   │   start_browser_session()                  │
   │   speak() / listen() / stop()              │
   └───────┬────────────────────────┬───────────┘
           │                        │
           │ multiprocessing.Queue  │ multiprocessing
           │ (IPC for tool calls)   │  .Process spawn
           ▼                        ▼
   ┌── pipecat child ──┐     ┌── playwright child ──┐
   │  WebsocketServer  │     │   Chromium + shim    │
   │  Transport :9091  │     │   CDP on :9222       │
   │  Whisper STT      │     │   page at <url>      │
   │  Kokoro TTS       │     └──────────┬───────────┘
   │  VAD/SmartTurn    │                │
   └──────┬────────────┘                │
          │ raw 16-bit LE mono PCM      │ getUserMedia(audio)
          │ over WebSocket              │ → MediaStreamTrack
          │                             │   from shim
          ▼                             ▼
                ┌─── injected shim.js ───┐
                │  ─ ws://localhost:9091 │
                │  ─ override getUserMedia
                │  ─ wrap RTCPeerConnection
                └─────┬──────────┬───────┘
                      │          │
                      │          │ track event (audio in)
                      │          │ → MediaStreamTrackProcessor
                      │          │ → WS → pipecat → Whisper
                      │          ▼
                MediaStreamTrack  ──► page's RTCPeerConnection
                                       ──► Daily ──► Ember bot
```

The MCP server has **two** child processes by design:

1. **pipecat child** — owns the Pipecat pipeline (STT/TTS/VAD) and the
   WebSocket server. Same multiprocessing pattern as the existing
   Toocan flow. Isolates Pipecat's asyncio loop and audio threads
   from FastMCP's HTTP request path.
2. **playwright child** — owns the browser lifecycle. Keeps Playwright
   off the main event loop and lets the browser stay alive
   independently of MCP tool calls.

Both children are managed by their respective lifecycle helpers:
[`agent_ipc.py`](../src/pipecat_mcp_server/agent_ipc.py) for pipecat,
[`browser_session.py`](../src/pipecat_mcp_server/browser_session.py)
for the browser.

---

## 5. The code walked through

### 5.1 Two modes, one `create_agent`

The branch from one mode to the other is a single type-dispatch in
[`agent.py:296-345`](../src/pipecat_mcp_server/agent.py):

```python
async def create_agent(runner_args: RunnerArguments) -> PipecatMCPAgent:
    if isinstance(runner_args, DailyRunnerArguments):
        # bot mode — existing Toocan flow, unchanged
        transport_params = {
            "daily": lambda: DailyParams(
                audio_in_enabled=True, audio_out_enabled=True,
                video_out_enabled=True, audio_in_filter=RNNoiseFilter(),
            )
        }
        transport = await create_transport(runner_args, transport_params)
        return PipecatMCPAgent(transport)

    if isinstance(runner_args, BrowserShimRunnerArguments):
        # browser mode — new
        params = WebsocketServerParams(
            audio_in_enabled=True, audio_out_enabled=True,
            audio_in_sample_rate=runner_args.sample_rate,
            audio_out_sample_rate=runner_args.sample_rate,
            add_wav_header=False,
            serializer=RawPCMSerializer(sample_rate=runner_args.sample_rate),
        )
        transport = WebsocketServerTransport(
            params=params, host=runner_args.host, port=runner_args.port,
        )
        return PipecatMCPAgent(transport, record_dir=runner_args.record_dir)

    raise ValueError(f"Unsupported runner_args type: …")
```

This is the entire architectural seam. STT, TTS, VAD, pipeline shape,
end-of-turn detection — none of it differs between modes. Only the
*transport* (how bytes get in and out) and the per-transport
configuration knobs change.

### 5.2 The wire format: 20-line `RawPCMSerializer`

Pipecat ships `ProtobufFrameSerializer` for WebSocket transports. That
would require us to bundle `protobufjs` in the browser shim, which
adds 50KB of JS for a feature we don't need (we only ever exchange
raw audio). Custom serializer — [`raw_pcm_serializer.py`](../src/pipecat_mcp_server/raw_pcm_serializer.py):

```python
class RawPCMSerializer(FrameSerializer):
    def __init__(self, sample_rate: int = 48000, num_channels: int = 1):
        super().__init__()
        self._sample_rate = sample_rate
        self._num_channels = num_channels

    async def serialize(self, frame: Frame) -> Optional[bytes]:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio   # raw 16-bit LE PCM mono
        return None

    async def deserialize(self, data) -> Optional[Frame]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            return None
        return InputAudioRawFrame(
            audio=bytes(data),
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
        )
```

Why this is safe: Pipecat resamples internally between transport rate
and pipeline rate (Whisper wants 16 kHz, Kokoro outputs 24 kHz; we
configure the transport at 48 kHz to match Chrome's WebRTC).

### 5.3 The shim: the load-bearing piece

[`shim.js`](../src/pipecat_mcp_server/shim.js) is injected via
`addInitScript`, so it runs strictly before any page code.

#### Diagnostics first, hooks second

The first thing the shim does is install `window.__voiceShim` with all
counters and flags. Then each risky hook is wrapped in try/catch. This
matters because:

- We want diagnostics even on pages where one or more APIs are
  missing (about:blank, http: origins that aren't localhost).
- A throw inside the IIFE before `__qzShim` is assigned would be
  invisible — we'd see "shim not installed" and have no idea why.

This took one iteration of debugging to discover: the very first
shim crashed on `about:blank` reading
`navigator.mediaDevices.getUserMedia` (because `mediaDevices` is
`undefined` outside a secure context), which left `__qzShim` unset and
gave us no diagnostics. The current shim ([`shim.js:33-49`](../src/pipecat_mcp_server/shim.js))
sets the diagnostics first.

#### Hook 1 — the synthetic microphone

```js
// shim.js:107-122
const origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async (constraints) => {
  if (!constraints || !constraints.audio) return origGUM(constraints);
  const audioStream = makeSyntheticMicStream();
  if (!constraints.video) return audioStream;
  const videoOnly = await origGUM({ video: constraints.video });
  videoOnly.getAudioTracks().forEach((t) => t.stop());
  return new MediaStream([
    ...audioStream.getAudioTracks(),
    ...videoOnly.getVideoTracks(),
  ]);
};
```

`makeSyntheticMicStream()` returns a `MediaStream` whose only audio
track is a `MediaStreamTrackGenerator` that we write into. When
inbound PCM frames arrive on the WebSocket from pipecat (Kokoro
output), we convert Int16 → Float32 and write an `AudioData` object to
the generator. The page's `RTCPeerConnection` consumes the generator
just like a real mic; Chrome encodes it Opus and ships to Daily; Daily
forwards to Ember.

The video preservation is defensive: if the page asks for `{audio,
video}`, we still want a real camera (in case anyone ever tests
camera-on flows), but we replace just the audio.

#### Hook 2 — capturing the bot's voice

```js
// shim.js:135-184
class WrappedRTCPeerConnection extends OrigRTCPeerConnection {
  constructor(...args) {
    super(...args);
    this.addEventListener('track', (ev) => {
      const track = ev.track;
      if (!track || track.kind !== 'audio') return;
      const processor = new MediaStreamTrackProcessor({ track });
      const reader = processor.readable.getReader();
      (async () => {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          // Float32 → Int16 LE, send raw bytes.
          const float32 = new Float32Array(value.numberOfFrames);
          value.copyTo(float32, { planeIndex: 0 });
          const int16 = new Int16Array(value.numberOfFrames);
          for (let i = 0; i < value.numberOfFrames; i++) {
            const s = Math.max(-1, Math.min(1, float32[i]));
            int16[i] = s < 0 ? s * 32768 : s * 32767;
          }
          ws.send(int16.buffer);
          value.close();
        }
      })();
    });
  }
}
window.RTCPeerConnection = WrappedRTCPeerConnection;
```

Class extension (not a `Proxy` wrapper) so that `new` semantics,
`instanceof` checks, and prototype chain all keep working. Daily JS's
internal code checks `RTCPeerConnection.prototype` and similar; class
extension is the only wrapper form that preserves all of them
transparently.

The asynchronous IIFE inside the `track` event is a generator-pulling
loop. `MediaStreamTrackProcessor.readable` is a `ReadableStream` whose
chunks are `AudioData` objects with `Float32` samples; we convert,
ship, and call `value.close()` (mandatory — these are reference-counted
GPU/WebCodecs objects, leaking them stalls the pipeline).

### 5.4 The launcher: `browser_session.py`

[`browser_session.py`](../src/pipecat_mcp_server/browser_session.py)
runs in its own subprocess. The critical part of `_run_browser_async`:

```python
chromium_args = [
    f"--remote-debugging-port={cdp_port}",        # CDP for external attach
    "--use-fake-ui-for-media-stream",             # auto-grant mic
    "--no-first-run", "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
]
async with async_playwright() as p:
    browser = await p.chromium.launch(headless=headless, args=chromium_args)
    context = await browser.new_context(permissions=["microphone"])
    await context.add_init_script(init_script)   # ← shim.js
    page = await context.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    ready_event.set()                            # → MCP tool returns
    while not stop_event.is_set():
        await asyncio.sleep(0.5)
```

`--remote-debugging-port=9222` is the key: it exposes Chromium's CDP
over WebSocket so an *external* Playwright client (or `@playwright/mcp`
or just `chromium.connect_over_cdp` from a test script) can attach and
drive the page from outside this process. Without this we'd have to
build our own click/type tool surface; with it, we delegate.

### 5.5 The MCP tool: `start_browser_session`

[`server.py:90-127`](../src/pipecat_mcp_server/server.py):

```python
@mcp.tool()
async def start_browser_session(
    url: str = "http://localhost:3000",
    headless: bool = False,
    cdp_port: int = 9222,
    audio_port: int = 9091,
) -> dict:
    audio_ws_url = f"ws://localhost:{audio_port}"
    start_pipecat_process(
        BrowserShimRunnerArguments(
            host="localhost", port=audio_port, sample_rate=48000,
        )
    )
    try:
        info = await asyncio.to_thread(
            start_browser, url=url, audio_ws_url=audio_ws_url,
            cdp_port=cdp_port, headless=headless,
        )
    except Exception:
        stop_pipecat_process()
        raise
    return info
```

`asyncio.to_thread` because `start_browser` blocks on a
`multiprocessing.Event.wait(timeout=startup_timeout)` — we don't want
to block the MCP request loop. If the browser fails to come up, we
roll back the pipecat process so we don't leak resources.

---

## 6. Alternatives we considered

### Alternative A — OS-level virtual audio devices

Install something like
[BlackHole](https://github.com/ExistentialAudio/BlackHole),
[Loopback](https://rogueamoeba.com/loopback/), or
[roc-vad](https://github.com/roc-streaming/roc-vad) (programmatic
virtual devices on macOS via libASPL), point Chrome at the virtual
device with `--audio-output-device-name` or via the system default,
have pipecat read/write the other side of the virtual device.

**Why rejected**: it requires a kernel-extension install on every dev
machine and CI runner. Parallelism is awkward (you'd need multiple
virtual device pairs). It's also OS-specific — Linux has
`module-null-sink`, Windows has VB-CABLE, macOS has its own. The JS
shim works the same way everywhere Playwright runs.

**When it would be the right choice**: if the app uses Web Workers or
cross-origin iframes that the shim can't reach into, OR if you're
testing a *native* desktop app (Electron, Tauri, native Mac/Windows).
Then virtual audio devices are the only option.

### Alternative B — WebRTC Insertable Streams (this is the one you asked to be unpacked)

This deserves a longer treatment because the name is unclear and
there's a forest of related APIs.

#### What "Insertable Streams" actually means

When a `RTCPeerConnection` sends or receives media, the data goes
through several stages: raw samples → encoder → SRTP packetizer →
network. WebRTC's **Insertable Streams API** ([Chrome status](https://chromestatus.com/feature/6321945865879552),
[sample](https://webrtc.github.io/samples/src/content/insertable-streams/audio-processing/),
Chrome 94+) opens up the **encoded-frame layer** of that pipeline:
you can call `sender.createEncodedStreams()` on a sender, or
`receiver.createEncodedStreams()` on a receiver, and you get a
`ReadableStream` + `WritableStream` pair of *encoded* frames (already
compressed, sitting just before the SRTP boundary). You can read,
inspect, mutate, drop, or replace each frame. The original intended
use case is end-to-end encryption: encrypt the encoded frame in JS
before it's sent, decrypt on the other side.

There is also a **related but separate** API — *MediaStreamTrack
Insertable Streams* (a.k.a. "Breakout Box",
[chromestatus](https://chromestatus.com/feature/5499415634640896),
[web.dev write-up](https://web.dev/mediastreamtrack-insertable-media-processing)).
This one works at the **raw sample layer** rather than the encoded
layer. It exposes:

- `MediaStreamTrackProcessor` — wrap a track, get a
  `ReadableStream<AudioData|VideoFrame>` of decoded media (the
  "read side").
- `MediaStreamTrackGenerator` — create a virtual track, get a
  `WritableStream<AudioData|VideoFrame>` to push decoded media into
  (the "write side").

**These two APIs are often conflated under the umbrella "Insertable
Streams" but they are different layers and have different use cases.**
Our shim uses **MediaStreamTrack Insertable Streams (a.k.a. Breakout
Box)** — the raw-sample variant — not the encoded-frame WebRTC variant.

#### Why we picked the raw-sample variant

Three reasons:

1. **It works for any audio track**, not just one inside a
   `RTCPeerConnection`. If the readme app ever switched from Daily
   WebRTC to WebSocket-streamed Opus chunks decoded into `<audio>`
   elements, the encoded-WebRTC API wouldn't help — but
   `MediaStreamTrackProcessor` on the audio output would. The shim
   becomes future-proof.

2. **The symmetry with `MediaStreamTrackGenerator`** is too
   convenient: same data type (`AudioData`), same conversion code,
   same buffering primitives. The shim's read path and write path
   are mirror images.

3. **Encoded-frame interception would force us to decode Opus in
   JS** — extra dependency, extra CPU, extra failure mode. Chrome
   already decoded the audio for us; we just take the result.

#### Why the encoded-WebRTC variant might be better in theory

- Lower latency in principle (one less decode/re-encode cycle on
  the way in).
- It's the only variant that catches the audio *before* Chrome's
  jitter buffer, so you'd see frames in transmission order with
  their original RTP timestamps — useful for forensic
  testing-of-WebRTC-itself, irrelevant to our "talk to the bot"
  goal.

#### Why we rejected it as primary

- More code (you have to handle the binary encoded frame format,
  and any time pipecat needs the data it expects raw PCM anyway).
- Locked to a `RTCPeerConnection`. The readme app today uses Daily
  WebRTC, so this works. If they ever changed transport (LiveKit
  has its own SDK with different surfaces, or a WebSocket-based
  custom transport), we'd be rewriting the shim.
- The raw-sample API is "live" at the right layer (samples), so
  there's nothing to gain from going deeper.

#### Could we add it later as a fast-path?

Yes. The shim could check `if (sender.createEncodedStreams) {…}` and
use Insertable Streams for the WebRTC case, falling back to
`MediaStreamTrackProcessor` otherwise. We didn't bother because the
slow path isn't slow.

### Alternative C — Pipecat's `SmallWebRTCTransport`

Pipecat ships `SmallWebRTCTransport` (see [pipecat docs](https://reference-server.pipecat.ai/en/latest/api/pipecat.transports.smallwebrtc.transport.html))
that lets pipecat be a *direct WebRTC peer* with a browser — no Daily,
no extra server. Daily's blog ["You don't need a WebRTC server for
your voice agents"](https://www.daily.co/blog/you-dont-need-a-webrtc-server-for-your-voice-agents/)
is the canonical reference.

**Why rejected**: we'd be running *two* WebRTC stacks in the same
browser tab — one to Daily (the readme app's existing one), one to
pipecat (for our audio). Doubles complexity, doubles latency budget,
and adds nothing because the browser-to-pipecat link is over
localhost. WebSocket carrying raw PCM is sub-millisecond and has zero
codec negotiation surface.

**When it would be right**: if the MCP server were running on a
*different host* from the browser. Then WebRTC's NAT traversal and
jitter buffering would earn their keep. For our localhost case, plain
PCM-over-WS dominates on every axis except prestige.

### Alternative D — Playwright `--use-file-for-fake-audio-capture`

Chrome supports launching with
`--use-file-for-fake-audio-capture=foo.wav` plus
`--use-fake-device-for-media-stream`, which makes `getUserMedia`
return a stream backed by a pre-recorded file
([Cyara write-up](https://cyara.com/blog/manipulating-getusermedia-available-devices/),
[Mad Devs](https://maddevs.io/writeups/testing-web-apps-with-speech-and-image-recognition/)).
You can append `%noloop` to stop it looping; you can swap files
between sessions.

**Why rejected**: file-only. There's no way to feed *live* audio (e.g.
the output of Kokoro TTS that we generate on demand based on the
conversation). For pre-recorded test fixtures this is great; for
interactive conversation it's not even close.

### Alternative E — A browser extension instead of `addInitScript`

We could ship a Manifest V3 extension that injects the shim on
specific origins, and Claude's Playwright instance would just load the
extension. This decouples shim injection from MCP-server-launched
browsers, so the user's normal Chrome could be used.

**Why deferred (not rejected)**: extensions need a manifest, a build
step, and either packaging or `--load-extension`-style developer
loading. The Playwright `addInitScript` path is one line of Python.
Worth revisiting if we ever want to test in a *human-driven*
Chrome instance (e.g., the user clicks while we listen).

### Alternative F — Anthropic Computer Use

[Computer Use](https://www.anthropic.com/news/3-5-models-and-computer-use)
(shipped March 2026 for Pro/Max subscribers, macOS only) lets Claude
drive any desktop app via screen and keyboard. Conceptually it could
replace Playwright entirely.

**Why not used here**: it would replace *Playwright*, not the *audio
shim*. The audio plumbing problem (virtual mic, virtual speaker) is
identical regardless of whether Claude clicks via Playwright or via
Computer Use. Computer Use also requires the desktop app, costs more
per action, and is non-deterministic (screenshot-based clicks). For a
deterministic CI-friendly flow, Playwright + accessibility tree wins.

---

## 7. Assumptions

Things that have to be true for this design to work. Each is documented
where it's load-bearing.

1. **Modern Chromium** with WebCodecs (`AudioData`) and Breakout-Box
   APIs (`MediaStreamTrackProcessor` / `MediaStreamTrackGenerator`).
   Verified with Playwright 1.60's bundled Chromium 148 (build 1223).
   Firefox is not currently supported — the shim would skip both hooks
   on Firefox.
2. **`localhost` is a secure context** in Chrome (it is by spec). The
   shim assumes `navigator.mediaDevices` is defined when the target
   URL is `http://localhost:*`. Tested on `localhost:3000`.
3. **Daily transport is in the top frame**, not a cross-origin iframe.
   The readme app uses `@pipecat-ai/daily-transport` directly in the
   client (verified by reading `client/app/h/[householdId]/r/[readerId]/call/page.tsx`),
   so this holds. Daily Prebuilt (`<DailyIframe>`) would NOT work
   without iframe-routing additions.
4. **48 kHz mono on the wire matches Chrome's WebRTC output rate.**
   The shim records the actual `AudioData.sampleRate` in
   `window.__voiceShim.outboundSampleRate` so a future mismatch is
   immediately diagnosable. Empirically, Daily delivers 48 kHz.
5. **VAD-based turn detection** segments the bot's audio into
   meaningful utterances. With `stop_secs=1.0` this works for both
   the readme bot (Ember) and the Toocan bot in our tests; longer or
   shorter values may suit other bots.
6. **The page consumes the mic track via standard
   `RTCPeerConnection.addTrack(track)`**, not some sideways API. Any
   app using a WebRTC SDK satisfies this.
7. **Pipecat's WebsocketServerTransport accepts a single client
   connection per session.** This matches our 1:1 model (one shim,
   one pipecat). Multiple concurrent browser sessions would need
   distinct audio ports.

---

## 8. Test session artifacts

The `scripts/e2e_readme_call.py` driver writes everything it produces
to `artifacts/e2e_readme_call/`:

| File | What it is |
|---|---|
| `run.log` | Full INFO-level log of one e2e run. Includes the Whisper transcripts of Ember's speech, the shim audio counters at each checkpoint, and the navigation timing. |
| `01_login_page.png` ... `06_after_end_call.png` | Screenshots from Playwright at six fixed points in the flow: landing on /auth/login, household page after login, call page just loaded, call connected, just before the End-reading click, after returning to /r/<readerId>. |
| `ember_voice.wav` | The audio the bot (Ember) spoke during the call, as captured by the shim's RTCPeerConnection hook and shipped to pipecat. 16-bit LE mono 48 kHz. |
| `kokoro_voice.wav` | The audio our Kokoro TTS generated for Ember to hear — the synthetic-mic side. Same format. |
| `merged.wav` | Both sides mixed into a single mono stream for easy listening. |

These are regenerated on each run; the run.log header includes the
date and the artifact directory absolute path.

The numbers from a representative successful run (the one whose
artifacts are committed):

```
ember (greeting): "Hello, welcome. I'm so excited to have you. Let me know if you're welcome to our podcast."
ember (turn 2):   "The one part of us told you, but clearly I must have a spear..."
shim counters (final): inbound=126  outbound=2639  pcCount=3  audioTrackCount=2
```

`pcCount=3` because Daily creates three `RTCPeerConnection`s (media,
data, signalling), and we wrap all of them. `audioTrackCount=2` —
inbound from the bot + outbound from the synthetic mic (mirror tracks
within Daily's plan-b SDP).

---

## 9. What we deliberately did NOT do

This list exists to flag things a reader might expect to see, and
explain why they're absent.

- **No global virtual audio driver.** We don't install BlackHole or
  similar — see §6.A. The shim is enough for browser-based apps.
- **No bundle step for the shim.** It's a single `.js` file read at
  runtime, prepended with a one-line URL config and shoved into the
  page via `addInitScript`. A real product would probably bundle and
  minify; not worth it for a debug tool.
- **No iframe routing.** The shim runs in the top document only. If
  the readme app ever moves Daily into an iframe, we'd need
  Playwright's frame routing or CDP-level injection into the iframe's
  target.
- **No retry-on-stop-mid-conversation.** If the user clicks "End
  reading", we don't try to revive the call. If pipecat crashes
  mid-conversation we restart from scratch.
- **No transcript quality tuning beyond `stop_secs`.** Whisper-MLX
  with `whisper-large-v3-turbo` is good enough for our assertions but
  not for production-grade transcription. Switching to a cloud STT
  would be a one-line change in `agent.py`.

---

## 10. Where to look in the code, one more time

- The dispatch: [`agent.py:296-345`](../src/pipecat_mcp_server/agent.py)
- The runner-args type: [`runner_args.py`](../src/pipecat_mcp_server/runner_args.py)
- The serializer: [`raw_pcm_serializer.py`](../src/pipecat_mcp_server/raw_pcm_serializer.py)
- The shim, diagnostics setup: [`shim.js:33-49`](../src/pipecat_mcp_server/shim.js)
- The shim, mic hook: [`shim.js:107-122`](../src/pipecat_mcp_server/shim.js)
- The shim, peer-connection wrap: [`shim.js:135-184`](../src/pipecat_mcp_server/shim.js)
- The browser launcher: [`browser_session.py`](../src/pipecat_mcp_server/browser_session.py)
- The MCP tool surface: [`server.py:90-127`](../src/pipecat_mcp_server/server.py)
- The end-to-end driver: [`scripts/e2e_readme_call.py`](../scripts/e2e_readme_call.py)
- VAD tuning explanation: [`agent.py:115-121`](../src/pipecat_mcp_server/agent.py)
- The recording dump: [`agent.py:_dump_recordings`](../src/pipecat_mcp_server/agent.py)

---

## 11. External references

- [Insertable Streams - Audio (WebRTC samples)](https://webrtc.github.io/samples/src/content/insertable-streams/audio-processing/) — encoded-frame WebRTC variant
- [MediaStreamTrack Insertable Streams (web.dev)](https://web.dev/mediastreamtrack-insertable-media-processing) — raw-sample "Breakout Box" variant, the one we use
- [Chrome Status: MediaStreamTrack Insertable Streams](https://chromestatus.com/feature/5499415634640896)
- [Chrome Status: WebRTC Insertable Streams](https://chromestatus.com/feature/6321945865879552)
- [Pipecat `WebsocketServerTransport`](https://docs.pipecat.ai/server/services/transport/websocket-server)
- [Pipecat `SmallWebRTCTransport`](https://reference-server.pipecat.ai/en/latest/api/pipecat.transports.smallwebrtc.transport.html)
- [Daily: "You don't need a WebRTC server for your voice agents"](https://www.daily.co/blog/you-dont-need-a-webrtc-server-for-your-voice-agents/)
- [Cyara: Manipulating getUserMedia and Available Devices](https://cyara.com/blog/manipulating-getusermedia-available-devices/)
- [Mad Devs: Playwright Fake Audio & Video Input](https://maddevs.io/writeups/testing-web-apps-with-speech-and-image-recognition/)
- [roc-vad: programmatic virtual audio devices for macOS](https://github.com/roc-streaming/roc-vad)
- [libASPL: C++17 lib for macOS Audio Server plug-ins](https://github.com/gavv/libASPL)
- [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)
- [Anthropic Computer Use](https://www.anthropic.com/news/3-5-models-and-computer-use)
