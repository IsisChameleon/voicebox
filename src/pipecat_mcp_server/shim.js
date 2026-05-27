// Browser audio shim for the MCP server.
//
// Injected as `addInitScript` before any page code runs. Pretends to be the
// user's microphone and intercepts inbound WebRTC audio so an MCP-controlled
// Pipecat agent can act as the user of any WebRTC voice app — without the app
// being aware of the indirection.
//
// Wire format on the WebSocket: raw little-endian 16-bit signed PCM, mono.
// Sample rate is fixed at SAMPLE_RATE (set to match WebsocketServerParams on
// the Python side; pipecat resamples internally for the rest of the pipeline).
//
// Hook surfaces:
//   1. navigator.mediaDevices.getUserMedia({audio}) returns a synthetic
//      MediaStream whose audio track is fed by inbound WS frames. The app's
//      RTCPeerConnection then sends OUR audio to the remote peer.
//   2. window.RTCPeerConnection is wrapped so every `track` event with an
//      audio kind is teed through a MediaStreamTrackProcessor that ships PCM
//      back over the same WebSocket. The bot's audio still plays out the
//      real speakers (we don't block it), and we get a clean copy for STT.
//
// Defensive: every hook is gated on the API existing. On pages where
// navigator.mediaDevices is undefined (about:blank, http: origins that
// aren't localhost), or WebCodecs is missing, the corresponding hook is
// skipped — the shim never throws and never blocks page load. Diagnostics
// live on window.__voiceShim regardless.

