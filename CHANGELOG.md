# Changelog

All notable changes to **voicebox** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Initial release: an MCP server that gives an LLM agent voice + ears in a
browser. The agent drives a Playwright-controlled Chromium with an audio
shim injected; the shim hijacks the page's microphone (fed by Kokoro TTS
from this server) and tees the page's WebRTC remote audio into Whisper.
The agent can then act as a synthetic voice user against any web voice
app — Daily, LiveKit, plain `RTCPeerConnection`, anything that uses
`getUserMedia` + WebRTC — without the app being aware of the indirection.

Tools exposed over MCP: `start_browser_session`, `speak`, `listen`, `stop`.
