"""Cloud-audio denoiser: streaming frames, resampling, alignment, gating, fallback."""
import _isolation  # noqa: F401  -- must precede backend imports (no real keys/data)
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend import audio_denoise
from backend.audio_denoise import (CloudDenoiser, Downsampler, RNNoiseEngine, Upsampler,
                                   WebRTCEngine)
from backend.speech_frontend import SpeechFrontend

FIXTURE = _isolation.SPEECH_FIXTURE
# The synthetic fallback below is not speech-like enough for VAD or lag checks.
needs_speech = unittest.skipUnless(
    FIXTURE.exists(), "needs a real speech WAV (set AOIDE_TEST_SPEECH_WAV)")


def speech():
    if FIXTURE.exists():
        import soundfile as sf
        return sf.read(FIXTURE, dtype="int16")[0]
    # Fallback: amplitude-modulated harmonic signal, speech-like enough for alignment.
    t = np.arange(16000 * 3) / 16000
    env = (np.sin(2 * np.pi * 3 * t) > 0).astype(float)
    sig = sum(np.sin(2 * np.pi * f * t) / k for k, f in enumerate((180, 360, 540, 900, 1400), 1))
    return (sig * env * 6000).astype(np.int16)


def run(denoiser, audio, chunk):
    out = b"".join(denoiser.process(audio[i:i + chunk].tobytes()) for i in range(0, len(audio), chunk))
    return np.frombuffer(out + denoiser.flush(), "<i2")


def lag(y, x):
    y, x = y.astype(float), x.astype(float)
    return max(range(-40, 41), key=lambda d: float(np.dot(y[4000 + d:36000 + d], x[4000:36000])))


class ResamplerTests(unittest.TestCase):
    def test_round_trip_16k_48k_16k(self):
        t = np.arange(16000) / 16000
        x = np.sin(2 * np.pi * 440 * t) * 10000
        up, down = Upsampler(3), Downsampler(3)
        y = np.concatenate([down(up(x[i:i + 333])) for i in range(0, len(x), 333)])
        d = (up.delay + down.delay) // 3
        self.assertEqual(len(y), len(x))
        err = np.abs(y[d + 200:-200] - x[200:-200 - d]).max()
        self.assertLess(err, 10000 * 0.01)  # < 1 % of amplitude after the filter delay

    def test_chunking_does_not_change_output(self):
        x = np.random.default_rng(1).normal(size=4800) * 1000
        whole = Downsampler(3)(Upsampler(3)(x))
        up, down = Upsampler(3), Downsampler(3)
        parts = np.concatenate([down(up(x[i:i + 7])) for i in range(0, len(x), 7)])
        np.testing.assert_allclose(parts, whole, atol=1e-6)


