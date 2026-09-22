"""ASR 语音识别引擎 — 封装 FunASR (修复: 流式缓存持久化 + GPU 加速 + 增量累积)"""
import asyncio
import logging
import time
from typing import Callable, Optional
import numpy as np

logger = logging.getLogger("aoide.asr")


def _detect_device(preferred: str = "cuda") -> str:
    """Detect available compute device (CUDA GPU, MPS, or CPU fallback).

    Returns the actual device string to use (e.g. "cuda:0", "cpu").
    """
    if preferred in ("cuda", "gpu"):
        try:
            import torch
            if torch.cuda.is_available():
                idx = 0
                if ":" in preferred:
                    try:
                        idx = int(preferred.split(":")[1])
                    except (ValueError, IndexError):
                        idx = 0
                count = torch.cuda.device_count()
                if idx >= count:
                    logger.warning(
                        f"Requested cuda:{idx} but only {count} GPU(s) found, using cuda:0"
                    )
                    idx = 0
                device = f"cuda:{idx}"
                logger.info(
                    f"GPU detected: {torch.cuda.get_device_name(idx)} (cuda:{idx})"
                )
                return device
        except ImportError:
            pass
        logger.info("CUDA not available, falling back to CPU")
        return "cpu"

    if preferred == "mps":
        try:
            import torch
            if torch.backends.mps.is_available():
                logger.info("MPS (Apple Silicon GPU) detected")
                return "mps"
        except ImportError:
            pass
        logger.info("MPS not available, falling back to CPU")
        return "cpu"

    return preferred  # pass through explicit device like "cpu"


