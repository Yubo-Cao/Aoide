"""Streaming cloud recognition sessions (one WebSocket per key press).

A session is fed raw 16 kHz PCM from the event loop while the trigger is held.
``feed`` never blocks and never touches the network: frames go into a bounded
queue drained by a sender task. Partial transcripts are reported through
``on_partial``; ``finish`` commits the audio and waits for the final text.

Failure policy: a session never reconnects. Any network, protocol or backlog
error marks it failed, reports ``on_failure`` once, and the caller degrades to
the local draft / batch recognizers for this utterance. The next key press may
open a new session, subject to ``StreamGate``'s bounded failure budget.
"""
import asyncio
import base64
import json
import logging
import time
from urllib.parse import urlencode

import numpy as np

logger = logging.getLogger("aoide.cloud_stream")

RATE = 16000


class StreamError(RuntimeError):
    pass


class StreamAuthError(StreamError):
    pass


def join_text(parts):
    """Concatenate transcript pieces, adding a space only between Latin words."""
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and part[0].isascii() and part[0].isalnum():
            out += " "
        elif out and out[-1] in ".,!?;:" and part[0].isascii() and part[0].isalnum():
            out += " "
        out += part
    return out


class Resampler16to24:
    """Stateful 16 kHz -> 24 kHz polyphase resampler (up 3, down 2).

    Chunk-by-chunk ``resample_poly`` would click at every frame boundary; this
    keeps the FIR state and the decimation phase across calls.
    """

    def __init__(self, taps=96):
        from scipy.signal import firwin
        self.h = (firwin(taps, 1 / 3) * 3).astype(np.float64)
        self.zi = np.zeros(len(self.h) - 1)
        self.phase = 0  # index parity of the next upsampled sample

    def process(self, pcm: bytes) -> bytes:
        from scipy.signal import lfilter
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        if not x.size:
            return b""
        up = np.zeros(x.size * 3)
        up[::3] = x
        y, self.zi = lfilter(self.h, 1.0, up, zi=self.zi)
        start = (-self.phase) % 2
        out = y[start::2]
        self.phase = (self.phase + up.size) % 2
        return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()


class StreamGate:
    """Bounds how often new sessions are attempted after failures.

    ``max_failures`` consecutive failed sessions pause streaming for
    ``cooldown`` seconds; an authentication rejection disables it until the
    backend restarts. Nothing retries within an utterance.
    """

    def __init__(self, max_failures=3, cooldown=600.0, clock=time.monotonic):
        self.max_failures = max_failures
        self.cooldown = cooldown
        self.clock = clock
        self.failures = 0
        self.paused_until = 0.0
        self.auth_failed = False

    def allowed(self):
        if self.auth_failed:
            return False
        if self.failures >= self.max_failures:
            if self.clock() < self.paused_until:
                return False
            self.failures = self.max_failures - 1  # one probe after cooldown
        return True

    def record(self, ok, auth=False):
        if auth:
            self.auth_failed = True
        if ok:
            self.failures = 0
            return
        self.failures += 1
        if self.failures >= self.max_failures:
            self.paused_until = self.clock() + self.cooldown
            logger.warning("Streaming recognition paused for %.0fs after %d failed sessions",
                           self.cooldown, self.failures)


