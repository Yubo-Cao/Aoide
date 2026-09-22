"""Aoide backend service entry point"""
import asyncio
import signal
import sys
import os
import time
import logging
import logging.handlers
import argparse
import json
import subprocess
from pathlib import Path

# Strip proxy env vars to avoid SOCKS proxy interfering with urllib3/httpx
# (funasr's AutoModel does a PyPI version check that hangs on SOCKS)
for _key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
             "HTTPS_PROXY", "https_proxy", "SOCKS_PROXY", "socks_proxy"):
    os.environ.pop(_key, None)

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.asr_engine import ASREngine
from backend.llm_optimizer import LLMOptimizer
from backend.audio_capture import AudioCapture
from backend.unix_server import UnixSocketServer
from backend.pipeline import PTTPipeline
from backend.speech_frontend import SpeechFrontend
from backend.cloud_asr import CloudASR
from backend.audio_denoise import CloudDenoiser
from backend.socket_path import resolve_socket_path

# Silence shorter than this is a too-short press, not a dead microphone.
MIC_SILENCE_MIN_SECONDS = 0.5
# At most one "no microphone signal" desktop notification per interval.
MIC_NOTICE_INTERVAL = 300.0

logger = logging.getLogger("aoide")


class _ReopeningFileWriter:
    """File-like object that writes to a log file and reopens it if deleted.

    Used to redirect sys.stdout/stderr so that even C library output
    (funasr debug prints) survives log file deletion.
    """
    def __init__(self, path: str, fallback=None):
        self._path = path
        self._fallback = fallback  # backup stream (e.g. original stderr)
        self._fd = -1
        self._open()

    def _open(self):
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
        try:
            self._fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError:
            self._fd = -1

    def write(self, data):
        if self._fd >= 0:
            try:
                os.write(self._fd, data.encode() if isinstance(data, str) else data)
                return
            except (OSError, ValueError):
                pass
        # File gone or fd invalid — reopen and retry
        self._open()
        if self._fd >= 0:
            try:
                os.write(self._fd, data.encode() if isinstance(data, str) else data)
                return
            except (OSError, ValueError):
                pass
        # Last resort: fallback stream
        if self._fallback:
            self._fallback.write(data)

    def flush(self):
        if self._fd >= 0:
            try:
                os.fsync(self._fd)
            except OSError:
                pass

    def fileno(self):
        return self._fd if self._fd >= 0 else (
            self._fallback.fileno() if self._fallback else -1)


def setup_logging(level: str = "INFO", log_file: str = ""):
    fmt = "[%(asctime)s] %(name)s %(levelname)s: %(message)s"
    formatter = logging.Formatter(fmt)

    # 1) If log file requested, redirect raw stdout/stderr FIRST so the
    #    StreamHandler below captures the file writer, not /dev/null.
    if log_file:
        try:
            fw = _ReopeningFileWriter(log_file, fallback=sys.__stderr__)
            sys.stdout = fw
            sys.stderr = fw
        except Exception:
            pass

    # 2) Build handler — WatchedFileHandler writes Python logging directly
    #    to the log file and auto-recreates it on delete/rotation.
    #    (stdout/stderr are redirected separately above to capture C library
    #    debug output from funasr, which writes to the same file but won't
    #    duplicate because it's a different stream.)
    handlers: list[logging.Handler] = []
    if log_file:
        try:
            fh = logging.handlers.WatchedFileHandler(log_file)
            fh.setFormatter(formatter)
            handlers.append(fh)
        except Exception:
            pass
    # Fallback: if no log file or file handler failed, use stderr
    if not handlers:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=fmt,
        handlers=handlers,
        force=True,
    )


def load_config(config_path: str) -> dict:
    try:
        import yaml
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to load config from {config_path}: {e}")
        return {}