class ASREngine:
    """基于 FunASR 的语音识别引擎

    修复重点:
      - 流式识别 cache 状态跨调用持久化 (原 bug: 每次传入空 dict)
      - 音频缓冲管理优化
      - 线程池任务隔离
      - GPU 自动检测 (cuda / mps / cpu)
      - 中间结果增量累积 (流式 chunk 文本拼接，不再覆盖丢失)
    """

    def __init__(
        self,
        online_model: str = "paraformer-zh-streaming",
        offline_model: str = "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        vad_model: str = "fsmn-vad",
        punc_model: str = "ct-punc",
        sense_voice_model: str = "iic/SenseVoiceSmall",
        language: str = "auto",
        sample_rate: int = 16000,
        intermediate_interval: float = 0.3,
        device: str = "cuda",
        final_on_release: bool = False,
    ):
        self.sample_rate = sample_rate
        self.final_on_release = final_on_release
        self.intermediate_interval = intermediate_interval
        # SenseVoice decode language. "auto" runs its language-identification
        # head, which is what code-switched speech needs; pinning a single
        # language makes the model decode the other one as if it were this one.
        self.language = language
        self._intermediate_callback: Optional[Callable] = None
        self._offline_callback: Optional[Callable] = None
        self._models_loaded = False

        # 计算设备
        self.device = _detect_device(device)

        # 音频缓冲
        self._audio_buffer = bytearray()
        self._finalized_text = ""

        # ★ 修复: 流式解码状态 (跨多次 _transcribe_partial 调用保持)
        self._stream_cache = {}          # FunASR streaming cache
        self._stream_audio_offset = 0    # 已处理音频样本数 (int16 samples)
        # ★ 流式代数戳：每次流式状态被重置（离线同步/音频裁剪/reset）
        # 时递增；在途的解码结果若代数不匹配则丢弃，防旧音频文本
        # 追加到已含同段内容的离线文本尾部（导致重复上屏）
        self._stream_generation = 0

        # ★ 增量文本累积 (流式 chunk 结果累积，不覆盖)
        self._accumulated_raw = ""       # 所有 chunk 文本的累积结果
        self._last_raw_text = ""         # 上次返回的 raw 文本（用于 diff）

        # ★ 后台异步处理
        self._new_audio_event = asyncio.Event()
        self._processing = False         # 防止并发 executor 调用
        self._running = False
        self._processing_task = None
        self._offline_task = None        # 定期离线纠正任务
        self._offline_busy = False       # 防止并发离线调用
        self._offline_text = ""          # 离线模型最新纠正结果（整段音频的完整转写）
        self._offline_text_generation = 0  # 离线文本版本号，用于 pipeline 判断是否需要更新
        self._offline_last_text_len = 0  # 上次离线纠正后的文本长度（reset 归零）
        self._offline_last_audio_samples = 0  # 上次纠正时的音频样本数（reset 归零）
        self._simple_append = False     # True=直追加模式（离线同步后，不检测重叠）

        # 模型
        self._vad_model = None
        self._online_model = None
        self._offline_model = None
        self._punc_model = None
        self._sense_voice_model = None  # SenseVoiceSmall for mixed zh-en (offline)

        # 加载模型
        self._load_models(online_model, offline_model, vad_model, punc_model,
                          sense_voice_model)

    # ── 模型加载 ──────────────────────────────────────

    def _load_models(self, online_model, offline_model, vad_model, punc_model,
                     sense_voice_model):
        """加载 FunASR 模型（使用检测到的 GPU/CPU 设备）

        每个模型名都取自配置。名字留空表示不加载该模型 —— 在 8GB 显存的
        笔记本 GPU 上，五个模型同时常驻并不总是划算，而 SenseVoice 已经
        自带 ITN 和标点，paraformer + ct-punc 这条后备链路可以关掉。
        """
        try:
            from funasr import AutoModel

            def load(label: str, name: str, optional: bool = False):
                if not name:
                    if optional:
                        logger.info(f"{label}: disabled by config (empty name)")
                        return None
                    raise ValueError(f"{label} must be configured")
                logger.info(f"Loading {label}: {name}  (device={self.device})")
                return AutoModel(
                    model=name,
                    device=self.device,
                    disable_update=True,
                )

            self._vad_model = load("VAD model", vad_model, optional=True)
            self._online_model = load("online ASR model", online_model)
            self._offline_model = load("offline ASR model", offline_model,
                                       optional=True)
            self._punc_model = load("punctuation model", punc_model,
                                    optional=True)
            self._sense_voice_model = load("SenseVoice model",
                                           sense_voice_model, optional=True)

            if self._offline_model is None and self._sense_voice_model is None:
                raise ValueError(
                    "at least one of offline_model / sense_voice_model must be set"
                )

            logger.info(f"SenseVoice decode language: {self.language}")
            self._models_loaded = True
            logger.info("All ASR models loaded successfully")

        except ImportError:
            logger.warning("FunASR not installed. Running in MOCK mode.")
            self._models_loaded = False
        except Exception as e:
            logger.error(f"Failed to load ASR models: {e}")
            self._models_loaded = False

    # ── 回调注册 ──────────────────────────────────────

    def set_intermediate_callback(self, callback: Callable):
        self._intermediate_callback = callback

    def set_offline_callback(self, callback: Callable):
        """离线模型纠正结果回调（参数: text, generation）"""
        self._offline_callback = callback

    def get_accumulated_text(self) -> str:
        """返回当前累积的完整 ASR 文本（用于 finalize 等场景）"""
        return self._accumulated_raw

    # ── 状态管理 ──────────────────────────────────────

    def reset(self):
        """重置识别状态 (每次 speech_start 时调用)"""
        self._audio_buffer = bytearray()
        self._finalized_text = ""
        self._stream_cache = {}       # ★ 修复: 清空流式缓存
        self._stream_audio_offset = 0
        self._stream_generation += 1  # ★ 作废在途流式解码
        self._accumulated_raw = ""    # ★ 清空增量累积
        self._last_raw_text = ""
        self._offline_text = ""       # ★ 清空离线纠正结果
        self._offline_text_generation = 0
        self._offline_last_text_len = 0  # ★ 重置离线计数基准，防止跨 session 污染
        self._offline_last_audio_samples = 0  # ★ 重置音频计数基准
        self._simple_append = False     # ★ 回到正常重叠检测模式
        self._new_audio_event.clear()
        self._finalized_audio = bytearray()  # 已裁剪的已提交音频（debug 用）

    def trim_committed_audio(self, char_count: int, commit_text: str = "",
                             remaining_text: str = ""):
        """裁剪已提交部分对应的音频，实现增量离线纠正。

        pipeline commit 后调用此方法，从 _audio_buffer 头部移除对应字节，
        使后续离线纠正只处理未提交音频，避免 O(n²) 全量重算。

        ★ 估算方式（改进）：
        按字符类型加权估算每字对应的音频时长：
        - 中文/假名：1.0 单位（标准发音时长）
        - ASCII 字母数字：1.5 单位（英文/数字发音更长）
        - 标点/空白：0 单位（不发音）

        ★ 分母修复：优先用 remaining_text（pipeline buffer 提交后
        的剩余文本，来自离线纠正，与音频 buffer 严格对应）计算总权重：
            ratio = w(committed) / (w(committed) + w(remaining))
        _accumulated_raw 流式拼接可能膨胀失真，分母虚大会导致欠裁剪，
        残余音频被重复识别重复上屏（如 "windows用户服务的" 重复）。

        ★ 转写滞后余量：音频尾部约 0.6s 尚未被转写成文本
        （流式延迟 + LLM 润色等待期间新进音频），这部分不参与比例分配，
        否则会过裁剪丢字（宁欠勿过：欠裁剪由文本去重兜底，过裁剪无法恢复）。
        """
        if not char_count or not self._audio_buffer:
            return

        audio_bytes = len(self._audio_buffer)
        audio_samples = audio_bytes // 2

        # ★ 转写滞后余量：尾部 0.6s 音频视为未转写，不参与比例分配
        LAG_MARGIN_SAMPLES = int(0.6 * self.sample_rate)
        effective_samples = max(0, audio_samples - LAG_MARGIN_SAMPLES)

        # ★ 按字符类型计算"语音权重"（发音时长近似）
        def _char_weight(c: str) -> float:
            if c.isascii():
                if c.isalnum():
                    # ★ 1.5 → 0.3。旧值按中文语境逐字母拼读校准
                    # （"F-C-I-T-X"每个字母读满 1.5 拍），但连读英文
                    # 每秒飞过 12+ 字母，权重虚高导致超裁；实录：提交
                    # 152 字英文裁掉 73.5% 音频，多砍 ~4s，把后续中文
                    # "OK这次我们用中文聊聊吧…"整句砍头蒸发。
                    # 宁欠勿过：欠裁剪由文本去重兜底，过裁剪无法恢复
                    return 0.3
                else:
                    return 0.0   # ASCII 标点/空白：不发音
            else:
                return 1.0       # 中文等：标准发音时长

        # 已提交文本的权重
        if commit_text:
            committed_weight = sum(_char_weight(c) for c in commit_text)
        else:
            committed_weight = char_count  # 无文本退化为字符数

        # 全部文本的权重：优先用 committed + remaining（与音频 buffer 对应）
        if commit_text and remaining_text:
            total_weight = committed_weight + sum(
                _char_weight(c) for c in remaining_text)
            total_weight = max(1.0, total_weight)
        elif self._accumulated_raw:
            total_weight = sum(_char_weight(c) for c in self._accumulated_raw)
            total_weight = max(1.0, total_weight)
        else:
            total_weight = max(1, char_count)

        # 按权重比例估算应裁剪的音频样本数（只分配已转写部分）
        weight_ratio = min(1.0, committed_weight / total_weight)
        remove_samples = int(effective_samples * weight_ratio)
        remove_bytes = remove_samples * 2

        # 限制裁剪范围不超过 buffer
        remove_bytes = min(remove_bytes, audio_bytes)
        remove_samples = remove_bytes // 2

        if remove_bytes <= 0:
            return

        # 将裁剪的音频保存到 _finalized_audio（debug/审计用）
        self._finalized_audio.extend(self._audio_buffer[:remove_bytes])

        # 裁剪音频 buffer
        del self._audio_buffer[:remove_bytes]

        # 更新流式模型偏移（裁剪后 offset 也要相应回退）
        self._stream_audio_offset = max(0, self._stream_audio_offset - remove_samples)

        # 更新累积文本：优先用 remaining_text（权威的未提交文本），
        # 保证与裁剪后的音频 buffer 严格对应
        if remaining_text:
            self._accumulated_raw = remaining_text
        elif self._accumulated_raw and char_count <= len(self._accumulated_raw):
            self._accumulated_raw = self._accumulated_raw[char_count:]
        else:
            self._accumulated_raw = ""

        # 更新离线纠正的计数基准（让它知道文本/音频已被裁剪）
        self._offline_last_text_len = max(0, self._offline_last_text_len - char_count)
        self._offline_last_audio_samples = max(0, len(self._audio_buffer) // 2)

        # 重置流式缓存（音频裁剪后缓存状态可能不一致）
        self._stream_cache = {}
        self._last_raw_text = ""
        self._stream_generation += 1  # ★ 作废在途流式解码
        self._simple_append = True  # 裁剪后切到直追加模式

        # ★ 日志改为 INFO 级别（之前 debug 被过滤导致看起来没调用）
        logger.info(
            f"Audio trimmed: weight={committed_weight:.1f}/{total_weight:.1f} "
            f"({weight_ratio:.1%}) → {remove_bytes} bytes ({remove_samples} samples), "
            f"committed_text='{commit_text[:20]}' ({char_count} chars), "
            f"remaining: {len(self._audio_buffer)} bytes"
        )

    # ── 后台处理循环 ──────────────────────────────────

    def start_processing(self):
        """启动后台 ASR 处理协程（与音频采集并行运行）"""
        if self._processing_task is None:
            self._running = True
            self._processing_task = asyncio.create_task(self._processing_loop())
            if not self.final_on_release:
                self._offline_task = asyncio.create_task(self._periodic_offline_correction())
            logger.info("ASR background processing loop started")

    async def stop_processing(self):
        """停止后台处理"""
        self._running = False
        self._new_audio_event.set()
        for task in (self._processing_task, self._offline_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._processing_task = None
        self._offline_task = None

    async def _processing_loop(self):
        """后台处理循环：独立于音频采集，持续处理缓冲区中的新音频"""
        process_interval = 0.15  # 150ms 轮询间隔，保证低延迟
        while self._running:
            try:
                # 等待新音频事件（带超时，防止死等）
                await asyncio.wait_for(
                    self._new_audio_event.wait(), timeout=process_interval
                )
            except asyncio.TimeoutError:
                # 定期检查是否有未处理的音频
                pass
            except asyncio.CancelledError:
                break

            self._new_audio_event.clear()

            if not self._models_loaded or len(self._audio_buffer) < 800:
                continue

            # 防止并发 executor 调用
            if self._processing:
                continue
            self._processing = True

            try:
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(None, self._transcribe_partial)
                if text and text.strip():
                    logger.info(f"ASR intermediate: {text.strip()}")
                    if self._intermediate_callback:
                        await self._intermediate_callback(text.strip())
            except Exception as e:
                logger.error(f"ASR intermediate error: {e}")
            finally:
                self._processing = False

    async def _periodic_offline_correction(self):
        """按字数+时长双重触发的离线纠正任务。

        SenseVoiceSmall RTF≈0.013 (GPU)，10s音频仅需0.13s，可高频纠正。
        触发条件（满足任一即触发）:
          1. 流式模型累积新增 ≥25 字（首轮 ≥10 字）
          2. 新增录音 ≥2.5 秒 —— 兜底触发
        """
        TRIGGER_CHARS = 25       # 新增字数阈值
        FIRST_TRIGGER_CHARS = 10 # 首轮更低，快速给出纠正
        TRIGGER_AUDIO_S = 2.5    # 新增录音时长兜底触发（秒）
        FIRST_AUDIO_S = 1.5      # 首轮音频兜底
        MIN_INTERVAL = 1.0       # 最小间隔秒数（SenseVoice 很快，可高频）
        POLL_INTERVAL = 0.3      # 轮询间隔
        STATUS_EVERY = 17        # 每 ~5s 输出一次状态心跳 (POLL_INTERVAL=0.3s)

        _last_offline_at = 0.0
        _cycle_count = 0

        logger.info("Offline correction task started "
                    "(trigger: +%d chars or +%.0fs audio, min interval: %.1fs)",
                    FIRST_TRIGGER_CHARS, FIRST_AUDIO_S, MIN_INTERVAL)

        while self._running:
            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            if not self._running:
                break

            _cycle_count += 1

            if not self._models_loaded:
                if _cycle_count % STATUS_EVERY == 0:
                    logger.warning("Offline check: models not loaded, skipping")
                continue
            if self._offline_busy:
                continue

            # ★ 音频 buffer 被裁剪为空时（全部已提交），跳过离线纠正
            if not self._audio_buffer:
                continue

            current_text_len = len(self._accumulated_raw)
            new_chars = current_text_len - self._offline_last_text_len
            first_run = (self._offline_last_text_len == 0)

            current_audio_samples = len(self._audio_buffer) // 2
            new_audio_s = (current_audio_samples - self._offline_last_audio_samples) / self.sample_rate

            char_threshold = FIRST_TRIGGER_CHARS if first_run else TRIGGER_CHARS
            audio_threshold = FIRST_AUDIO_S if first_run else TRIGGER_AUDIO_S

            # 周期状态心跳
            if _cycle_count % STATUS_EVERY == 0:
                buf_len = len(self._audio_buffer)
                audio_dur = buf_len / (self.sample_rate * 2) if self.sample_rate else 0
                logger.info(
                    f"Offline check: text={current_text_len} chars "
                    f"(new={new_chars}/{char_threshold}), "
                    f"audio={audio_dur:.1f}s (new={new_audio_s:.1f}s/{audio_threshold}s), "
                    f"first_run={first_run}, busy={self._offline_busy}"
                )

            trigger_by_chars = new_chars >= char_threshold
            trigger_by_audio = new_audio_s >= audio_threshold
            if not (trigger_by_chars or trigger_by_audio):
                continue

            now = time.time()
            if now - _last_offline_at < MIN_INTERVAL:
                continue

            buf_len = len(self._audio_buffer)
            audio_dur = buf_len / (self.sample_rate * 2) if self.sample_rate else 0
            if audio_dur < 1.0:
                continue

            _last_offline_at = now
            self._offline_last_audio_samples = current_audio_samples
            self._offline_busy = True

            reason = "chars" if trigger_by_chars else "audio_duration"
            try:
                logger.info(
                    f"Offline correction trigger: "
                    f"+{new_chars} chars, +{new_audio_s:.0f}s audio "
                    f"(reason={reason}), running..."
                )
                # ★ 代数戳快照：解码期间若发生 commit 裁剪/reset，
                # 结果基于裁剪前音频，包含已提交内容，回调会让已提交文本
                # 在 buffer 复活、冲垮冻结绿区（实录："效果很棒"蒸发）
                gen_snapshot = self._stream_generation
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(None, self._run_offline_quick)
                if gen_snapshot != self._stream_generation:
                    logger.info(
                        "Offline correction dropped: audio trimmed/reset "
                        "during decode")
                    continue
                if text and text.strip():
                    self._offline_text = text.strip()
                    self._offline_text_generation += 1
                    self._offline_last_text_len = len(self._offline_text)
                    logger.info(
                        f"Offline correction (#{self._offline_text_generation}): "
                        f"{len(self._offline_text)} chars "
                        f"(+{new_chars} new, {audio_dur:.0f}s audio)"
                    )
                    if self._offline_callback:
                        await self._offline_callback(
                            self._offline_text,
                            self._offline_text_generation,
                        )
                    self._sync_streaming_from_offline()
                else:
                    logger.warning(
                        f"Offline correction returned empty text "
                        f"(audio={audio_dur:.0f}s)"
                    )
            except Exception as e:
                logger.error(f"Periodic offline correction failed: {e}", exc_info=True)
            finally:
                self._offline_busy = False

    @staticmethod
    def _clean_sense_voice_text(text: str) -> str:
        """Strip SenseVoice special tokens: <|zh|>, <|NEUTRAL|>, <|Speech|>, etc."""
        import re
        return re.sub(r'<\|[^|]+\|>', '', text).strip()

    def _run_offline_quick(self) -> str:
        """在 executor 线程中快速运行离线 ASR（SenseVoiceSmall，带 ITN，含标点）"""
        buf_snapshot = bytes(self._audio_buffer)
        if len(buf_snapshot) < 1600:  # 至少 0.1s 音频
            return ""
        audio_np = np.frombuffer(buf_snapshot, dtype=np.int16)
        audio_float = audio_np.astype(np.float32) / 32768.0
        audio_float = self._preprocess_audio(audio_float, self.sample_rate)

        try:
            # 优先使用 SenseVoiceSmall（中英混合识别 + ITN + 自带标点）
            if self._sense_voice_model:
                res = self._sense_voice_model.generate(
                    input=audio_float,
                    language=self.language,
                    use_itn=True,
                )
                if res and len(res) > 0:
                    raw = res[0].get("text", "")
                    text = self._clean_sense_voice_text(raw)
                    if text:
                        logger.debug(
                            f"Offline quick ASR (SenseVoice): "
                            f"{len(buf_snapshot)/self.sample_rate:.1f}s audio "
                            f"→ {len(text)} chars"
                        )
                        return text
                logger.debug(
                    f"Offline quick ASR (SenseVoice): returned empty text "
                    f"({len(buf_snapshot)/self.sample_rate:.1f}s audio)"
                )
                return ""

            # Fallback: 原始 paraformer 离线模型（可被配置关闭）
            if self._offline_model is None:
                return ""
            res = self._offline_model.generate(input=audio_float)
            if res and len(res) > 0:
                text = res[0].get("text", "")
                if text:
                    logger.debug(
                        f"Offline quick ASR (paraformer): {len(buf_snapshot)/self.sample_rate:.1f}s audio "
                        f"→ {len(text)} chars"
                    )
                    return text
                else:
                    logger.debug(
                        f"Offline quick ASR (paraformer): returned empty text "
                        f"({len(buf_snapshot)/self.sample_rate:.1f}s audio)"
                    )
        except Exception as e:
            logger.warning(f"Offline quick ASR failed: {e}", exc_info=True)
        return ""

    def get_offline_text(self) -> tuple:
        """返回 (离线纠正文本, 版本号)"""
        return self._offline_text, self._offline_text_generation

    def _sync_streaming_from_offline(self):
        """离线纠正后重置流式模型状态，让后续增量追加到离线文本上。

        离线模型纠正了全部音频 → 文本是权威的。
        流式模型 cache 重置 → 从当前音频位置重新开始，
        _accumulated_raw 替换为离线文本 → 后续流式 chunk 追加到离线文本尾部。
        """
        offline_text = self._offline_text
        if not offline_text:
            return
        # 替换累积文本为离线纠正结果
        self._accumulated_raw = offline_text
        self._last_raw_text = ""
        # 重置流式解码状态，从当前缓冲区末尾开始
        self._stream_cache = {}
        self._stream_audio_offset = max(0, len(self._audio_buffer) // 2)
        # ★ 作废在途流式解码：它解的是离线已覆盖的旧音频，
        # 若完成后追加会把同段内容再拼一次（重复上屏根因）
        self._stream_generation += 1
        # 直追加模式：后续流式 chunk 代表全新音频，直接追加不检测重叠
        self._simple_append = True
        logger.info(
            f"Streaming reset after offline correction: "
            f"accumulated={len(offline_text)} chars, "
            f"audio_offset={self._stream_audio_offset} samples"
        )

    # ── 音频处理 ──────────────────────────────────────

    async def process_audio(self, pcm_data: bytes):
        """接收音频数据（非阻塞：只积累缓冲区，触发后台处理）"""
        self._audio_buffer.extend(pcm_data)
        if self._models_loaded:
            self._new_audio_event.set()

    # ── 流式中间识别 (online) ──────────────────────────

    def _transcribe_partial(self) -> str:
        """★ 流式识别：只送新增的音频段，配合 cache 维持解码状态

        关键修复:
          - 只处理执行时刻已有的音频，不猜后面新增的
          - offset 只前进实际处理的样本数，防止跳帧
          - 限制单次处理时长 (MAX_CHUNK_SECONDS) 防止级联延迟
        ★ 增量累积: 流式 chunk 返回的非累积文本，由引擎拼接为完整文本。
        """
        if not self._models_loaded or len(self._audio_buffer) < 800:
            return ""

        # 快照当前缓冲区长度，防止处理过程中 buffer 继续增长导致 offset 跳帧
        buf_len_snapshot = len(self._audio_buffer)
        byte_offset = self._stream_audio_offset * 2  # samples → bytes
        gen_snapshot = self._stream_generation  # ★ 代数戳快照

        if byte_offset >= buf_len_snapshot:
            return ""

        # 限制单次处理最大时长: 最多处理 3 秒音频，避免卡死
        MAX_BYTES = self.sample_rate * 3 * 2  # 3s * 16000Hz * 2bytes/int16
        process_end = min(byte_offset + MAX_BYTES, buf_len_snapshot)
        new_bytes = bytes(self._audio_buffer[byte_offset:process_end])

        if len(new_bytes) < 800:
            return ""

        try:
            audio_np = np.frombuffer(new_bytes, dtype=np.int16)
            audio_float = audio_np.astype(np.float32) / 32768.0
            audio_float = self._preprocess_audio(audio_float, self.sample_rate)

            res = self._online_model.generate(
                input=audio_float,
                cache=self._stream_cache,    # 持久化解码状态
                is_final=False,
                chunk_size=[5, 10, 5],
            )

            # ★ 代数校验：解码期间流式状态被重置（离线同步/裁剪）
            # → 本次结果解的是旧音频，追加会重复，整体丢弃
            if gen_snapshot != self._stream_generation:
                logger.info("ASR partial dropped: stream state reset during decode")
                return ""

            # ★ 关键修复: 只前进已处理的样本数，不跳帧
            self._stream_audio_offset += len(audio_np)

            if res and len(res) > 0:
                chunk_text = (res[0].get("text", "") or "").strip()
                if chunk_text:
                    self._accumulate(chunk_text)
                    logger.info(
                        f"ASR partial result: [{chunk_text[:30]}]"
                        f"  (accumulated: {len(self._accumulated_raw)} chars)"
                    )
                    return self._accumulated_raw
                else:
                    logger.info(
                        f"ASR partial returned empty (res has {len(res)} items)"
                    )
        except Exception as e:
            logger.debug(f"Partial transcription error: {e}")
            # On error, still advance offset to avoid infinite loop;
            # use a conservative advance since audio_np may not be defined.
            try:
                self._stream_audio_offset += len(audio_np)
            except NameError:
                self._stream_audio_offset += int(len(new_bytes) // 2)

        return self._accumulated_raw if self._accumulated_raw else ""

    def _accumulate(self, chunk_text: str):
        """增量累积：抗上下文重置的拼接策略。

        正常模式: 流式 chunk 通过 LCP+后缀匹配拼回完整文本。
        直追加模式 (_simple_append=True): 离线纠正后流式模型已重置，
        后续 chunk 代表全新音频，直接追加不检测重叠。
        """
        if not chunk_text:
            return

        # 直追加模式：离线纠正后的全新流式输出，直接拼到离线文本尾部
        if self._simple_append:
            prev = self._last_raw_text
            lcp = 0
            max_lcp = min(len(chunk_text), len(prev))
            while lcp < max_lcp and chunk_text[lcp] == prev[lcp]:
                lcp += 1
            if lcp >= len(prev):
                # chunk 包含上次全部内容 → 只追加尾部新增
                new_content = chunk_text[lcp:]
                if new_content:
                    self._accumulated_raw += new_content
            elif lcp > 0:
                # 部分前缀重叠 → 追加非重叠部分
                new_content = chunk_text[lcp:]
                if new_content:
                    self._accumulated_raw += new_content
            else:
                # lcp==0: 流式模型上下文重置，chunk 是全新内容，直接整段追加
                self._accumulated_raw += chunk_text
            self._last_raw_text = chunk_text
            return

        if not self._accumulated_raw:
            self._accumulated_raw = chunk_text
            self._last_raw_text = chunk_text
            return

        prev = self._last_raw_text
        lcp = 0
        max_lcp = min(len(chunk_text), len(prev))
        while lcp < max_lcp and chunk_text[lcp] == prev[lcp]:
            lcp += 1

        if lcp >= len(prev):
            # Case A: chunk 完全包含上次结果 → 只追加尾部新增
            new_content = chunk_text[lcp:]
            if new_content:
                self._accumulated_raw += new_content
        elif lcp > 0:
            # Case B: 部分前缀重叠 → 追加非重叠部分
            new_content = chunk_text[lcp:]
            if new_content:
                self._accumulated_raw += new_content
        else:
            # Case C: lcp==0，前缀完全不重叠 — 模型窗口滑动或上下文重置
            # 不能盲目追加！先找 chunk 与 accumulated 的任意位置重叠点
            # 策略：在 accumulated_raw 尾部找 chunk 前缀的最长匹配
            best_overlap = 0
            search_limit = min(len(chunk_text), len(self._accumulated_raw), 80)
            for i in range(search_limit, 0, -1):
                if self._accumulated_raw.endswith(chunk_text[:i]):
                    best_overlap = i
                    break

            if best_overlap > 0:
                # 找到后缀重叠 → 只追加真正新增的部分
                new_content = chunk_text[best_overlap:]
                if new_content:
                    self._accumulated_raw += new_content
                logger.debug(
                    f"ASR lcp=0 → suffix overlap={best_overlap}, "
                    f"added {len(new_content)} chars"
                )
            else:
                # 无任何重叠 — 检查 chunk 是否完全冗余 (模型重启相同内容)
                if chunk_text in self._accumulated_raw:
                    logger.debug(
                        f"ASR lcp=0 → chunk already in accumulated (len={len(chunk_text)}), skip"
                    )
                else:
                    # 真正的新内容，追加
                    self._accumulated_raw += chunk_text
                    logger.debug(
                        f"ASR lcp=0 → no overlap, appending {len(chunk_text)} chars"
                    )

        self._last_raw_text = chunk_text

    # ── 最终识别 (offline + VAD + 标点) ────────────────

    async def flush_final_offline(self):
        """松键终审前的最后一次离线解码：把尚未被解码的尾部音频抛回文本链。

        流式解码有 1~2s 延迟，松键太快时尾巴音频从未进过文本，
        而 finalize 只拿 buffer 现有文本终审（实录："怎么样"只上屏到
        "怎"，0.7s 音频躺在缓冲区里蒸发）。走正常离线纠正回调，
        去重/冻结绿区逻辑照常生效；须在 pipeline.finalize() 之前调用
        （_finalizing 锁住后离线纠正会被拒收）。
        """
        if not self._models_loaded or not self._audio_buffer:
            return
        current_samples = len(self._audio_buffer) // 2
        new_samples = current_samples - self._offline_last_audio_samples
        if new_samples < int(0.2 * self.sample_rate):
            return  # 上次离线解码后没有新增音频，无需补刀
        logger.info(
            f"Final offline flush: +{new_samples / self.sample_rate:.1f}s "
            f"undecoded tail audio")
        self._offline_busy = True  # 互斥周期离线纠正
        try:
            self._offline_last_audio_samples = current_samples
            gen_snapshot = self._stream_generation
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, self._run_offline_quick)
            if gen_snapshot != self._stream_generation:
                logger.info("Final offline flush dropped: "
                            "audio trimmed/reset during decode")
                return
            if text and text.strip():
                self._offline_text = text.strip()
                self._offline_text_generation += 1
                self._offline_last_text_len = len(self._offline_text)
                if self._offline_callback:
                    await self._offline_callback(
                        self._offline_text,
                        self._offline_text_generation,
                    )
                self._sync_streaming_from_offline()
        except Exception as e:
            logger.error(f"Final offline flush failed: {e}", exc_info=True)
        finally:
            self._offline_busy = False

    async def transcribe_segments(self, chunks):
        """Final-only local fallback on VAD-bounded immutable audio chunks."""
        def decode():
            if not self._models_loaded:
                raise RuntimeError("Local ASR model is unavailable")
            model = self._sense_voice_model or self._offline_model
            texts = []
            for audio in chunks:
                result = model.generate(input=audio.astype(np.float32) / 32768,
                                        language=self.language, use_itn=True)
                text = self._clean_sense_voice_text(result[0].get("text", "")) if result else ""
                if not text.strip():
                    raise RuntimeError("Local ASR returned an empty speech segment")
                texts.append(text.strip())
            return "\n".join(texts)
        return await asyncio.to_thread(decode)

    async def finalize(self) -> str:
        """最终识别 — 使用完整离线模型"""
        if not self._audio_buffer:
            return self._finalized_text
        if not self._models_loaded:
            audio_len = len(self._audio_buffer) / self.sample_rate
            if audio_len < 0.5:
                return ""
            return "（语音识别结果 — 请安装 FunASR）"

        try:
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, self._transcribe_final)
            if text and text.strip():
                self._finalized_text = text.strip()
                logger.info(f"ASR final: {self._finalized_text}")
            return self._finalized_text
        except Exception as e:
            logger.error(f"Final transcription error: {e}")
            return self._finalized_text

    @staticmethod
    def _preprocess_audio(audio: 'np.ndarray', sample_rate: int) -> 'np.ndarray':
        """音频预处理: 去直流偏移 + 轻度降噪，提升 ASR 准确率"""
        # 去直流偏移
        audio = audio - np.mean(audio)
        # 简单峰值归一化（防止削波）
        peak = np.max(np.abs(audio))
        if peak > 0.9:
            audio = audio * (0.9 / peak)
        return audio.astype(np.float32)

    def _transcribe_final(self) -> str:
        """最终转写: SenseVoiceSmall 全量识别（中英混合 + ITN + 自带标点）

        全量送入离线模型，不做 VAD 分段（GPU 显存充足，无 OOM 风险）。
        SenseVoiceSmall 自带标点恢复和 ITN（数字/英文标准化）。
        回退到 paraformer + ct-punc 当 SenseVoice 不可用时。
        """
        audio_np = np.frombuffer(bytes(self._audio_buffer), dtype=np.int16)
        if len(audio_np) < 160:  # 至少 10ms 音频
            return ""
        audio_float = audio_np.astype(np.float32) / 32768.0
        audio_float = self._preprocess_audio(audio_float, self.sample_rate)

        audio_duration = len(audio_float) / self.sample_rate
        logger.info(f"Final offline ASR: {audio_duration:.1f}s audio (full, no VAD)")

        try:
            # 优先使用 SenseVoiceSmall（中英混合 + ITN + 标点）
            if self._sense_voice_model:
                res = self._sense_voice_model.generate(
                    input=audio_float,
                    language=self.language,
                    use_itn=True,
                )
                if res and len(res) > 0:
                    raw = res[0].get("text", "")
                    text = self._clean_sense_voice_text(raw)
                    if text:
                        logger.info(
                            f"Offline ASR final (SenseVoice, {len(text)} chars): "
                            f"{text[:80]}..."
                        )
                        return text
                    logger.info("SenseVoice ASR returned empty text, falling back to paraformer")
                else:
                    logger.info("SenseVoice ASR returned no result, falling back to paraformer")

            # Fallback: paraformer + punctuation（可被配置关闭）
            if self._offline_model is None:
                return ""
            res = self._offline_model.generate(input=audio_float)
            if not res or len(res) == 0:
                logger.info("Offline ASR returned no result")
                return ""

            text = (res[0].get("text", "") or "").strip()
            if not text:
                logger.info("Offline ASR returned empty text")
                return ""

            # 标点恢复（短文本 ≤500 字）
            if len(text) <= 500 and self._punc_model is not None:
                try:
                    punc_res = self._punc_model.generate(input=text)
                    if punc_res and len(punc_res) > 0:
                        text = punc_res[0].get("text", text)
                except Exception as e:
                    logger.warning(f"Punctuation restoration failed: {e}")

            logger.info(f"Offline ASR final (paraformer, {len(text)} chars): {text[:80]}...")
            return text

        except Exception as e:
            logger.error(f"Final transcription error: {e}", exc_info=True)
            return ""

    @property
    def is_loaded(self) -> bool:
        return self._models_loaded
