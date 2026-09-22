"""Optional noise suppression for the audio sent to cloud recognition.

``cloud_asr.denoise`` selects the engine:
  none     raw microphone samples (default)
  webrtc   WebRTC APM noise suppression at 16 kHz (level, high-pass, AGC2)
  rnnoise  Xiph RNNoise at 48 kHz (system ``librnnoise``), with an optional
           attenuation cap (wet/dry blend) and optional werman-style VAD gate

The processor is streaming: ``process`` takes any number of int16 samples and
returns denoised samples; ``flush`` returns the tail. Engine latency is
compensated, so output sample *k* always corresponds to input sample *k* and
``flush`` makes the total output length equal the total input length. The
batch path can therefore cut the denoised copy at the same VAD boundaries as
the raw one.

If an engine fails (library missing, native error) the processor logs once
and passes raw samples through for the rest of the utterance, starting exactly
where denoised output stopped, so no sample is lost or duplicated.
"""
import ctypes
import ctypes.util
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("aoide.denoise")

RATE = 16000
WEBRTC_LEVELS = {"low": 0, "moderate": 1, "high": 2, "very_high": 3}


class StreamFIR:
    """Stateful linear-phase FIR with an integer group delay of (taps-1)/2."""

    def __init__(self, cutoff, taps=121, gain=1.0):
        from scipy.signal import firwin
        self.h = firwin(taps, cutoff) * gain
        self.zi = np.zeros(taps - 1)
        self.delay = (taps - 1) // 2

    def __call__(self, x):
        from scipy.signal import lfilter
        if not x.size:
            return x
        y, self.zi = lfilter(self.h, 1.0, x, zi=self.zi)
        return y


class Upsampler:
    """Integer-factor upsampler (zero-stuffing + anti-imaging FIR)."""

    def __init__(self, factor, taps=121):
        self.factor = factor
        self.fir = StreamFIR(1 / factor, taps, gain=factor)
        self.delay = self.fir.delay  # at the high rate

    def __call__(self, x):
        up = np.zeros(x.size * self.factor)
        up[::self.factor] = x
        return self.fir(up)


class Downsampler:
    """Integer-factor downsampler (anti-alias FIR + phase-tracked decimation)."""

    def __init__(self, factor, taps=121):
        self.factor = factor
        self.fir = StreamFIR(1 / factor, taps)
        self.delay = self.fir.delay  # at the high rate
        self.phase = 0

    def __call__(self, x):
        y = self.fir(x)
        start = (-self.phase) % self.factor
        self.phase = (self.phase + x.size) % self.factor
        return y[start::self.factor]


class WebRTCEngine:
    """WebRTC APM noise suppression; 160-sample (10 ms) frames at 16 kHz."""

    BLOCK = 160
    delay = 96  # NS overlap-add: 256-point analysis window minus the 160-sample hop
    hold = 0

    def __init__(self, level="moderate", high_pass=True, agc=False, library=None):
        if level not in WEBRTC_LEVELS:
            raise ValueError(f"webrtc_level must be one of {sorted(WEBRTC_LEVELS)}")
        path = library or Path.home() / ".local/lib/aoide/libdenoise.so"
        self.lib = ctypes.CDLL(str(path))
        self.lib.yh_denoise_create_ex.restype = ctypes.c_void_p
        self.lib.yh_denoise_create_ex.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.lib.yh_denoise_free.argtypes = [ctypes.c_void_p]
        self.lib.yh_denoise_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.c_int]
        self.state = self.lib.yh_denoise_create_ex(WEBRTC_LEVELS[level], int(high_pass), int(agc))
        if not self.state:
            raise RuntimeError("WebRTC noise suppressor could not initialize")
        self.pending = np.empty(0, np.int16)

    def process(self, x):
        x = np.concatenate((self.pending, x.astype(np.int16)))
        n = x.size // self.BLOCK * self.BLOCK
        self.pending = x[n:].copy()
        src = np.ascontiguousarray(x[:n])
        out = np.empty_like(src)
        if n and self.lib.yh_denoise_process(self.state, src.ctypes.data, out.ctypes.data, n):
            raise RuntimeError("WebRTC noise suppression failed")
        return out.astype(np.float64)

    def close(self):
        if self.state:
            self.lib.yh_denoise_free(self.state)
            self.state = None