class StreamingSession:
    """Base class: queueing, lifecycle and failure reporting."""

    name = "stream"
    MAX_BACKLOG_SECONDS = 30  # audio queued while connecting / on a slow link

    def __init__(self, url, headers, on_partial=None, on_failure=None,
                 connect_timeout=5.0, idle_timeout=20.0):
        self.url = url
        self.headers = headers
        self.on_partial = on_partial
        self.on_failure = on_failure
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.queue = asyncio.Queue()
        self.backlog = 0
        self.failed = None          # exception once failed
        self.auth_failed = False
        self.closing = False
        self.draft = ""
        self.final = asyncio.get_running_loop().create_future()
        self.usage = {}
        self.fed_samples = 0
        self.first_partial_at = None
        self.started_at = None
        self.finished_at = None
        self._task = None
        self._ws = None

    # ── public API (event loop only) ──
    def start(self):
        self.started_at = time.monotonic()
        self._task = asyncio.create_task(self._run(), name=f"aoide-{self.name}")

    def feed(self, pcm: bytes):
        if self.failed or self.closing or not pcm:
            return
        n = len(pcm) // 2
        if self.backlog + n > self.MAX_BACKLOG_SECONDS * RATE:
            self._fail(StreamError("Streaming upload backlog exceeded; network too slow"))
            return
        self.backlog += n
        self.fed_samples += n
        self.queue.put_nowait(pcm)

    @property
    def healthy(self):
        return self.failed is None

    async def finish(self, timeout):
        """Commit the audio and return the final transcript, or raise."""
        self.finished_at = time.monotonic()
        if self.failed:
            await self.abort()
            raise self.failed
        self.closing = True
        self.queue.put_nowait(None)
        try:
            return await asyncio.wait_for(asyncio.shield(self.final), timeout)
        except asyncio.TimeoutError:
            self._fail(StreamError("Timed out waiting for the streaming final transcript"))
            raise self.failed from None
        finally:
            await self.abort()

    async def abort(self):
        self.closing = True
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if not self.final.done():
            self.final.cancel()

    # ── internals ──
    def _fail(self, exc):
        if self.failed is not None or (self.final.done() and not isinstance(exc, StreamAuthError)):
            return
        self.failed = exc
        if isinstance(exc, StreamAuthError):
            self.auth_failed = True
        if not self.final.done():
            self.final.set_exception(exc)
            self.final.exception()  # mark retrieved; callers re-raise self.failed
        if not self.closing:
            logger.warning("%s failed mid-utterance (%s: %s); local draft takes over",
                           self.name, type(exc).__name__, str(exc)[:200])
        if self.on_failure:
            asyncio.get_running_loop().call_soon(self._notify_failure, exc)
        if self._task and not self._task.done() and asyncio.current_task() is not self._task:
            self._task.cancel()

    def _notify_failure(self, exc):
        result = self.on_failure(self, exc)
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)

    async def _partial(self, text):
        if text == self.draft:
            return
        self.draft = text
        if self.first_partial_at is None and text:
            self.first_partial_at = time.monotonic()
        if self.on_partial:
            try:
                await self.on_partial(self, text)
            except Exception:
                logger.exception("Partial transcript callback failed")

    def _complete(self, text):
        if not self.final.done():
            self.final.set_result(text.strip())

    async def _next_audio(self):
        pcm = await self.queue.get()
        if pcm is not None:
            self.backlog -= len(pcm) // 2
        return pcm

    async def _run(self):
        import websockets
        from websockets.exceptions import InvalidStatus
        try:
            async with websockets.connect(
                    self.url, additional_headers=self.headers, proxy=None,
                    open_timeout=self.connect_timeout, close_timeout=2,
                    max_size=4 * 1024 * 1024, ping_interval=10, ping_timeout=10) as ws:
                self._ws = ws
                await asyncio.wait_for(self._handshake(ws), self.connect_timeout)
                sender = asyncio.create_task(self._send_loop(ws))
                try:
                    await self._receive_loop(ws)
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                if not self.final.done():
                    raise StreamError("Streaming connection closed before the final transcript")
        except asyncio.CancelledError:
            raise
        except InvalidStatus as exc:
            status = exc.response.status_code
            self._fail(StreamAuthError(f"Handshake HTTP {status}") if status in (401, 403)
                       else StreamError(f"Handshake HTTP {status}"))
        except Exception as exc:  # network, protocol, JSON: all degrade the same way
            self._fail(exc if isinstance(exc, StreamError) else StreamError(f"{type(exc).__name__}: {exc}"[:300]))

    async def _receive_loop(self, ws):
        while not self.final.done():
            raw = await asyncio.wait_for(ws.recv(), self.idle_timeout)
            await self._handle(json.loads(raw))

    async def _handshake(self, ws):
        raise NotImplementedError

    async def _send_loop(self, ws):
        raise NotImplementedError

    async def _handle(self, event):
        raise NotImplementedError


