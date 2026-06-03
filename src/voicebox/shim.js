// Browser audio shim for the MCP server.
//
// Injected as `addInitScript` before any page code runs. Pretends to be the
// user's microphone and intercepts inbound WebRTC audio so an MCP-controlled
// Pipecat agent can act as the user of any WebRTC voice app — without the app
// being aware of the indirection.
//
// Wire format on the WebSocket: raw little-endian 16-bit signed PCM, mono.
// Two sample rates: MIC_RATE (48 kHz) for inbound Kokoro playback to the
// page, TAP_RATE (16 kHz) for outbound capture to pipecat — matches Whisper.
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
  // Kokoro → fake-mic playback (browser side): stay at 48 kHz to match
  // the page's native AudioContext and avoid quality loss.
  const MIC_RATE = 48000;
  // Remote-track → pipecat tap: Whisper-MLX expects 16 kHz audio
  // (mlx_whisper.transcribe has no sample_rate parameter — it always
  // treats input as 16 kHz). Capture at 16 kHz here so we don't have to
  // resample on the pipecat side. Web Audio handles the 48→16 conversion
  // inside MediaStreamAudioSourceNode → AudioContext({sampleRate: 16000}).
  const TAP_RATE = 16000;
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
          sampleRate: MIC_RATE,
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

  // --- Hook 2: RTCPeerConnection wrap. ---
  //
  // Tap the remote audio track via Web Audio (MediaStreamAudioSourceNode +
  // AudioWorkletNode), NOT MediaStreamTrackProcessor.
  //
  // Why: MediaStreamTrackProcessor is a side-channel on the WebCodecs path
  // that only emits AudioData chunks when the source produces samples — on
  // a remote WebRTC track, silence periods produce *no* chunks at all. We'd
  // then ship a sparse byte stream to pipecat, which has no notion of "this
  // chunk arrived 800 ms after the previous one", and the recorded WAV
  // sounds 3× sped up because all the pauses are elided.
  //
  // Web Audio is pulled by the AudioContext at a fixed sample rate. The
  // source node's internal jitter buffer fills silence with zeros, so the
  // worklet's process() callback receives a continuous stream of Float32
  // samples regardless of whether the bot is talking. End-of-pipe duration
  // = wall-clock duration, which is what AudioBufferProcessor expects.
  const hasWebAudio = typeof AudioContext !== 'undefined' && typeof AudioWorkletNode !== 'undefined';
  if (typeof RTCPeerConnection !== 'undefined' && hasWebAudio) {
    try {
      const OrigRTCPeerConnection = window.RTCPeerConnection;

      // Daily creates multiple RTCPeerConnections (media, data, fallback). The
      // same logical bot audio can surface as a `track` event on more than one
      // of them, or even fire multiple times on the same one (renegotiation,
      // ICE restart). Deduplicate by track.id.
      const tappedTrackIds = new Set();

      // AudioWorklet processor source — registered once via a Blob URL.
      // process() is called every 128 frames at the AudioContext's sample
      // rate (~2.67 ms at 48 kHz). We convert Float32 → Int16 and forward
      // the buffer to the main thread via port.postMessage.
      const WORKLET_SRC = `
        class PcmCapture extends AudioWorkletProcessor {
          process(inputs) {
            const input = inputs[0];
            if (!input || !input[0]) return true;
            const f32 = input[0];
            const n = f32.length;
            const i16 = new Int16Array(n);
            for (let i = 0; i < n; i++) {
              const s = Math.max(-1, Math.min(1, f32[i]));
              i16[i] = s < 0 ? s * 32768 : s * 32767;
            }
            this.port.postMessage(i16.buffer, [i16.buffer]);
            return true;
          }
        }
        registerProcessor('pcm-capture', PcmCapture);
      `;
      const workletUrl = URL.createObjectURL(
        new Blob([WORKLET_SRC], { type: 'application/javascript' })
      );

      // One AudioContext for the whole shim — created lazily on first tap.
      // Constructed at TAP_RATE (16 kHz) so the worklet emits Whisper-ready
      // samples and pipecat doesn't need to resample on the WS side.
      let audioCtx = null;
      let workletReady = null;
      const ensureAudioCtx = async () => {
        if (audioCtx) {
          await workletReady;
          return audioCtx;
        }
        audioCtx = new AudioContext({ sampleRate: TAP_RATE });
        workletReady = audioCtx.audioWorklet.addModule(workletUrl);
        await workletReady;
        // Some Chromium builds start the context in 'suspended' state even
        // with autoplay-allowed flags — explicitly resume so the graph ticks.
        if (audioCtx.state === 'suspended') {
          try { await audioCtx.resume(); } catch (e) { recordError('audioCtx.resume', e); }
        }
        console.log(TAG, 'AudioContext ready', {
          sampleRate: audioCtx.sampleRate,
          state: audioCtx.state,
        });
        return audioCtx;
      };

      class WrappedRTCPeerConnection extends OrigRTCPeerConnection {
        constructor(...args) {
          super(...args);
          diag.pcCount++;
          this.addEventListener('track', async (ev) => {
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

            // Chromium only decodes a REMOTE WebRTC audio track while it is being
            // rendered by a media element; a MediaStreamAudioSourceNode alone is
            // NOT enough — the tap below then captures pure silence. (Verified by
            // loopback test: createMediaStreamSource on a remote track yields all
            // zeros unless an <audio>/<video> element is also playing the stream;
            // headless and muted state make no difference.) Previously capture
            // only worked as a side-effect of the page rendering the bot's audio,
            // so it broke on apps/states that don't. Sink the track into our own
            // muted, un-attached <audio> element so the decode always runs.
            let sinkEl = null;
            try {
              sinkEl = new Audio();
              sinkEl.muted = true;
              sinkEl.autoplay = true;
              sinkEl.srcObject = new MediaStream([track]);
              const playPromise = sinkEl.play();
              if (playPromise && playPromise.catch) {
                playPromise.catch((e) => recordError('sink element play', e));
              }
            } catch (e) {
              recordError('sink element', e);
            }

            let ctx;
            try {
              ctx = await ensureAudioCtx();
            } catch (e) {
              recordError('AudioContext init', e);
              return;
            }
            if (diag.outboundSampleRate === null) {
              diag.outboundSampleRate = ctx.sampleRate;
              diag.outboundNumChannels = 1;
              diag.outboundFormat = 'webaudio-f32';
            }

            let source, node;
            try {
              source = ctx.createMediaStreamSource(new MediaStream([track]));
              // numberOfOutputs: 0 → pure sink. The node still ticks because
              // its input is connected; we don't connect to ctx.destination
              // (the page already plays the audio out the real speakers).
              node = new AudioWorkletNode(ctx, 'pcm-capture', { numberOfOutputs: 0 });
            } catch (e) {
              recordError('Web Audio graph construct', e);
              return;
            }
            node.port.onmessage = (msg) => {
              const buf = msg.data;
              if (!(buf instanceof ArrayBuffer)) return;
              if (diag.wsReady && ws && ws.readyState === WebSocket.OPEN) {
                try {
                  ws.send(buf);
                  diag.outboundChunks++;
                  diag.perTrackBytes[track.id] =
                    (diag.perTrackBytes[track.id] || 0) + buf.byteLength;
                } catch (e) {
                  recordError('ws.send outbound', e);
                }
              }
            };
            source.connect(node);
            track.addEventListener('ended', () => {
              try { source.disconnect(); } catch {}
              try { node.disconnect(); } catch {}
              if (sinkEl) { try { sinkEl.srcObject = null; } catch {} }
            });
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