(() => {
  const AUDIO_WS_URL = window.__VOICE_SHIM_WS_URL__ || 'ws://localhost:9091';
  const SAMPLE_RATE = 48000;
  const TAG = '[voice-shim]';

  // --- Diagnostics object available immediately, before any risky hook. ---
  const diag = {
    installed: false,
    micHookInstalled: false,
    pcHookInstalled: false,
    wsReady: false,
    inboundChunks: 0,
    outboundChunks: 0,
    audioWsUrl: AUDIO_WS_URL,
    pcCount: 0,
    audioTrackCount: 0,
    // The sample rate of the FIRST outbound AudioData chunk we observed.
    // For Daily/WebRTC tracks in Chrome this is typically 48000, but record
    // it so we can verify / detect mismatches against the pipecat side.
    outboundSampleRate: null,
    outboundNumChannels: null,
    outboundFormat: null,
    // Per-track byte counter — keyed by track.id. Useful to confirm we are
    // tapping one logical audio track, not multiple copies of the same one.
    perTrackBytes: {},
    errors: [],
    get hasMediaDevices() { return !!navigator?.mediaDevices?.getUserMedia; },
    get hasWebCodecs() { return typeof AudioData !== 'undefined' && typeof MediaStreamTrackGenerator !== 'undefined'; },
  };
  window.__voiceShim = diag;
  const recordError = (where, e) => {
    const msg = `${where}: ${e && e.message ? e.message : String(e)}`;
    diag.errors.push(msg);
    console.warn(TAG, msg);
  };

  let ws = null;
  let micWriter = null;
  let pendingInbound = [];

  function openWs() {
    try {
      ws = new WebSocket(AUDIO_WS_URL);
    } catch (e) {
      recordError('WebSocket constructor', e);
      setTimeout(openWs, 1000);
      return;
    }
    ws.binaryType = 'arraybuffer';
    ws.addEventListener('open', () => {
      diag.wsReady = true;
      console.log(TAG, 'audio WS connected:', AUDIO_WS_URL);
    });
    ws.addEventListener('close', () => {
      diag.wsReady = false;
      // Reconnect: pipecat may not be up yet on initial page load, or the
      // session may have been torn down and restarted.
      setTimeout(openWs, 1000);
    });
    ws.addEventListener('error', (e) => recordError('WS error event', e));
    ws.addEventListener('message', (ev) => {
      if (!(ev.data instanceof ArrayBuffer)) return;
      if (!diag.hasWebCodecs) return;
      try {
        const int16 = new Int16Array(ev.data);
        const float32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
        const frame = new AudioData({
          format: 'f32',
          sampleRate: SAMPLE_RATE,
          numberOfFrames: float32.length,
          numberOfChannels: 1,
          timestamp: performance.now() * 1000,
          data: float32,
        });
        if (micWriter) {
          micWriter.write(frame).catch(() => {});
        } else {
          pendingInbound.push(frame);
        }
        diag.inboundChunks++;
      } catch (e) {
        recordError('inbound message handling', e);
      }
    });
  }
  openWs();

  function makeSyntheticMicStream() {
    const generator = new MediaStreamTrackGenerator({ kind: 'audio' });
    micWriter = generator.writable.getWriter();
    for (const f of pendingInbound) micWriter.write(f).catch(() => {});
    pendingInbound = [];
    console.log(TAG, 'created synthetic mic stream');
    return new MediaStream([generator]);
  }

  // --- Hook 1: getUserMedia override (only if mediaDevices exists). ---
  if (diag.hasMediaDevices && diag.hasWebCodecs) {
    try {
      const origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async (constraints) => {
        if (!constraints || !constraints.audio) return origGUM(constraints);
        console.log(TAG, 'intercepting getUserMedia(audio)');
        const audioStream = makeSyntheticMicStream();
        if (!constraints.video) return audioStream;
        const videoOnly = await origGUM({ video: constraints.video });
        videoOnly.getAudioTracks().forEach((t) => t.stop());
        return new MediaStream([
          ...audioStream.getAudioTracks(),
          ...videoOnly.getVideoTracks(),
        ]);
      };
      diag.micHookInstalled = true;
    } catch (e) {
      recordError('install getUserMedia hook', e);
    }
  } else {
    console.log(TAG, 'skipping getUserMedia hook (insecure context or missing WebCodecs)');
  }

  // --- Hook 2: RTCPeerConnection wrap (only if RTCPeerConnection + WebCodecs). ---
  if (typeof RTCPeerConnection !== 'undefined' && typeof MediaStreamTrackProcessor !== 'undefined') {
    try {
      const OrigRTCPeerConnection = window.RTCPeerConnection;

      // Daily creates multiple RTCPeerConnections (media, data, fallback). The
      // same logical bot audio can surface as a `track` event on more than one
      // of them, or even fire multiple times on the same one (renegotiation,
      // ICE restart). If we naively tap every event we'd ship the same
      // samples N times to pipecat — the recorded WAV ends up "fast forward"
      // because the buffer accumulates Nx audio over the same real-time
      // interval. Deduplicate by track.id.
      const tappedTrackIds = new Set();

      class WrappedRTCPeerConnection extends OrigRTCPeerConnection {
        constructor(...args) {
          super(...args);
          diag.pcCount++;
          this.addEventListener('track', (ev) => {
            const track = ev.track;
            if (!track || track.kind !== 'audio') return;
            if (tappedTrackIds.has(track.id)) {
              console.log(TAG, 'duplicate track event for', track.id, '— skipping');
              return;
            }
            tappedTrackIds.add(track.id);
            track.addEventListener('ended', () => tappedTrackIds.delete(track.id));
            diag.audioTrackCount++;
            console.log(TAG, 'tee-ing remote audio track', track.id);
            let processor;
            try {
              processor = new MediaStreamTrackProcessor({ track });
            } catch (e) {
              recordError('MediaStreamTrackProcessor construct', e);
              return;
            }
            const reader = processor.readable.getReader();
            (async () => {
              while (true) {
                let chunk;
                try {
                  chunk = await reader.read();
                } catch {
                  break;
                }
                if (chunk.done) break;
                const value = chunk.value;
                if (diag.outboundSampleRate === null) {
                  diag.outboundSampleRate = value.sampleRate;
                  diag.outboundNumChannels = value.numberOfChannels;
                  diag.outboundFormat = value.format;
                  console.log(TAG, 'first outbound AudioData', {
                    sampleRate: value.sampleRate,
                    numberOfChannels: value.numberOfChannels,
                    numberOfFrames: value.numberOfFrames,
                    format: value.format,
                  });
                }
                const n = value.numberOfFrames;
                const float32 = new Float32Array(n);
                try {
                  value.copyTo(float32, { planeIndex: 0 });
                } catch (e) {
                  value.close();
                  continue;
                }
                const int16 = new Int16Array(n);
                for (let i = 0; i < n; i++) {
                  const s = Math.max(-1, Math.min(1, float32[i]));
                  int16[i] = s < 0 ? s * 32768 : s * 32767;
                }
                if (diag.wsReady && ws && ws.readyState === WebSocket.OPEN) {
                  try {
                    ws.send(int16.buffer);
                    diag.outboundChunks++;
                    diag.perTrackBytes[track.id] =
                      (diag.perTrackBytes[track.id] || 0) + int16.byteLength;
                  } catch (e) {
                    recordError('ws.send outbound', e);
                  }
                }
                value.close();
              }
            })();
          });
        }
      }
      window.RTCPeerConnection = WrappedRTCPeerConnection;
      diag.pcHookInstalled = true;
    } catch (e) {
      recordError('install RTCPeerConnection wrap', e);
    }
  } else {
    console.log(TAG, 'skipping RTCPeerConnection hook (API missing)');
  }

  diag.installed = true;
  console.log(TAG, 'installed.', {
    micHook: diag.micHookInstalled,
    pcHook: diag.pcHookInstalled,
    audioWs: AUDIO_WS_URL,
  });
})();