class OpenAIRealtimeSession(StreamingSession):
    """OpenAI Realtime transcription session (``intent=transcription``).

    Manual turns (``turn_detection: null``): gpt-live-transcribe streams
    ``...input_audio_transcription.delta`` while audio is appended, and one
    ``...completed`` per committed item after ``input_audio_buffer.commit``.
    """

    name = "openai-realtime"
    URL = "wss://api.openai.com/v1/realtime?intent=transcription"
    CHUNK_24K = 4800 * 2  # 100 ms of 24 kHz int16

    def __init__(self, api_key, model="gpt-live-transcribe", languages=("zh", "en"),
                 keywords=(), prompt="", delay="low", url=None, **kw):
        super().__init__(url or self.URL, {"Authorization": "Bearer " + api_key}, **kw)
        self.model = model
        self.languages = list(languages or [])
        self.keywords = [k for k in keywords if k][:100]
        self.prompt = prompt
        self.delay = delay
        self.items = {}      # item_id -> {"delta": str, "final": str | None}
        self.order = []
        self.commit_sent = False
        self.committed_item = None
        self.resampler = Resampler16to24()

    def session_config(self):
        transcription = {"model": self.model}
        if self.languages:
            transcription["languages"] = self.languages
        if self.model == "gpt-live-transcribe":
            if self.delay:
                transcription["delay"] = self.delay
            if self.keywords:
                transcription["keywords"] = self.keywords
        if self.prompt:
            transcription["prompt"] = self.prompt
        return {"type": "session.update", "session": {
            "type": "transcription",
            "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": transcription,
                "turn_detection": None,
                "noise_reduction": None,
            }},
        }}

    async def _handshake(self, ws):
        await ws.send(json.dumps(self.session_config()))
        while True:
            event = json.loads(await ws.recv())
            kind = event.get("type")
            if kind == "session.updated":
                return
            if kind == "error":
                self._raise_error(event)

    def _raise_error(self, event):
        error = event.get("error") or {}
        code = str(error.get("code") or error.get("type") or "")
        message = f"Realtime error {code}: {str(error.get('message', ''))[:200]}"
        if code in ("invalid_api_key", "unauthorized", "authentication_error", "insufficient_quota"):
            raise StreamAuthError(message)
        raise StreamError(message)

    async def _send_loop(self, ws):
        pending = b""
        while True:
            pcm = await self._next_audio()
            if pcm is None:
                if pending:
                    await self._append(ws, pending)
                self.commit_sent = True
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                return
            pending += self.resampler.process(pcm)
            while len(pending) >= self.CHUNK_24K:
                await self._append(ws, pending[:self.CHUNK_24K])
                pending = pending[self.CHUNK_24K:]

    async def _append(self, ws, data):
        await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                  "audio": base64.b64encode(data).decode()}))

    def _item(self, item_id):
        if item_id not in self.items:
            self.items[item_id] = {"delta": "", "final": None}
            self.order.append(item_id)
        return self.items[item_id]

    def _text(self):
        return join_text(self.items[i]["final"] if self.items[i]["final"] is not None
                         else self.items[i]["delta"] for i in self.order)

    async def _handle(self, event):
        kind = event.get("type", "")
        if kind == "error":
            code = str((event.get("error") or {}).get("code") or "")
            if self.commit_sent and code == "input_audio_buffer_commit_empty":
                # Everything was already committed; the final is what we have.
                if all(v["final"] is not None for v in self.items.values()):
                    self._complete(self._text())
                return
            self._raise_error(event)
        elif kind == "conversation.item.input_audio_transcription.delta":
            item = self._item(event.get("item_id", ""))
            item["delta"] += event.get("delta", "")
            await self._partial(self._text())
        elif kind == "input_audio_buffer.committed":
            self._item(event.get("item_id", ""))
            if self.commit_sent:
                self.committed_item = event.get("item_id")
        elif kind == "conversation.item.input_audio_transcription.completed":
            item = self._item(event.get("item_id", ""))
            item["final"] = event.get("transcript", "")
            if isinstance(event.get("usage"), dict):
                self.usage = event["usage"]
            await self._partial(self._text())
            if (self.committed_item is not None
                    and all(v["final"] is not None for v in self.items.values())):
                self._complete(self._text())
        elif kind == "conversation.item.input_audio_transcription.failed":
            error = event.get("error") or {}
            raise StreamError(f"Realtime transcription failed: {str(error.get('message', ''))[:200]}")