def main():
    parser = argparse.ArgumentParser(description="Aoide Backend Service")
    parser.add_argument("-c", "--config",
                        default=os.path.expanduser("~/.config/aoide/config.yaml"),
                        help="Config file path")
    parser.add_argument("-l", "--log-file",
                        default=os.path.expanduser("~/.config/aoide/backend.log"),
                        help="Log file path (auto-reopened if deleted)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    parser.add_argument("--memory-limit", type=int, default=0,
                        help="Hard memory limit in MB (default: 0 = disabled). "
                             "A watchdog process will SIGKILL the backend "
                             "if RSS exceeds this value.")
    args = parser.parse_args()

    setup_logging("DEBUG" if args.verbose else "INFO", log_file=args.log_file)
    config = load_config(args.config)

    socket_path = resolve_socket_path(config.get("backend", {}).get("socket_path"))

    logger.info("=" * 50)
    logger.info("Aoide Backend v0.2.0")
    logger.info("=" * 50)

    # ---- Init modules ----
    asr_config = config.get("asr", {})
    audio_config = config.get("audio", {})
    llm_config = config.get("llm", {})
    release_only = config.get("pipeline", {}).get("commit_on_release", True)
    frontend = SpeechFrontend(denoise=config.get("audio", {}).get("noise_suppression", True)) if release_only else None
    yaml_cloud_config = config.get("cloud_asr", {})
    cloud_config = yaml_cloud_config
    cloud_asr = CloudASR(cloud_config) if cloud_config.get("enabled", False) and release_only else None
    cloud_denoiser = None
    if cloud_asr:
        try:
            cloud_denoiser = CloudDenoiser(cloud_config.get("denoise", "none"),
                                           cloud_config.get("denoise_options") or {})
        except Exception as exc:
            logger.error("Cloud denoiser unavailable (%s: %s); sending raw audio",
                         type(exc).__name__, exc)
            cloud_denoiser = CloudDenoiser("none")
        logger.info("Cloud ASR: provider=%s final=%s draft=%s denoise=%s", cloud_asr.provider,
                    cloud_asr.model, "cloud" if cloud_asr.cloud_draft else "local",
                    cloud_denoiser.mode)

    # ASR engine
    logger.info("Loading ASR models...")
    try:
        asr_engine = ASREngine(
            online_model=asr_config.get("online_model", "paraformer-zh-streaming"),
            offline_model=asr_config.get("offline_model",
                "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"),
            vad_model=asr_config.get("vad_model", "fsmn-vad"),
            punc_model=asr_config.get("punc_model", "ct-punc"),
            sense_voice_model=asr_config.get("sense_voice_model",
                                             "iic/SenseVoiceSmall"),
            language=asr_config.get("language", "auto"),
            sample_rate=audio_config.get("sample_rate", 16000),
            intermediate_interval=asr_config.get("intermediate_interval", 0.3),
            device=asr_config.get("device", "cuda"),
            final_on_release=release_only,
        )
        logger.info("ASR models loaded successfully")
    except Exception as e:
        logger.error(f"Failed to load ASR models: {e}")
        logger.info("Continuing without ASR - mock mode")
        asr_engine = None

    # LLM optimizer (initially disabled, enabled by fcitx5 config)
    llm_optimizer = None
    # YAML-only knobs the addon does not mirror.
    llm_extras = dict(
        reasoning_effort=llm_config.get("reasoning_effort", ""),
        aws_profile=llm_config.get("aws_profile", ""),
        timeout=float(llm_config.get("timeout", 25)),
    )
    # When false, config.yaml is authoritative and the addon's LLM fields
    # (base_url/model/key/temperature/...) are ignored.
    addon_llm_override = llm_config.get("addon_override", True)
    if llm_config.get("enabled", False):
        llm_optimizer = LLMOptimizer(
            base_url=llm_config.get("base_url", "http://localhost:8000/v1"),
            api_key=llm_config.get("api_key", ""),
            model=llm_config.get("model", "qwen2.5-7b-instruct"),
            temperature=llm_config.get("temperature", 0.3),
            max_tokens=llm_config.get("max_tokens", 2000),
            system_prompt=llm_config.get("system_prompt", ""),
            optimize_delay=llm_config.get("optimize_delay", 0.5),
            auto_commit_delay=llm_config.get("auto_commit_delay", 0.2),
            **llm_extras,
        )
        logger.info("LLM optimizer configured: %s via %s", llm_optimizer.model, llm_optimizer.provider)
    else:
        logger.info("LLM optimization disabled in config (can be enabled via fcitx5 GUI)")

    # Audio capture — 完全由 PTT 按键控制，无需音量阈值/VAD
    audio_capture = AudioCapture(
        sample_rate=audio_config.get("sample_rate", 16000),
        channels=audio_config.get("channels", 1),
        frame_size=audio_config.get("frame_size", 4800),
        device=audio_config.get("device", None) or None,
    )
    # ---- Server setup ----
    server = UnixSocketServer(socket_path)

    # ---- PTT 流式管道 ----
    from .result_store import ResultStore
    _pipeline = PTTPipeline(server, llm_optimizer, commit_on_release=release_only,
                            result_store=ResultStore())
    _finishing = False
    _last_mic_notice = float("-inf")
    _stream = None  # streaming cloud session of the current key press
    pending_cloud_config = None

    # ---- Callbacks ----

    def _cloud_draft_active():
        return _stream is not None and cloud_asr.cloud_draft and _stream.healthy

    async def on_stream_partial(session, text):
        if session is _stream and cloud_asr.cloud_draft and text:
            await _pipeline.on_intermediate(text)

    async def on_stream_failure(session, exc):
        # Degrade to the local draft immediately; the utterance audio is kept
        # by the frontend, so the final transcript is unaffected.
        if session is _stream and cloud_asr.cloud_draft and asr_engine:
            text = asr_engine.get_accumulated_text()
            if text:
                await _pipeline.on_intermediate(text)

    async def _drop_stream():
        nonlocal _stream
        session, _stream = _stream, None
        if session is not None:
            await session.abort()

    async def _apply_cloud_config():
        """Install GUI cloud settings between utterances, preserving YAML extras."""
        nonlocal pending_cloud_config, cloud_asr, cloud_denoiser, cloud_config
        if pending_cloud_config is None:
            return
        updated = dict(yaml_cloud_config)
        if not pending_cloud_config.get("use_yaml"):
            provider = pending_cloud_config.get("provider", updated.get("provider", "openai"))
            if provider.startswith("elevenlabs"):
                updated.update({key: value for key, value in pending_cloud_config.items()
                                if key not in ("api_key", "model", "realtime_model")})
                eleven = dict(updated.get("elevenlabs") or {})
                for key in ("api_key", "model", "realtime_model"):
                    if key in pending_cloud_config:
                        eleven[key] = pending_cloud_config[key]
                updated["elevenlabs"] = eleven
            else:
                updated.update(pending_cloud_config)
        if updated == cloud_config:
            pending_cloud_config = None
            return
        try:
            candidate = CloudASR(updated) if updated.get("enabled") and release_only else None
        except (TypeError, ValueError) as exc:
            logger.error("Cloud settings rejected: %s", exc)
            pending_cloud_config = None
            return
        denoiser = None
        if candidate:
            try:
                denoiser = CloudDenoiser(updated.get("denoise", "none"),
                                         updated.get("denoise_options") or {})
            except Exception as exc:
                await candidate.close()
                logger.error("Cloud settings rejected: %s", exc)
                return
        old_asr, old_denoiser = cloud_asr, cloud_denoiser
        cloud_asr, cloud_denoiser, cloud_config = candidate, denoiser, updated
        pending_cloud_config = None
        if old_asr:
            await old_asr.close()
        if old_denoiser is not None:
            old_denoiser.close()
        logger.info("Cloud ASR settings applied: enabled=%s provider=%s draft=%s",
                    bool(cloud_asr), updated.get("provider"), updated.get("draft"))

    def _to_cloud(pcm):
        """Feed the cloud path (optional denoise); never blocks on the network."""
        if not pcm:
            return
        if cloud_denoiser is not None and cloud_denoiser.enabled and frontend:
            frontend.add_cloud(pcm)
        if _stream is not None:
            _stream.feed(pcm)  # non-blocking enqueue

    async def on_audio_data(pcm_data: bytes):
        raw = pcm_data
        if frontend:
            try:
                pcm_data = frontend.process(raw)
            except ValueError as exc:
                audio_capture.stop_listening()
                logger.warning("Recording stopped: %s", exc)
                await server.broadcast({"type": "error", "text": "单次录音已达五分钟，请松键提交。"})
                return
        if cloud_asr:
            _to_cloud(cloud_denoiser.process(raw))
        if asr_engine:
            await asr_engine.process_audio(pcm_data)

    async def on_asr_intermediate(text: str):
        """流式模型中间结果"""
        if text and not _cloud_draft_active():
            await _pipeline.on_intermediate(text)

    async def on_asr_offline(text: str, generation: int):
        """离线模型纠正结果"""
        if text:
            await _pipeline.on_offline_correction(text, generation)

    if asr_engine:
        asr_engine.set_intermediate_callback(on_asr_intermediate)
        asr_engine.set_offline_callback(on_asr_offline)

    # PTT handlers
    async def on_start_listening():
        nonlocal _stream
        # ★ 防重入：已在监听时的重复 start（典型场景：合成器
        # 吞掉松键事件后用户补按触发键救援）绝不能 reset 洗掉
        # 进行中的会话，直接忽略
        if audio_capture.is_listening or _finishing:
            logger.warning(
                "start_listening ignored: already listening "
                "(duplicate PTT press, release event likely lost)")
            return
        await _apply_cloud_config()
        try:
            await audio_capture.follow_default_device()
        except Exception as exc:
            logger.error("Cannot follow default microphone: %s", exc)
            return
        if frontend:
            frontend.reset()
        if cloud_denoiser is not None:
            cloud_denoiser.reset()
        audio_capture.start_listening()
        _pipeline.reset()
        if asr_engine:
            asr_engine.reset()
            # ★ 重新启动 ASR 后台处理（on_stop_listening 会停掉它）
            if not asr_engine._processing_task:
                asr_engine.start_processing()
            # ★ 绑定 trim 回调：commit 时裁剪已提交音频
            _pipeline.buffer._trim_audio_callback = asr_engine.trim_committed_audio
        if cloud_asr:
            await _drop_stream()
            _stream = cloud_asr.open_session(on_stream_partial, on_stream_failure)
        if server:
            await server.broadcast({"type": "reset"})

    async def _stop_impl(interrupt: bool):
        """PTT 停止公共实现。interrupt=True 为打断（只润色剩余提交），
        False 为松开（全文终审删除重推）。"""
        nonlocal _finishing
        if _finishing:
            return
        _finishing = True
        try:
            await _finish_impl(interrupt)
        finally:
            _finishing = False
            await server.broadcast({"type": "finalized"})

    async def _finish_impl(interrupt: bool):
        audio_capture.stop_listening()
        await audio_capture.drain_pending()
        nonlocal _last_mic_notice
        if audio_capture.session_peak == 0:
            # A press shorter than this has not had time to deliver audio, so
            # silence says nothing about the microphone. Key auto-repeat that
            # arrives as release/press pairs used to produce dozens of these.
            if audio_capture.session_duration < MIC_SILENCE_MIN_SECONDS:
                logger.info("PTT too short to judge the microphone (%.2fs)",
                            audio_capture.session_duration)
            else:
                logger.warning("No microphone signal during %.1fs PTT (all samples zero or missing)",
                               audio_capture.session_duration)
                now = time.monotonic()
                if now - _last_mic_notice >= MIC_NOTICE_INTERVAL:
                    _last_mic_notice = now
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "notify-send", "Aoide：麦克风没有声音", "请检查麦克风是否静音或选错了设备。")
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except (OSError, asyncio.TimeoutError):
                        logger.warning("Could not display microphone notification")
        if release_only:
            if asr_engine:
                await asr_engine.stop_processing()
            if interrupt:
                await _drop_stream()
                await _pipeline.finalize_interrupt()
                return
            # Our public-fixture comparison found that APM suppression can
            # damage proper nouns for GPT Transcribe. VAD uses the clean copy;
            # recognition receives the original samples at the same boundaries,
            # or the cloud_asr.denoise copy (same timeline) when configured.
            if cloud_asr:
                _to_cloud(cloud_denoiser.flush())
            chunks = frontend.finish(use_raw=True)
            cloud_chunks = (frontend.finish(source="cloud")
                            if cloud_denoiser is not None and cloud_denoiser.enabled else chunks)
            logger.info("Final-only speech: %d segment(s), %.2fs of %.2fs recording; AEC off",
                        len(chunks), sum(len(c) for c in chunks) / 16000, frontend.samples / 16000)
            if not chunks:
                await _drop_stream()
                _pipeline.reset()
                await server.broadcast({"type": "reset"})
                return
            text = ""
            if cloud_asr:
                try:
                    text = await cloud_asr.final(cloud_chunks, _stream)
                    logger.info("Cloud ASR completed: %s, %d chars", cloud_asr.model, len(text))
                except Exception as exc:
                    logger.warning("Cloud ASR failed (%s); using local recognition", type(exc).__name__)
                finally:
                    await _drop_stream()
            if not text and asr_engine and (cloud_asr is None or cloud_asr.local_fallback):
                try:
                    text = await asr_engine.transcribe_segments(chunks)
                except Exception as exc:
                    logger.error("Local final recognition failed: %s", type(exc).__name__)
            if text:
                await _pipeline.on_offline_correction(text, 1)
            # If both recognizers failed, retain the local draft instead of deleting it.
            await _pipeline.finalize()
            return
        # ★ 等待最后一次离线纠正完成（小步轮询，完成即走，不固定睡 0.5s）
        if asr_engine:
            for _ in range(12):  # 最多 0.6s
                if not asr_engine._offline_busy:
                    break
                await asyncio.sleep(0.05)
            # ★ 尾部音频补刀：流式解码有延迟，松键太快时尾巴
            # 音频还没进过文本（实录："怎么样"只上屏到"怎"）
            await asr_engine.flush_final_offline()
        if interrupt:
            await _pipeline.finalize_interrupt()
        else:
            await _pipeline.finalize()
        # ★ 立即停止 ASR 后台任务，防止空转
        # 下次 on_start_listening 时重新启动
        if asr_engine:
            await asr_engine.stop_processing()

    async def on_stop_listening():
        await _stop_impl(interrupt=False)

    async def on_interrupt():
        """打断收尾：用户按其他键/切焦点，润色剩余后放行拼音。"""
        await _stop_impl(interrupt=True)

    async def on_toggle():
        if audio_capture.is_listening:
            await on_stop_listening()
        else:
            await on_start_listening()

    async def on_reset():
        if asr_engine:
            asr_engine.reset()

    async def on_commit_now():
        """回车键强制提交当前 buffer 内容"""
        await _pipeline.commit_now()

    async def on_config(cmd: dict):
        """Handle config update from the fcitx5 addon."""
        nonlocal llm_optimizer, pending_cloud_config

        if isinstance(cmd.get("cloud_asr"), dict):
            pending_cloud_config = cmd["cloud_asr"]
            if not audio_capture.is_listening and not _finishing:
                await _apply_cloud_config()

        llm_cfg = cmd.get("llm", {}) if addon_llm_override else {}

        # Only create/update LLM optimizer when explicitly enabled
        llm_enabled = llm_cfg.get("enabled", llm_optimizer is not None)
        if not llm_enabled and llm_optimizer:
            # LLM was disabled → discard optimizer
            logger.info("LLM optimization disabled by fcitx5 config")
            await llm_optimizer.close()
            llm_optimizer = None
        elif llm_enabled and not llm_optimizer:
            logger.info("Creating LLM optimizer from fcitx5 config")
            llm_optimizer = LLMOptimizer(
                base_url=llm_cfg.get("base_url", "http://localhost:8000/v1"),
                api_key=llm_cfg.get("api_key", ""),
                model=llm_cfg.get("model", "qwen2.5-7b-instruct"),
                temperature=llm_cfg.get("temperature", 0.3),
                max_tokens=llm_cfg.get("max_tokens", 2000),
                optimize_delay=llm_cfg.get("optimize_delay", 0.5),
                auto_commit_delay=llm_cfg.get("auto_commit_delay", 0.2),
                **llm_extras,
            )
            logger.info(f"LLM optimizer created: {llm_cfg.get('model', 'unknown')}")
        elif llm_optimizer and llm_cfg:
            llm_optimizer.update_config(
                base_url=llm_cfg.get("base_url", llm_optimizer.base_url),
                api_key=llm_cfg.get("api_key", llm_optimizer.api_key),
                model=llm_cfg.get("model", llm_optimizer.model),
                temperature=llm_cfg.get("temperature", llm_optimizer.temperature),
                max_tokens=llm_cfg.get("max_tokens", llm_optimizer.max_tokens),
                optimize_delay=llm_cfg.get("optimize_delay", llm_optimizer.optimize_delay),
                auto_commit_delay=llm_cfg.get("auto_commit_delay", llm_optimizer.auto_commit_delay),
            )
            logger.info("LLM optimizer config updated from fcitx5 GUI")

        # ★ 同步给运行中的 pipeline：更新 optimizer 引用 + 绿区开关
        _pipeline.llm_optimizer = llm_optimizer
        _pipeline.buffer.set_llm_enabled(llm_optimizer is not None)
        logger.info(f"Pipeline LLM enabled: {llm_optimizer is not None} (green zone {'on' if llm_optimizer else 'off'})")

        # Hot-swap audio device
        old_device = audio_capture.device or ""
        new_device = cmd.get("audio_device", old_device)
        if new_device != old_device:
            audio_capture.set_device(new_device if new_device else None)
            logger.info(f"Audio device changed: '{old_device or 'default'}' → '{new_device or 'default'}'")

    server.on_audio_data = on_audio_data
    server.on_start_listening = on_start_listening
    server.on_stop_listening = on_stop_listening
    server.on_interrupt = on_interrupt
    server.on_toggle = on_toggle
    server.on_reset = on_reset
    server.on_commit_now = on_commit_now
    server.on_config = on_config

    # ---- Event loop ----
    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    async def run():
        if os.path.exists(socket_path):
            os.unlink(socket_path)

        server_task = asyncio.create_task(server.serve())
        audio_task = asyncio.create_task(
            audio_capture.start(on_audio_data)
        )

        # ★ 启动 ASR 后台处理循环（必须在 event loop 运行后）
        if asr_engine:
            asr_engine.start_processing()
            # ★ 绑定 trim 回调：commit 时裁剪已提交音频
            _pipeline.buffer._trim_audio_callback = asr_engine.trim_committed_audio

        # ★ 启动防卡死定时器
        _pipeline.start_emergency_timer()

        # ★ 内存守护进程（独立子进程，硬限制）
        _watchdog_proc = None
        mem_limit = args.memory_limit
        if mem_limit > 0:
            _watchdog_script = str(Path(__file__).parent.parent / "tools" / "memory_watchdog.py")
            try:
                _watchdog_proc = subprocess.Popen(
                    [
                        sys.executable,
                        _watchdog_script,
                        "--pid", str(os.getpid()),
                        "--limit-mb", str(mem_limit),
                        "--restart-cmd",
                        f"aoide-backend --memory-limit {mem_limit}",
                    ],
                    start_new_session=True,
                )
                logger.info(
                    f"Memory watchdog started (PID {_watchdog_proc.pid}, "
                    f"limit={mem_limit}MB)"
                )
            except Exception as e:
                logger.warning(f"Failed to start memory watchdog: {e}")
                _watchdog_proc = None

        logger.info(f"Unix socket listening on: {socket_path}")
        logger.info("Audio capture started")
        logger.info("Aoide Backend is ready!")
        logger.info("")
        if _watchdog_proc:
            logger.info(
                f"  Memory limit: {mem_limit}MB "
                f"(hard watchdog PID {_watchdog_proc.pid})"
            )
        logger.info("Usage:")
        logger.info("  Hold the trigger key (default: Right Ctrl) to speak")
        logger.info("  Release trigger key -> text appears at cursor")
        logger.info("  ESC in fcitx5: cancel preedit")
        logger.info("  Enter in fcitx5: commit preedit")
        logger.info("  F5: force LLM optimize")
        logger.info("  F6: toggle listening mode")
        logger.info("")

        await stop_event.wait()

        logger.info("Shutting down...")
        _pipeline.stop_emergency_timer()
        await audio_capture.stop()
        if llm_optimizer:
            await llm_optimizer.close()
        if cloud_asr:
            await _drop_stream()
            await cloud_asr.close()
        if frontend:
            frontend.close()
        if cloud_denoiser is not None:
            cloud_denoiser.close()
        server.stop()
        if asr_engine:
            await asr_engine.stop_processing()

        # Cancel all pending tasks gracefully
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        await asyncio.gather(server_task, audio_task, return_exceptions=True)
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        logger.info("Shutdown complete")

    loop.run_until_complete(run())
    loop.close()


if __name__ == "__main__":
    main()
