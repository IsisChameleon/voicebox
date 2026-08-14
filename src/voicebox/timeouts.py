"""Shared child wait budgets used to derive parent IPC deadlines."""

CONNECT_GRACE_SECS = 10.0
TURN_WAIT_TIMEOUT_SECS = 120.0
PLAYOUT_TIMEOUT_SECS = 30.0
PLAYOUT_SECS_PER_WORD = 0.8
IPC_MARGIN_SECS = 20.0

# Stock TTSService pushes a TTSStoppedFrame after this much silence on an open
# audio context, even when synthesis has not produced its first chunk yet
# (pipecat tts_service.py, `stop_frame_timeout_s`). The 3 s default is sized for
# streaming HTTP providers; Kokoro under TOKEN aggregation synthesizes the WHOLE
# utterance before yielding anything, measured at 4.2 s for three sentences and
# 11.5 s under CPU contention (round 6). A premature stop breaks three things at
# once: it disarms GeneratedUtteranceAudioBuffer before the audio arrives, it is
# counted by the pipeline observer so `_tts_pending` decrements twice per
# speak(), and it sets `_Playout._tts_finished` before any audio has played.
# This is a wedged-context fallback, not the normal path (the generator
# completing is), so it only has to outlive any window a caller waits on —
# the parent's speak deadline is 60 s.
TTS_STOP_FRAME_TIMEOUT_SECS = 120.0