class ElevenLabsRealtimeSession(StreamingSession):
    """ElevenLabs Scribe v2 Realtime (``/v1/speech-to-text/realtime``).

    NOT live-verified (no ElevenLabs key on the development machine); built
    from the published API reference and covered by a local fake server.
    VAD commits split long speech into committed segments; the final manual
    commit flushes whatever is still partial.
    """

    name = "elevenlabs-realtime"
    URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    CHUNK = 3200 * 2  # 200 ms of 16 kHz int16
    ERRORS = {"error", "auth_error", "quota_exceeded", "commit_throttled", "unaccepted_terms",
              "rate_limited", "queue_overflow", "resource_exhausted", "session_time_limit_exceeded",
              "input_error", "invalid_request", "chunk_size_exceeded", "transcriber_error"}

    def __init__(self, api_key, model="scribe_v2_realtime", language="", keyterms=(),
                 url=None, zero_retention=False, **kw):
        params = [("model_id", model), ("audio_format", "pcm_16000"),
                  ("commit_strategy", "vad")]
        if language:
            params.append(("language_code", language))
        for term in keyterms:
            params.append(("keyterms", term))
        if zero_retention:
            params.append(("enable_logging", "false"))
        super().__init__((url or self.URL) + "?" + urlencode(params),
                         {"xi-api-key": api_key}, **kw)
        self.committed = []
        self.partial = ""
        self.commit_sent = False

    async def _handshake(self, ws):
        while True:
            event = json.loads(await ws.recv())
            kind = event.get("message_type")
            if kind == "session_started":
                return
            if kind in self.ERRORS:
                self._raise_error(event)

    def _raise_error(self, event):
        kind = event.get("message_type")
        message = f"Scribe {kind}: {str(event.get('error', ''))[:200]}"
        if kind in ("auth_error", "unaccepted_terms"):
            raise StreamAuthError(message)
        raise StreamError(message)

    async def _send_loop(self, ws):
        pending = b""
        while True:
            pcm = await self._next_audio()
            if pcm is None:
                if pending:
                    await self._chunk(ws, pending)
                self.commit_sent = True
                await self._chunk(ws, b"", commit=True)
                return
            pending += pcm
            while len(pending) >= self.CHUNK:
                await self._chunk(ws, pending[:self.CHUNK])
                pending = pending[self.CHUNK:]

    async def _chunk(self, ws, data, commit=False):
        await ws.send(json.dumps({"message_type": "input_audio_chunk",
                                  "audio_base_64": base64.b64encode(data).decode(),
                                  "commit": commit, "sample_rate": RATE}))

    async def _handle(self, event):
        kind = event.get("message_type", "")
        if kind == "partial_transcript":
            self.partial = event.get("text", "")
            await self._partial(join_text(self.committed + [self.partial]))
        elif kind == "committed_transcript":
            self.committed.append(event.get("text", ""))
            self.partial = ""
            await self._partial(join_text(self.committed))
            if self.commit_sent:
                self._complete(join_text(self.committed))
        elif kind == "insufficient_audio_activity" and self.commit_sent:
            # Nothing left to commit after the last VAD segment.
            self._complete(join_text(self.committed))
        elif kind in self.ERRORS:
            self._raise_error(event)
