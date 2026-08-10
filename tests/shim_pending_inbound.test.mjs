import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const SHIM_SOURCE = fs.readFileSync(
  new URL('../src/voicebox/shim.js', import.meta.url),
  'utf8',
);

test('pre-microphone audio is bounded and discarded AudioData is closed', async () => {
  const sockets = [];
  const frames = [];
  const writes = [];

  class FakeWebSocket {
    static OPEN = 1;

    constructor() {
      this.listeners = {};
      this.readyState = FakeWebSocket.OPEN;
      sockets.push(this);
    }

    addEventListener(type, listener) {
      this.listeners[type] = listener;
    }

    receive(data) {
      this.listeners.message({ data });
    }
  }

  class FakeAudioData {
    constructor(options) {
      this.numberOfFrames = options.numberOfFrames;
      this.closed = false;
      frames.push(this);
    }

    close() {
      this.closed = true;
    }
  }

  class FakeMediaStreamTrackGenerator {
    constructor() {
      this.writable = {
        getWriter: () => ({ write: async (frame) => writes.push(frame) }),
      };
    }
  }

  class FakeMediaStream {
    constructor(tracks) {
      this.tracks = tracks;
    }

    getAudioTracks() {
      return this.tracks;
    }

    getVideoTracks() {
      return [];
    }
  }

  const context = {
    ArrayBuffer,
    AudioData: FakeAudioData,
    Float32Array,
    Int16Array,
    MediaStream: FakeMediaStream,
    MediaStreamTrackGenerator: FakeMediaStreamTrackGenerator,
    WebSocket: FakeWebSocket,
    console: { log() {}, warn() {} },
    navigator: { mediaDevices: { getUserMedia: async () => new FakeMediaStream([]) } },
    performance: { now: () => 0 },
    setTimeout() {},
    window: {},
  };
  context.window = context;
  vm.runInNewContext(SHIM_SOURCE, context);

  const oneSecond = new Int16Array(48_000).buffer;
  for (let i = 0; i < 7; i++) sockets[0].receive(oneSecond);

  assert.equal(context.__voiceShim.pendingInboundFrames, 3 * 48_000);
  assert.equal(context.__voiceShim.droppedInboundChunks, 4);
  assert.equal(context.__voiceShim.droppedInboundFrames, 4 * 48_000);
  assert.deepEqual(frames.map((frame) => frame.closed), [true, true, true, true, false, false, false]);

  await context.navigator.mediaDevices.getUserMedia({ audio: true });
  assert.equal(writes.length, 3);
  assert.equal(context.__voiceShim.pendingInboundFrames, 0);
});