class DenoiserTests(unittest.TestCase):
    def test_disabled_is_a_noop(self):
        d = CloudDenoiser("none")
        pcm = speech()[:1601].tobytes()
        self.assertIs(d.process(pcm), pcm)
        self.assertEqual(d.flush(), b"")
        self.assertFalse(d.enabled)

    def test_invalid_mode_fails_at_construction(self):
        with self.assertRaises(ValueError):
            CloudDenoiser("krisp")
        with self.assertRaises(ValueError):
            CloudDenoiser("webrtc", {"webrtc_level": "extreme"})

    def test_any_frame_size_is_length_exact_and_deterministic(self):
        audio = speech()[:16000 * 2 + 37]
        for mode, options in [("webrtc", {}), ("webrtc", {"webrtc_level": "very_high", "webrtc_agc": True}),
                              ("rnnoise", {}), ("rnnoise", {"rnnoise_attenuation_limit_db": 12}),
                              ("rnnoise", {"rnnoise_vad_threshold": 0.5, "rnnoise_vad_retro_ms": 60})]:
            reference = None
            for chunk in (1, 159, 480, 1601, 4800):
                d = CloudDenoiser(mode, options)
                y = run(d, audio, chunk)
                self.assertIsNone(d.failed, (mode, options, chunk))
                self.assertEqual(len(y), len(audio), (mode, options, chunk))
                if reference is None:
                    reference = y
                else:
                    np.testing.assert_array_equal(y, reference, err_msg=f"{mode} {options} {chunk}")
                d.close()

    @needs_speech
    def test_output_is_time_aligned_with_raw(self):
        audio = speech()
        for mode in ("webrtc", "rnnoise"):
            y = run(CloudDenoiser(mode, {"rnnoise_attenuation_limit_db": 20}), audio, 1600)
            self.assertLessEqual(abs(lag(y, audio)), 2, mode)

    def test_attenuation_cap_bounds_noise_reduction(self):
        noise = (np.random.default_rng(7).normal(size=16000 * 2) * 2000).astype(np.int16)
        full = run(CloudDenoiser("rnnoise"), noise, 1600)[8000:]
        capped = run(CloudDenoiser("rnnoise", {"rnnoise_attenuation_limit_db": 12}), noise, 1600)[8000:]
        rms = lambda a: float(np.sqrt(np.mean(a.astype(float) ** 2)))
        self.assertLess(rms(full), rms(noise[8000:]) * 10 ** (-12 / 20))  # full RNNoise goes deeper
        self.assertGreater(rms(capped), rms(noise[8000:]) * 10 ** (-12 / 20) * 0.8)

    def test_vad_gate_mutes_silence_and_keeps_grace(self):
        engine = RNNoiseEngine(vad_threshold=0.5, vad_grace_ms=50, vad_retro_ms=10)
        frames = []
        probs = iter([0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.9] + [0.0] * 20)
        with patch.object(engine.lib, "rnnoise_process_frame",
                          side_effect=lambda st, out, inp: (
                              np.ctypeslib.as_array((audio_denoise.ctypes.c_float * 480).from_address(out))
                              .__setitem__(slice(None), 1.0), next(probs))[1]):
            for _ in range(12):
                frames.extend(engine._frame(np.ones(480, np.float32)))
        opened = [bool(f.any()) for f in frames]
        # Frame 0 voiced; 1-5 within 50 ms grace; 6 closed; 7 opened retroactively
        # (10 ms before voiced frame 8); retro holds the last frame back.
        self.assertEqual(opened[:10], [True] * 6 + [False, True, True, True])
        self.assertEqual(len(frames), 12 - engine.retro)

    def test_engine_failure_mid_utterance_falls_back_to_exact_raw(self):
        audio = speech()[:16000]
        d = CloudDenoiser("webrtc")
        first = d.process(audio[:8000].tobytes())
        with patch.object(d.engine, "process", side_effect=RuntimeError("boom")):
            second = d.process(audio[8000:12000].tobytes())
        third = d.process(audio[12000:].tobytes())
        tail = d.flush()
        self.assertIsNotNone(d.failed)
        y = np.frombuffer(first + second + third + tail, "<i2")
        self.assertEqual(len(y), len(audio))
        k = len(first) // 2
        np.testing.assert_array_equal(y[k:], audio[k:])  # raw resumes exactly where output stopped

    def test_missing_library_sends_raw(self):
        d = CloudDenoiser("none")
        d.mode = "rnnoise"
        d.options = {"rnnoise_library": "/nonexistent/librnnoise.so"}
        d.reset()
        pcm = speech()[:3200].tobytes()
        self.assertEqual(d.process(pcm) + d.flush(), pcm)
        self.assertIsNotNone(d.failed)

    def test_reset_clears_state_between_utterances(self):
        audio = speech()[:8000]
        d = CloudDenoiser("rnnoise")
        a = run(d, audio, 1600)
        d.reset()
        b = run(d, audio, 1600)
        np.testing.assert_array_equal(a, b)

    def test_faster_than_real_time(self):
        audio = speech()
        for mode in ("webrtc", "rnnoise"):
            d = CloudDenoiser(mode)
            t = time.perf_counter()
            run(d, audio, 1600)
            self.assertLess((time.perf_counter() - t) / (len(audio) / 16000), 0.2, mode)


class FrontendCloudCopyTests(unittest.TestCase):
    @needs_speech
    def test_cloud_chunks_use_denoised_copy_at_raw_boundaries(self):
        f = SpeechFrontend(denoise=False)
        self.addCleanup(f.close)
        raw = speech()
        d = CloudDenoiser("webrtc")
        for i in range(0, len(raw), 1600):
            f.process(raw[i:i + 1600].tobytes())
            f.add_cloud(d.process(raw[i:i + 1600].tobytes()))
        f.add_cloud(d.flush())
        raw_chunks = f.finish(use_raw=True)
        cloud_chunks = f.finish(source="cloud")
        self.assertTrue(raw_chunks)
        self.assertEqual([len(c) for c in raw_chunks], [len(c) for c in cloud_chunks])
        self.assertFalse(all(np.array_equal(a, b) for a, b in zip(raw_chunks, cloud_chunks)))

    def test_misaligned_cloud_copy_falls_back_to_raw(self):
        f = SpeechFrontend(denoise=False)
        self.addCleanup(f.close)
        raw = speech()
        for i in range(0, len(raw), 1600):
            f.process(raw[i:i + 1600].tobytes())
        f.add_cloud(raw[:1000].tobytes())
        with self.assertLogs("aoide.frontend", "WARNING"):
            chunks = f.finish(source="cloud")
        for a, b in zip(chunks, f.finish(use_raw=True)):
            np.testing.assert_array_equal(a, b)


if __name__ == "__main__":
    unittest.main()
