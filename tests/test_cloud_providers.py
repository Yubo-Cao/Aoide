"""Cloud ASR provider selection, streaming protocols and fallback; no external requests.

OpenAI Realtime and ElevenLabs Scribe Realtime run against a local fake
WebSocket server; batch endpoints use httpx.MockTransport.
"""
import _isolation  # noqa: F401  -- must precede backend imports (no real keys/data)
import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import numpy as np
from websockets.asyncio.server import serve

from backend.cloud_asr import CloudASR, CloudRecognizer, ElevenLabsRecognizer, keyterms
from backend.cloud_stream import (ElevenLabsRealtimeSession, OpenAIRealtimeSession,
                                  Resampler16to24, StreamAuthError, StreamError, StreamGate,
                                  join_text)
from backend.personal_dictionary import PersonalDictionary

KEYS = {"AOIDE_OPENAI_API_KEY": "sk-test-openai", "AOIDE_ELEVENLABS_API_KEY": "xi-test"}


def dictionary(terms):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(json.dumps({"terms": terms}))
    tmp.close()
    return PersonalDictionary(tmp.name).reload()


def tone(seconds, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2")


class FakeServer:
    """Local WebSocket server; ``behaviour(ws, log)`` scripts the provider."""

    def __init__(self, behaviour, status=None):
        self.behaviour = behaviour
        self.status = status
        self.connections = 0
        self.requests = []
        self.received = []

    async def __aenter__(self):
        async def process_request(connection, request):
            self.requests.append(request)
            if self.status:
                return connection.respond(self.status, "rejected\n")
        async def handler(ws):
            self.connections += 1
            try:
                await self.behaviour(ws, self.received)
            except Exception:
                pass
        self.server = await serve(handler, "127.0.0.1", 0, process_request=process_request)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    def url(self, path="/v1/realtime?intent=transcription"):
        return f"ws://127.0.0.1:{self.port}{path}"


async def openai_behaviour(ws, received, deltas=("你好", "，", "OpenAI"), drop_after=None,
                           complete=True, first_error=None):
    update = json.loads(await ws.recv())
    received.append(update)
    if first_error:
        await ws.send(json.dumps({"type": "error", "error": first_error}))
        return
    await ws.send(json.dumps({"type": "session.updated", "session": update["session"]}))
    appended = 0
    pending = list(deltas)
    async for raw in ws:
        event = json.loads(raw)
        received.append(event)
        if event["type"] == "input_audio_buffer.append":
            appended += 1
            if drop_after and appended >= drop_after:
                await ws.close()
                return
            if pending:
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta",
                                          "item_id": "item_1", "delta": pending.pop(0)}))
        elif event["type"] == "input_audio_buffer.commit":
            await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": "item_1"}))
            if complete:
                await ws.send(json.dumps({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item_1", "transcript": "你好，OpenAI。",
                    "usage": {"type": "duration", "seconds": 2}}))


class HelperTests(unittest.TestCase):
    def test_resampler_is_continuous_across_chunks(self):
        audio = tone(1.0)
        whole = np.frombuffer(Resampler16to24().process(audio.tobytes()), "<i2")
        r = Resampler16to24()
        parts = b"".join(r.process(audio[i:i + 1597].tobytes()) for i in range(0, len(audio), 1597))
        chunked = np.frombuffer(parts, "<i2")
        self.assertEqual(len(whole), 24000)
        np.testing.assert_array_equal(whole, chunked)
        # 440 Hz survives with its amplitude (filter gain compensates zero-stuffing).
        self.assertAlmostEqual(np.abs(whole[2000:]).max() / 8000, 1.0, delta=0.05)

    def test_join_text_spacing(self):
        self.assertEqual(join_text(["你好", "世界"]), "你好世界")
        self.assertEqual(join_text(["hello", "world"]), "hello world")
        self.assertEqual(join_text(["Done.", "Next", ""]), "Done. Next")

    def test_keyterms_filtering(self):
        d = dictionary([{"term": "OpenAI"}, {"term": "a" * 60}, {"term": "one two three four five six"},
                        {"term": "bad[term]"}, {"term": "OpenAI"}, {"term": "Kylian"}])
        self.assertEqual(keyterms(d), ["OpenAI", "Kylian"])

    def test_gate_bounds_failures_and_auth(self):
        now = [0.0]
        gate = StreamGate(max_failures=2, cooldown=60, clock=lambda: now[0])
        gate.record(False)
        self.assertTrue(gate.allowed())
        gate.record(False)
        self.assertFalse(gate.allowed())
        now[0] = 61
        self.assertTrue(gate.allowed())    # single probe after cooldown
        gate.record(False)
        self.assertFalse(gate.allowed())
        gate.record(True)
        self.assertTrue(gate.allowed())
        gate.record(False, auth=True)
        now[0] = 10 ** 6
        self.assertFalse(gate.allowed())


class ParallelBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_segments_run_concurrently_and_keep_order(self):
        from backend.cloud_asr import Transcript, recognize_parallel
        running, peak = [0], [0]
        async def one(chunk):
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await asyncio.sleep(0.05 * (5 - int(chunk[0])))
            running[0] -= 1
            return Transcript(f"段{int(chunk[0])}", 0, {})
        chunks = [np.full(10, i, np.int16) for i in range(5)]
        self.assertEqual(await recognize_parallel(one, chunks, concurrency=3), "段0\n段1\n段2\n段3\n段4")
        self.assertEqual(peak[0], 3)

    async def test_one_empty_segment_fails_the_utterance(self):
        from backend.cloud_asr import CloudASRError, Transcript, recognize_parallel
        async def one(chunk):
            return Transcript("" if chunk[0] == 1 else "有", 0, {})
        with self.assertRaises(CloudASRError):
            await recognize_parallel(one, [np.zeros(3, np.int16), np.ones(3, np.int16)])


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_selection(self):
        cases = {
            "openai": (CloudRecognizer, False, "gpt-transcribe"),
            "openai-realtime": (CloudRecognizer, True, "gpt-live-transcribe"),
            "elevenlabs": (ElevenLabsRecognizer, False, "scribe_v2"),
            "elevenlabs-realtime": (ElevenLabsRecognizer, True, "scribe_v2_realtime"),
        }
        for provider, (batch, streamed, model) in cases.items():
            cloud = CloudASR({"provider": provider})
            self.addAsyncCleanup(cloud.close)
            self.assertIsInstance(cloud.batch, batch)
            self.assertEqual(cloud.final_from_stream, streamed)
            self.assertEqual(cloud.streaming, streamed)
            self.assertEqual(cloud.model, model)
        legacy = CloudASR({"enabled": True, "model": "gpt-transcribe", "mode": "file"})
        self.addAsyncCleanup(legacy.close)
        self.assertEqual((legacy.provider, legacy.cloud_draft, legacy.streaming), ("openai", False, False))
        draft = CloudASR({"provider": "openai", "draft": "cloud"})
        self.addAsyncCleanup(draft.close)
        self.assertTrue(draft.streaming and draft.cloud_draft and not draft.final_from_stream)
        with self.assertRaises(ValueError):
            CloudASR({"provider": "deepgram"})
        with self.assertRaises(ValueError):
            CloudASR({"draft": "sometimes"})

    async def test_batch_fallback_can_be_disabled(self):
        cloud = CloudASR({"provider": "openai-realtime", "fallback_batch": False,
                          "local_fallback": False, "realtime_url": "ws://127.0.0.1:9/x"})
        cloud.dictionary = dictionary([])
        cloud.batch.recognize = AsyncMock(return_value="不应调用")
        self.addAsyncCleanup(cloud.close)
        self.assertFalse(cloud.local_fallback)
        with patch.dict(os.environ, KEYS):
            session = cloud.open_session()
            await asyncio.sleep(0.2)
            with self.assertRaises(Exception):
                await cloud.final([tone(0.2)], session)
        cloud.batch.recognize.assert_not_called()

    async def test_keyword_bias_is_off_by_default(self):
        async with FakeServer(openai_behaviour) as server:
            cloud = CloudASR({"provider": "openai-realtime", "realtime_url": server.url()})
            cloud.dictionary = dictionary([{"term": "OpenRouter"}])
            self.addAsyncCleanup(cloud.close)
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session()
                session.feed(tone(0.2).tobytes())
                await cloud.final([tone(0.2)], session)
        self.assertNotIn("keywords", server.received[0]["session"]["audio"]["input"]["transcription"])
        self.assertTrue(cloud.batch.dictionary_prompt)

    async def test_missing_key_disables_streaming_without_connecting(self):
        cloud = CloudASR({"provider": "elevenlabs-realtime"})
        self.addAsyncCleanup(cloud.close)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(cloud.open_session())
            self.assertIsNone(cloud.open_session())


class OpenAIRealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_partials_and_final_with_dictionary_keywords(self):
        async with FakeServer(openai_behaviour) as server:
            cloud = CloudASR({"provider": "openai-realtime", "draft": "cloud",
                              "realtime_url": server.url(), "dictionary_keywords": True})
            cloud.dictionary = dictionary([{"term": "OpenAI"}, {"term": "Kylian"}])
            self.addAsyncCleanup(cloud.close)
            partials = []
            async def on_partial(session, text):
                partials.append(text)
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session(on_partial)
            audio = tone(1.0)
            for i in range(0, len(audio), 1600):
                session.feed(audio[i:i + 1600].tobytes())
                await asyncio.sleep(0.01)
            text = await cloud.final([audio], session)
        self.assertEqual(text, "你好，OpenAI。")
        self.assertEqual(partials[:3], ["你好", "你好，", "你好，OpenAI"])
        update = server.received[0]
        audio_cfg = update["session"]["audio"]["input"]
        self.assertEqual(update["session"]["type"], "transcription")
        self.assertEqual(audio_cfg["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertIsNone(audio_cfg["turn_detection"])
        self.assertEqual(audio_cfg["transcription"]["model"], "gpt-live-transcribe")
        self.assertEqual(audio_cfg["transcription"]["languages"], ["zh", "en"])
        self.assertEqual(audio_cfg["transcription"]["keywords"], ["OpenAI", "Kylian"])
        self.assertEqual(server.requests[0].headers["Authorization"], "Bearer sk-test-openai")
        sent = b"".join(base64.b64decode(e["audio"]) for e in server.received
                        if e["type"] == "input_audio_buffer.append")
        self.assertEqual(len(sent), 24000 * 2)  # 1 s resampled to 24 kHz, nothing lost
        self.assertEqual(server.received[-1]["type"], "input_audio_buffer.commit")

    async def test_mid_utterance_drop_degrades_and_batch_takes_final(self):
        behaviour = lambda ws, rec: openai_behaviour(ws, rec, drop_after=3)
        async with FakeServer(behaviour) as server:
            cloud = CloudASR({"provider": "openai-realtime", "realtime_url": server.url()})
            cloud.dictionary = dictionary([])
            cloud.batch.recognize = AsyncMock(return_value="批量结果")
            self.addAsyncCleanup(cloud.close)
            failures = []
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session(on_failure=lambda s, e: failures.append(e))
                audio = tone(1.0)
                for i in range(0, len(audio), 1600):
                    session.feed(audio[i:i + 1600].tobytes())
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.2)
                self.assertFalse(session.healthy)
                self.assertEqual(len(failures), 1)
                text = await cloud.final([audio], session)
        self.assertEqual(text, "批量结果")
        cloud.batch.recognize.assert_awaited_once()
        self.assertEqual(server.connections, 1)  # never reconnects
        self.assertEqual(cloud.gate.failures, 1)

    async def test_final_timeout_falls_back(self):
        behaviour = lambda ws, rec: openai_behaviour(ws, rec, complete=False)
        async with FakeServer(behaviour) as server:
            cloud = CloudASR({"provider": "openai-realtime", "realtime_url": server.url(),
                              "stream_final_timeout": 0.3})
            cloud.dictionary = dictionary([])
            cloud.batch.recognize = AsyncMock(return_value="批量结果")
            self.addAsyncCleanup(cloud.close)
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session()
                session.feed(tone(0.3).tobytes())
                self.assertEqual(await cloud.final([tone(0.3)], session), "批量结果")

    async def test_auth_error_disables_streaming(self):
        behaviour = lambda ws, rec: openai_behaviour(
            ws, rec, first_error={"type": "invalid_request_error", "code": "invalid_api_key",
                                  "message": "bad key"})
        async with FakeServer(behaviour) as server:
            cloud = CloudASR({"provider": "openai-realtime", "realtime_url": server.url()})
            cloud.dictionary = dictionary([])
            self.addAsyncCleanup(cloud.close)
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session()
                await asyncio.sleep(0.3)
                self.assertIsInstance(session.failed, StreamAuthError)
                self.assertIsNone(cloud.open_session())
        self.assertEqual(server.connections, 1)

    async def test_handshake_401_is_auth_failure(self):
        async with FakeServer(openai_behaviour, status=401) as server:
            cloud = CloudASR({"provider": "openai-realtime", "draft": "cloud",
                              "realtime_url": server.url()})
            cloud.dictionary = dictionary([])
            self.addAsyncCleanup(cloud.close)
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session()
                await asyncio.sleep(0.3)
            self.assertIsInstance(session.failed, StreamAuthError)
            self.assertTrue(cloud.gate.auth_failed)

    async def test_unreachable_server_fails_fast_without_retry(self):
        cloud = CloudASR({"provider": "openai", "draft": "cloud",
                          "realtime_url": "ws://127.0.0.1:9/v1/realtime"})
        cloud.dictionary = dictionary([])
        cloud.batch.recognize = AsyncMock(return_value="批量结果")
        self.addAsyncCleanup(cloud.close)
        with patch.dict(os.environ, KEYS):
            session = cloud.open_session()
            session.feed(tone(0.2).tobytes())
            await asyncio.sleep(0.3)
            self.assertIsInstance(session.failed, StreamError)
            self.assertEqual(await cloud.final([tone(0.2)], session), "批量结果")

    async def test_backlog_overflow_fails_session_instead_of_blocking(self):
        session = OpenAIRealtimeSession("k", url="ws://127.0.0.1:9/x")
        session.MAX_BACKLOG_SECONDS = 1
        session.feed(tone(0.8).tobytes())
        self.assertTrue(session.healthy)
        session.feed(tone(0.8).tobytes())
        self.assertFalse(session.healthy)
        with self.assertRaises(StreamError):
            await session.finish(1)

    async def test_draft_only_session_is_closed_and_batch_is_final(self):
        async with FakeServer(openai_behaviour) as server:
            cloud = CloudASR({"provider": "openai", "draft": "cloud", "realtime_url": server.url()})
            cloud.dictionary = dictionary([])
            cloud.batch.recognize = AsyncMock(return_value="批量结果")
            self.addAsyncCleanup(cloud.close)
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session()
                session.feed(tone(0.3).tobytes())
                await asyncio.sleep(0.1)
                self.assertEqual(await cloud.final([tone(0.3)], session), "批量结果")
            self.assertTrue(session._task.done())
            self.assertFalse(any(e.get("type") == "input_audio_buffer.commit" for e in server.received))


async def scribe_behaviour(ws, received, error=None):
    await ws.send(json.dumps({"message_type": "session_started", "session_id": "s1", "config": {}}))
    if error:
        await ws.send(json.dumps({"message_type": error, "error": "rejected"}))
        return
    chunks = 0
    async for raw in ws:
        msg = json.loads(raw)
        received.append(msg)
        if msg["commit"]:
            await ws.send(json.dumps({"message_type": "committed_transcript", "text": "第二段 OpenAI"}))
            continue
        chunks += 1
        if chunks == 1:
            await ws.send(json.dumps({"message_type": "partial_transcript", "text": "第一"}))
        elif chunks == 2:
            await ws.send(json.dumps({"message_type": "committed_transcript", "text": "第一段。"}))
        elif chunks == 3:
            await ws.send(json.dumps({"message_type": "partial_transcript", "text": "第二段"}))


class ElevenLabsTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_vad_segments_and_manual_commit(self):
        async with FakeServer(scribe_behaviour) as server:
            cloud = CloudASR({"provider": "elevenlabs-realtime", "draft": "cloud", "dictionary_keywords": True,
                              "elevenlabs": {"realtime_url": server.url("/v1/speech-to-text/realtime")}})
            cloud.dictionary = dictionary([{"term": "OpenAI"}, {"term": "Kylian"}])
            self.addAsyncCleanup(cloud.close)
            partials = []
            async def on_partial(session, text):
                partials.append(text)
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session(on_partial)
                audio = tone(1.0)
                for i in range(0, len(audio), 1600):
                    session.feed(audio[i:i + 1600].tobytes())
                    await asyncio.sleep(0.01)
                text = await cloud.final([audio], session)
        self.assertEqual(text, "第一段。第二段 OpenAI")
        self.assertEqual(partials, ["第一", "第一段。", "第一段。第二段", "第一段。第二段 OpenAI"])
        request = server.requests[0]
        query = parse_qs(urlsplit(request.path).query)
        self.assertEqual(query["model_id"], ["scribe_v2_realtime"])
        self.assertEqual(query["audio_format"], ["pcm_16000"])
        self.assertEqual(query["commit_strategy"], ["vad"])
        self.assertEqual(query["keyterms"], ["OpenAI", "Kylian"])
        self.assertNotIn("enable_logging", query)
        self.assertEqual(request.headers["xi-api-key"], "xi-test")
        audio_msgs = [m for m in server.received if not m["commit"]]
        self.assertTrue(all(m["message_type"] == "input_audio_chunk" and m["sample_rate"] == 16000
                            for m in server.received))
        sent = b"".join(base64.b64decode(m["audio_base_64"]) for m in audio_msgs)
        self.assertEqual(sent, audio.tobytes())  # raw 16 kHz, unmodified
        self.assertTrue(server.received[-1]["commit"])

    async def test_realtime_auth_error(self):
        behaviour = lambda ws, rec: scribe_behaviour(ws, rec, error="auth_error")
        async with FakeServer(behaviour) as server:
            cloud = CloudASR({"provider": "elevenlabs-realtime",
                              "elevenlabs": {"realtime_url": server.url("/rt")}})
            cloud.dictionary = dictionary([])
            cloud.batch.recognize = AsyncMock(side_effect=AssertionError("must not be called"))
            self.addAsyncCleanup(cloud.close)
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session()
                await asyncio.sleep(0.3)
            self.assertIsInstance(session.failed, StreamAuthError)
            self.assertFalse(cloud.gate.allowed())

    async def test_realtime_quota_error_mid_stream(self):
        async def behaviour(ws, rec):
            await ws.send(json.dumps({"message_type": "session_started"}))
            await ws.recv()
            await ws.send(json.dumps({"message_type": "quota_exceeded", "error": "quota"}))
            await asyncio.sleep(1)
        async with FakeServer(behaviour) as server:
            cloud = CloudASR({"provider": "elevenlabs-realtime",
                              "elevenlabs": {"realtime_url": server.url("/rt")}})
            cloud.dictionary = dictionary([])
            cloud.batch.recognize = AsyncMock(return_value="批量")
            self.addAsyncCleanup(cloud.close)
            with patch.dict(os.environ, KEYS):
                session = cloud.open_session()
                session.feed(tone(0.5).tobytes())
                await asyncio.sleep(0.3)
                self.assertFalse(session.healthy)
                self.assertFalse(session.auth_failed)
                self.assertEqual(await cloud.final([tone(0.5)], session), "批量")

    async def test_batch_request_and_auth_stop(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 2:
                return httpx.Response(401)
            return httpx.Response(200, json={"text": "识别结果 OpenAI", "audio_duration_secs": 1.0})
        rec = ElevenLabsRecognizer(use_keyterms=True)
        rec.personal_dictionary = PersonalDictionary(dictionary([{"term": "OpenAI"}]).path)
        rec.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(rec.close)
        with patch.dict(os.environ, KEYS):
            self.assertEqual(await rec.recognize([tone(0.5)]), "识别结果 OpenAI")
            body = calls[0].content.decode("latin-1")
            self.assertEqual(calls[0].headers["xi-api-key"], "xi-test")
            self.assertEqual(str(calls[0].url), "https://api.elevenlabs.io/v1/speech-to-text")
            for field in ('name="model_id"\r\n\r\nscribe_v2', 'name="file_format"\r\n\r\npcm_s16le_16',
                          'name="keyterms"\r\n\r\nOpenAI'):
                self.assertIn(field, body)
            with self.assertRaises(Exception):
                await rec.recognize([tone(0.5)])
            with self.assertRaises(Exception):
                await rec.recognize([tone(0.5)])
        self.assertEqual(len(calls), 2)
        self.assertTrue(rec.auth_failed)


if __name__ == "__main__":
    unittest.main()
