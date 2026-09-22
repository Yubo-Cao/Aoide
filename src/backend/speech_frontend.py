"""16 kHz dictation front end: WebRTC NS + Silero segmentation, no AEC.

Silero recurrent state, onset/continuation hysteresis, preroll and hangover
follow Kylian's speech-gate.ts, adapted from 8 kHz telephony to 16 kHz PCM.
VAD selects ASR segments; it never commits text or ends push-to-talk.
"""
import ctypes
from pathlib import Path

import numpy as np


RATE = 16000


class Denoiser:
    def __init__(self, path=None):
        path = path or Path.home() / ".local/lib/yuhuang/libdenoise.so"
        self.lib = ctypes.CDLL(str(path))
        self.lib.yh_denoise_create.restype = ctypes.c_void_p
        self.lib.yh_denoise_free.argtypes = [ctypes.c_void_p]
        self.lib.yh_denoise_reset.argtypes = [ctypes.c_void_p]
        self.lib.yh_denoise_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.c_int]
        self.state = self.lib.yh_denoise_create()
        if not self.state:
            raise RuntimeError("WebRTC noise suppressor could not initialize")

    def reset(self):
        if self.lib.yh_denoise_reset(self.state):
            raise RuntimeError("WebRTC noise suppressor reset failed")

    def process(self, audio):
        src = np.ascontiguousarray(audio, dtype=np.int16)
        out = np.empty_like(src)
        if self.lib.yh_denoise_process(self.state, src.ctypes.data, out.ctypes.data, len(src)):
            raise RuntimeError("WebRTC noise suppression failed")
        return out

    def close(self):
        if self.state:
            self.lib.yh_denoise_free(self.state)
            self.state = None


class SpeechFrontend:
    # Same temporal decisions as Kylian, with 512 samples + 64 context at 16 kHz.
    CHUNK = 512
    CONTEXT = 64
    ONSET = 0.7
    CONTINUE = 0.45
    PREROLL = 0.30
    HANGOVER = 9
    MAX_SECONDS = 300

    def __init__(self, denoise=True, model_path=None):
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        path = model_path or Path.home() / ".local/share/yuhuang/models/silero_vad.onnx"
        self.vad = ort.InferenceSession(str(path), sess_options=options,
                                       providers=["CPUExecutionProvider"])
        self.denoiser = Denoiser() if denoise else None
        self.reset()

    def reset(self):
        if self.denoiser:
            self.denoiser.reset()
        self.state = np.zeros((2, 1, 128), np.float32)
        self.context = np.zeros(self.CONTEXT, np.float32)
        self.pending = np.empty(0, np.int16)
        self.vad_pending = np.empty(0, np.int16)
        self.parts = []
        self.raw_parts = []
        self.probabilities = []
        self.samples = 0
        self.finished = False

    def process(self, pcm):
        if self.finished:
            raise RuntimeError("Audio received after finalization")
        values = np.frombuffer(pcm, dtype=np.int16)
        if self.samples + len(self.pending) + len(values) > self.MAX_SECONDS * RATE:
            raise ValueError("Dictation is limited to five minutes per key press")
        values = np.concatenate((self.pending, values))
        count = len(values) // 160 * 160
        self.pending = values[count:].copy()
        if not count:
            return b""
        self.raw_parts.append(values[:count].copy())
        cleaned = self.denoiser.process(values[:count]) if self.denoiser else values[:count].copy()
        self._accept(cleaned)
        return cleaned.tobytes()

    def _accept(self, cleaned):
        self.parts.append(cleaned)
        self.samples += len(cleaned)
        values = np.concatenate((self.vad_pending, cleaned))
        count = len(values) // self.CHUNK * self.CHUNK
        for offset in range(0, count, self.CHUNK):
            self._classify(values[offset:offset + self.CHUNK])
        self.vad_pending = values[count:].copy()

    def _classify(self, samples):
        audio = samples.astype(np.float32) / 32768
        inp = np.concatenate((self.context, audio)).reshape(1, -1)
        prob, self.state = self.vad.run(None, {
            "input": inp, "state": self.state, "sr": np.array([RATE], np.int64)})
        self.context = audio[-self.CONTEXT:].copy()
        self.probabilities.append(float(prob.reshape(-1)[0]))

    def finish(self, use_raw=False):
        """Flush every real sample exactly once, then return contiguous speech chunks."""
        if not self.finished:
            if self.pending.size:
                n = len(self.pending)
                self.raw_parts.append(self.pending.copy())
                padded = np.pad(self.pending, (0, 160 - n))
                cleaned = self.denoiser.process(padded) if self.denoiser else padded
                self._accept(cleaned[:n])
                self.pending = np.empty(0, np.int16)
            if self.vad_pending.size:
                self._classify(np.pad(self.vad_pending, (0, self.CHUNK - len(self.vad_pending))))
                self.vad_pending = np.empty(0, np.int16)
            self.finished = True
        parts = self.raw_parts if use_raw else self.parts
        audio = np.concatenate(parts) if parts else np.empty(0, np.int16)
        intervals = self._speech_intervals(len(audio))
        chunks = []
        # Merge close phrases while keeping at most 25 seconds per ASR request.
        merged = []
        for start, end in intervals:
            if merged and end - merged[-1][0] <= 25 * RATE:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        for start, end in merged:
            while end - start > 25 * RATE:
                # Choose the least speech-like 32 ms window in the last 5 s.
                lo, hi = (start + 20 * RATE) // self.CHUNK, (start + 25 * RATE) // self.CHUNK
                cut = (lo + int(np.argmin(self.probabilities[lo:hi])) + 1) * self.CHUNK
                chunks.append(audio[start:cut].copy())
                start = cut
            if end > start:
                chunks.append(audio[start:end].copy())
        return chunks

    def _speech_intervals(self, total):
        intervals = []
        opened = False
        onset = hangover = 0
        since_strong = 17
        begin = 0
        for i, prob in enumerate(self.probabilities):
            if prob >= self.ONSET:
                since_strong = 0
                onset += 1
                hangover = self.HANGOVER
                if onset >= 2 and not opened:
                    opened = True
                    begin = max(0, (i - 1) * self.CHUNK - int(self.PREROLL * RATE))
            else:
                onset = 0
                since_strong += 1
                if opened and prob >= self.CONTINUE and since_strong <= 16:
                    hangover = self.HANGOVER
                elif hangover:
                    hangover -= 1
                    if not hangover and opened:
                        intervals.append((begin, min(total, (i + 1) * self.CHUNK)))
                        opened = False
        if opened:
            intervals.append((begin, total))
        # Preroll may overlap the previous phrase; never transcribe overlap twice.
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return merged

    def close(self):
        if self.denoiser:
            self.denoiser.close()