class RNNoiseEngine:
    """RNNoise on 480-sample (10 ms) frames at 48 kHz, fed by a 16k->48k->16k chain.

    ``attenuation_limit_db`` > 0 blends the time-aligned dry signal back in so
    noise (and any speech RNNoise mistakes for noise) is reduced by at most
    that many dB. ``vad_threshold`` > 0 enables the gate from werman's
    noise-suppression-for-voice: frames are muted unless RNNoise's voice
    probability reaches the threshold, stays open ``vad_grace_ms`` after the
    last voiced frame, and opens ``vad_retro_ms`` before it (which delays the
    output by that much).
    """

    FRAME = 480
    CORE_DELAY = 960  # RNNoise analysis/synthesis latency at 48 kHz (measured)

    def __init__(self, attenuation_limit_db=0.0, vad_threshold=0.0, vad_grace_ms=200,
                 vad_retro_ms=0, library=None):
        name = library or ctypes.util.find_library("rnnoise") or "librnnoise.so.0"
        self.lib = ctypes.CDLL(name)
        self.lib.rnnoise_create.restype = ctypes.c_void_p
        self.lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        self.lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        self.lib.rnnoise_process_frame.restype = ctypes.c_float
        self.lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        if self.lib.rnnoise_get_frame_size() != self.FRAME:
            raise RuntimeError("Unexpected RNNoise frame size")
        self.state = self.lib.rnnoise_create(None)
        if not self.state:
            raise RuntimeError("RNNoise could not initialize")
        self.up = Upsampler(3)
        self.down = Downsampler(3)
        self.pending = np.empty(0, np.float32)
        self.dry_mix = 10 ** (-attenuation_limit_db / 20) if attenuation_limit_db > 0 else 0.0
        self.dry = np.zeros(self.CORE_DELAY, np.float32)  # dry delay line, aligned to wet
        self.vad_threshold = vad_threshold
        self.grace = int(round(vad_grace_ms / 10))
        self.retro = int(round(vad_retro_ms / 10)) if vad_threshold > 0 else 0
        self.queue = []          # [frame, open] waiting for retroactive decisions
        self.since_voice = 10 ** 9
        self.vad = []            # per-frame voice probability, for diagnostics/tests
        # Sample-sequence delay. The retroactive gate holds frames back
        # (``hold``) but does not shift the sequence, so it is not included.
        delay48 = self.up.delay + self.CORE_DELAY + self.down.delay
        if delay48 % 3:
            raise RuntimeError("RNNoise chain delay must be a whole number of 16 kHz samples")
        self.delay = delay48 // 3
        self.BLOCK = self.FRAME // 3
        self.hold = self.retro * self.BLOCK

    def process(self, x):
        x48 = np.concatenate((self.pending, self.up(x.astype(np.float64)).astype(np.float32)))
        n = x48.size // self.FRAME * self.FRAME
        self.pending = x48[n:].copy()
        frames = []
        for i in range(0, n, self.FRAME):
            frames.extend(self._frame(np.ascontiguousarray(x48[i:i + self.FRAME])))
        y48 = np.concatenate(frames) if frames else np.empty(0, np.float32)
        return self.down(y48.astype(np.float64))

    def _frame(self, frame):
        wet = np.empty(self.FRAME, np.float32)
        prob = self.lib.rnnoise_process_frame(self.state, wet.ctypes.data, frame.ctypes.data)
        self.vad.append(float(prob))
        self.dry = np.concatenate((self.dry, frame))
        dry, self.dry = self.dry[:self.FRAME], self.dry[self.FRAME:]
        out = (1 - self.dry_mix) * wet + self.dry_mix * dry if self.dry_mix else wet
        if self.vad_threshold <= 0:
            return [out]
        voiced = prob >= self.vad_threshold
        self.since_voice = 0 if voiced else self.since_voice + 1
        self.queue.append([out, voiced or self.since_voice <= self.grace])
        if voiced:
            for item in self.queue[-self.retro - 1:]:
                item[1] = True
        released = []
        while len(self.queue) > self.retro:
            frame_out, is_open = self.queue.pop(0)
            released.append(frame_out if is_open else np.zeros_like(frame_out))
        return released

    def close(self):
        if self.state:
            self.lib.rnnoise_destroy(self.state)
            self.state = None


def make_engine(mode, options):
    if mode == "webrtc":
        return WebRTCEngine(level=options.get("webrtc_level", "moderate"),
                            high_pass=bool(options.get("high_pass", True)),
                            agc=bool(options.get("webrtc_agc", False)),
                            library=options.get("webrtc_library"))
    if mode == "rnnoise":
        return RNNoiseEngine(attenuation_limit_db=float(options.get("rnnoise_attenuation_limit_db", 0)),
                             vad_threshold=float(options.get("rnnoise_vad_threshold", 0)),
                             vad_grace_ms=float(options.get("rnnoise_vad_grace_ms", 200)),
                             vad_retro_ms=float(options.get("rnnoise_vad_retro_ms", 0)),
                             library=options.get("rnnoise_library"))
    raise ValueError(f"cloud_asr.denoise must be none, webrtc or rnnoise, not {mode}")


class CloudDenoiser:
    """Latency-compensated, fail-safe streaming wrapper around one engine.

    One instance per key press (``reset`` between utterances). Runs on the
    event loop; the PortAudio callback thread never calls it.
    """

    def __init__(self, mode="none", options=None):
        self.mode = mode or "none"
        self.options = dict(options or {})
        if self.mode != "none":
            make_engine(self.mode, self.options).close()  # fail at startup on bad config
        self.engine = None
        self.reset()

    @property
    def enabled(self):
        return self.mode != "none"

    def reset(self):
        if self.engine:
            self.engine.close()
        self.engine = None
        self.failed = None
        self.inputs = 0       # samples received
        self.emitted = 0      # samples returned
        self.skip = 0         # engine start-up delay still to discard
        self.unmatched = np.empty(0, np.int16)  # raw input not yet returned
        if self.enabled:
            try:
                self.engine = make_engine(self.mode, self.options)
                self.skip = self.engine.delay
            except Exception as exc:
                self._fail(exc)

    def _fail(self, exc):
        if self.failed is None:
            self.failed = exc
            logger.warning("Cloud denoiser %s failed (%s: %s); sending raw audio for this utterance",
                           self.mode, type(exc).__name__, str(exc)[:200])
        if self.engine:
            try:
                self.engine.close()
            except Exception:
                pass
            self.engine = None

    def _emit(self, y):
        if self.skip:
            drop = min(self.skip, y.size)
            y, self.skip = y[drop:], self.skip - drop
        y = y[:max(0, self.inputs - self.emitted)]
        self.emitted += y.size
        self.unmatched = self.unmatched[y.size:]
        return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()

    def process(self, pcm: bytes) -> bytes:
        if not self.enabled:
            return pcm
        x = np.frombuffer(pcm, dtype="<i2")
        self.inputs += x.size
        self.unmatched = np.concatenate((self.unmatched, x))
        if self.engine is not None:
            try:
                return self._emit(self.engine.process(x))
            except Exception as exc:
                self._fail(exc)
        return self._passthrough()

    def _passthrough(self):
        raw, self.unmatched = self.unmatched, np.empty(0, np.int16)
        self.emitted += raw.size
        return raw.astype("<i2").tobytes()

    def flush(self) -> bytes:
        """Return the remaining output so total output == total input."""
        if not self.enabled:
            return b""
        if self.engine is not None:
            try:
                out = []
                zeros = np.zeros(self.engine.delay + self.engine.hold + 2 * self.engine.BLOCK + 3,
                                 np.int16)
                y = self.engine.process(zeros)
                out.append(self._emit(y))
                if self.emitted == self.inputs:
                    return b"".join(out)
                raise RuntimeError("denoiser flush did not drain the engine")
            except Exception as exc:
                self._fail(exc)
        return self._passthrough()

    def close(self):
        if self.engine:
            self.engine.close()
            self.engine = None
