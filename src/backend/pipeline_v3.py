"""YuHuang PTT Pipeline v3.8 — 三区间字符串模型 + 增量音频裁剪 + 润色结果切句提交

v3.8 变更摘要:
  - 冻结绿区：离线重识别不再改写绿区文本（对同段音频的输出会反复
    抖动，导致真实 LLM 5~7s 润色期间头部校验永远失败、绿区无法上屏）；
    锚点/序列对齐拼接绿区+新黄红区，副作用顺带丢弃头部幽灵残余字
  - commit_refined 校验失败从静默丢弃改为 warning 日志（可观测）
  - LLM_FINAL_TIMEOUT 3s→6s：真实 LLM 单次 5~7s，3s 等于永远吃不到终审结果

v3.7 变更摘要:
  - 切点改在润色结果上决定：整个绿区送 LLM，在润色文（标点由 LLM
    规范过，无假句号/漏句号）上找最后一个强边界切句，difflib 序列对齐
    把切点映射回原文位置后提交（音频裁剪/buffer 弹出仍以原文前缀为准）
  - 原文残句留在绿区与后续新文字合并进下一轮润色（周而复始）

v3.6 变更摘要:
  - 句子优先提交：常态只在强边界（。！？；）处整句上屏，边界后
    残句留在绿区与后续新文字合并再润（周而复始）；停顿超时/绿区超限
    才放宽到逗号/语气词兜底（_try_commit/_find_commit_point 的 relaxed）

v3.5 变更摘要:
  - 跨段衔接：润色请求携带上文（已定稿尾部）+下文（后续粗识别），
    LLM 可基于跨段语义纠错并保证段界标点/重字衔接自然
  - _strip_context_echo 防回显：剥离 LLM 输出中的标签/上文尾部回显

v3.4 变更摘要:
  - 绿区超限强制提交改走 LLM 润色通道（旧版 _do_commit 原文绕过润色，
    导致 "vebco与火爆" 这类残次词直接上屏）
  - finalize 终审提速：超时 8s→3s；长文本按语义边界切两段并行润色；
    终审等待期间先渲染全绿 preedit 给用户即时反馈

v3.3 变更摘要:
  - LLM 模式提交链路重构：绿区头部先送 LLM 润色、后提交（超时回退原文）
  - 删除 _try_llm_refine（存在润色文本 append 到尾部的乱序 bug）
  - finalize 加 _finalizing 锁：阻断 LLM 等待期间离线纠正/应急提交的竞态重复
  - _strip_committed_overlap 检查窗口降到 1 字（1~2 字裁剪残余漏网）
  - 离线纠正头部残留标点清理（消除 "，。" 双标点）

v3.1 变更摘要:
  - 删除 _find_committed_offset：commit 后裁剪音频，离线纠正输出天然是增量
  - 新增 _trim_audio_callback：commit 时通知 ASR 引擎裁剪已提交音频
  - 修复 _do_commit strip 不一致
  - 修复 finalize 丢字：直接用 buffer.full_text
  - 修复语义边界：WEAK_BOUNDARIES 加最小位置约束 + ASCII 序列保护
  - 删除 DISPLAY_LINE_WIDTH 硬换行
  - preedit 只渲染黄+红区（绿区 commit 后清空，不再混入 preedit）
"""

import asyncio
import difflib
import time
import logging
from collections import deque
from typing import Optional, List, Tuple, Callable

logger = logging.getLogger("yuhuang.pipeline")


class CandidateBuffer:
    """
    三区间字符串模型：绿(安全区) → 黄(修正中) → 红(实时流)

    基于离线模型回溯射程（10~30字）的距离判定:
      - 红区: 尾部 0~10 字 — 流式草稿，离线模型必重写
      - 黄区: 尾部 10~30 字 — 离线修正射程内，上限 20 字
      - 绿区: 尾部 30+ 字 — 安全区，可立即提交

    纯字数距离判定 + 语义边界对齐，不依赖置信度。
    """

    # === 配置参数（基于 SenseVoice 回溯射程实证）===
    RED_MAX_SIZE = 10          # 红区最大字数（流式模型刚输出，必被改写）
    MAX_YELLOW_SIZE = 20       # 黄区最大字数（离线修正射程约 30 字，红 10+黄 20）
    MIN_COMMIT_CHARS = 20      # 最小提交字数（绿区距尾部足够远即可提交）
    SAFE_DISTANCE = 30         # 安全距离 — 距尾部超过此值可提交
    STABLE_TIME_THRESHOLD = 3.0  # 绿区稳定超时兜底（长停顿场景）
    FORCE_COMMIT_SIZE = 60     # 绿区超限强制提交
    YELLOW_STABLE_TIMEOUT = 3.0  # 黄区稳定超时推绿（长停顿场景）

    # 语义边界字符集
    STRONG_BOUNDARIES = frozenset({'。', '！', '？', '；', '\n'})
    MEDIUM_BOUNDARIES = frozenset({'，', '、', '：'})
    WEAK_BOUNDARIES = frozenset({'呢', '啊', '吧', '吗', '嘛', '哦', '哈'})

    def __init__(self):
        self._chars: deque[str] = deque()
        self._green_end: int = 0
        self._yellow_end: int = 0
        self._committed_chars: int = 0          # 整数计数器：已提交的总字数
        self._last_commit_text: str = ""         # 最近一次提交的文本（日志/调试用）
        self._last_commit_raw: str = ""          # 最近一次提交对应的 ASR 原文（去重匹配用）
        self._last_yellow_modified: float = 0
        self._last_green_modified: float = 0
        self._last_commit_time: float = time.time()
        self._showing_placeholder: bool = False

        # ★ trim 回调：commit 时通知 ASR 引擎裁剪对应音频
        self._trim_audio_callback: Optional[Callable[[int], None]] = None

        # ★ 提交回调：由 PTTPipelineV3 注入
        self._notify_commit: Callable[[str], None] = lambda text: None

        # ★ 润色请求回调：由 PTTPipelineV3 注入
        # v3.7: 参数为 (整个绿区原文, relaxed)，切点由润色结果决定
        self._request_refine: Optional[Callable[[str, bool], None]] = None

        # ★ LLM 可用性标志：决定是否有绿区
        # False（无 LLM）：绿区恒为 0，黄区稳定头部直接 commit
        # True（有 LLM）：黄区稳定 → 绿区（LLM润色）→ commit
        self._llm_enabled: bool = False

        # ★ v3.8 冻结绿区长度：前 N 字已冻结，不再被离线重识别改写，
        # 且 _recalc_zones 不得让绿区边界回缩到冻结区内
        self._frozen_green_len: int = 0

    def set_llm_enabled(self, enabled: bool):
        """设置 LLM 是否可用，决定是否启用绿区。"""
        self._llm_enabled = bool(enabled)

    # ============ 属性 ============

    @property
    def green_text(self) -> str:
        return ''.join(list(self._chars)[:self._green_end])

    @property
    def yellow_text(self) -> str:
        return ''.join(list(self._chars)[self._green_end:self._yellow_end])

    @property
    def red_text(self) -> str:
        return ''.join(list(self._chars)[self._yellow_end:])

    @property
    def full_text(self) -> str:
        return ''.join(self._chars)

    # ============ 流式输入 ============

    def append_streaming(self, text: str):
        """直接追加流式文本到 buffer 尾部（红区）。"""
        for ch in text:
            self._chars.append(ch)
        self._showing_placeholder = False

    def update_streaming(self, full_text: str):
        """更新红区流式文本。

        流式模型输出不可靠（断句、错字），只放入红区显示。
        离线纠正输出只覆盖未提交音频，find 失败时保留黄区、替换红区。
        """
        buf_str = self.full_text
        if not buf_str:
            # 空 buffer：直接追加全部流式文本
            for ch in full_text:
                self._chars.append(ch)
            return

        idx = full_text.find(buf_str)
        if idx >= 0:
            # buffer 文本在流式全文中找到 → 仅追加尾部新字符到红区
            new_chars = full_text[idx + len(buf_str):]
            for ch in new_chars:
                self._chars.append(ch)
        else:
            # buffer 文本不在流式全文中（头部含裁剪残余/标点抖动，
            # 两边位置索引不可直接互换）
            # ★ v3.8.1：先锚点对齐保留区尾部再替换红区，避免错位重复
            # （实测：流式头部多 7 字残余 → full_text[preserved:] 错位
            # 把 "FCITX5，" 重复灌进红区）
            preserved = self._yellow_end
            kept = buf_str[:preserved]
            rest = self._align_head(kept, full_text) if kept else None
            if rest is not None:
                while len(self._chars) > preserved:
                    self._chars.pop()
                for ch in rest:
                    self._chars.append(ch)
                return
            # 对齐失败兜底：保留黄区，按位置替换红区（旧行为）
            if len(full_text) < preserved:
                while len(self._chars) > preserved:
                    self._chars.pop()
                return
            new_red = full_text[preserved:]
            while len(self._chars) > preserved:
                self._chars.pop()
            for ch in new_red:
                self._chars.append(ch)

    # ============ 离线修正 ============

    def apply_offline_correction(self, offline_text: str):
        """离线模型纠正：直接替换 buffer，重新划分三区。

        因为 commit 后音频被裁剪，离线纠正输出天然只覆盖未提交部分。
        但音频裁剪基于权重估算，边界可能有 1-2 字残余，导致离线纠正
        重复识别已提交文本的尾部（如 "44" 残留）。
        这里做防御性文本去重：去掉离线文本开头与上次 commit 结尾的重叠。
        """
        if not offline_text:
            return

        # ★ 防御性去重：去掉离线文本开头与上次 commit 结尾的重叠
        offline_text = self._strip_committed_overlap(offline_text)

        # ★ 头部残留标点清理：句子不应以标点开头
        # （裁剪残余重识别常产生 "，xxx"，与上次 commit 尾标点拼成双标点）
        offline_text = offline_text.lstrip('。，！？；、：,.!?;: ')
        if not offline_text:
            return

        # ★ v3.8 冻结绿区：绿区文本不再被离线重识别改写
        # 离线全量重识别对同段音频的输出会反复抖动（web coding/webco/
        # webcoing），若任其改写绿区头部，LLM 润色（5~7s）完成时的
        # 头部一致性校验将永远失败，绿区永远无法上屏（实测 6 连败）
        self._frozen_green_len = 0
        if self._llm_enabled and self._green_end > 0:
            green = ''.join(list(self._chars)[:self._green_end])
            if green:
                offline_text = self._align_frozen_green(green, offline_text)
                if offline_text.startswith(green):
                    self._frozen_green_len = len(green)

        # 直接替换 buffer 为离线纠正全文
        self._chars.clear()
        for ch in offline_text:
            self._chars.append(ch)

        # 按距离重新划分三区（冻结下限在 _recalc_zones 内统一钳制）
        self._recalc_zones()
        self._last_yellow_modified = time.time()

        # 尝试提交稳定头部
        self._try_commit()

    @staticmethod
    def _align_head(head: str, text: str) -> Optional[str]:
        """在 text 头部窗口内定位 head 对应的终点，返回终点之后的剩余文本。

        head 与 text 头部是同段音频的两次识别，存在字词/标点抖动。
        - 锚点用去尾标点的核心文本（尾标点是抖动高发位："一个，"↔"一个。"）
        - 锚点失配用序列对齐兜底：跳过 sentinel 空块，用最后真实匹配块尾
          1:1 外推 head 终点（否则空块会映射到 text 末尾越界）
        - 完全对不上返回 None，由调用方决定兜底策略
        """
        _PUNCT = '。，！？；、：,.!?;: '
        search_end = min(len(text), len(head) + 15)
        core = head.rstrip(_PUNCT)
        trailing = head[len(core):]
        for n in (10, 6, 4, 3):
            if n > len(core):
                continue
            tail = core[-n:]
            pos = text.rfind(tail, 0, search_end)
            if pos >= 0:
                rest = text[pos + n:]
                if trailing:
                    # head 已带尾标点 → 跳过 text 中对应的抖动标点
                    rest = rest.lstrip(_PUNCT)
                return rest
        # 锚点失配：序列对齐（只对齐 text 头部窗口，避免长文本稀释 ratio）
        sm = difflib.SequenceMatcher(None, head, text[:search_end], autojunk=False)
        if sm.ratio() >= 0.5:
            end = 0
            for a, b, size in sm.get_matching_blocks():
                if size == 0:
                    continue  # sentinel 空块，跳过
                # 块尾到 head 终点按 1:1 外推（最后真实块的结果生效）
                end = b + size + (len(head) - (a + size))
            end = min(end, search_end)
            if end > 0:
                rest = text[end:]
                if trailing:
                    rest = rest.lstrip(_PUNCT)
                return rest
        return None

    def _align_frozen_green(self, green: str, offline_text: str) -> str:
        """冻结绿区：在离线文本中定位绿区对应终点，拼接 绿区+后续部分。

        对齐逻辑见 _align_head；完全对不上（识别彻底翻篇，罕见）才放弃冻结。
        副作用：离线文本头部的裁剪残余幽灵字（如"黄"）会被自然丢弃。
        """
        rest = self._align_head(green, offline_text)
        if rest is not None:
            return green + rest
        logger.warning(
            f"Frozen green alignment failed, falling back to full replace "
            f"(green='{green[:15]}...', offline='{offline_text[:15]}...')")
        return offline_text

    def _strip_committed_overlap(self, offline_text: str) -> str:
        """去掉离线纠正文本开头与上次 commit 结尾的重叠部分。

        场景：commit "1点44。" 后音频裁剪留了 "44" 残余，
        下次离线纠正输出 "44我今天来..." → 去掉开头 "44"。

        ★ v3.2 修复：
        - 忽略上次 commit 末尾的标点再匹配（重识别的残余音频不含标点）
        - 检查窗口 10 → 20 字（欠裁剪时残余可能超过 10 字）
        ★ v3.3 修复：
        - 最小后缀降到 1 字（1~2 字残余如 "法"、"44" 之前永远漏网）
        - 同时匹配 ASR 原文尾部（LLM 润色后提交文本可能≠残余音频对应的原文）
        """
        if not offline_text:
            return offline_text

        # 优先用 ASR 原文尾部匹配（残余音频重识别出的是原文），其次用提交文本
        candidates = []
        if self._last_commit_raw:
            candidates.append(self._last_commit_raw)
        if self._last_commit_text and self._last_commit_text != self._last_commit_raw:
            candidates.append(self._last_commit_text)

        for last_commit in candidates:
            # 忽略尾部标点后再做后缀匹配
            base = last_commit.rstrip('。，！？；、：,.!?;: ')
            if not base:
                continue

            # 从最长后缀开始检查（最多 20 字，最少 1 字）
            max_check = min(20, len(base))
            for n in range(max_check, 0, -1):
                suffix = base[-n:]
                if offline_text.startswith(suffix):
                    remaining = offline_text[n:]
                    if not remaining:
                        # 离线文本全部是残余 → 整体丢弃
                        return ""
                    # 确认这是真正的重叠（后缀后跟着不同内容）
                    if remaining[0] != suffix[-1]:
                        logger.debug(
                            f"Stripped committed overlap: '{suffix}' "
                            f"({n} chars) from offline correction head"
                        )
                        return remaining
        return offline_text

    # ============ 三区划分（距离驱动）============

    def _recalc_zones(self):
        """按字数距离从尾部反推三区边界，对齐语义边界。

        ★ LLM 状态感知：
        - 无 LLM（_llm_enabled=False）：绿区恒为 0，黄区稳定头部直接 commit
        - 有 LLM（_llm_enabled=True）：黄区稳定 → 绿区（LLM润色）→ commit

        有 LLM 时（≥60字）:
          红 = 尾 10 字, 黄 = 中间 20 字, 绿 = 前 30+ 字
        无 LLM 时:
          红 = 尾 10 字, 黄 = 前 N 字（稳定头部直接 commit）, 绿 = 0
        """
        L = len(self._chars)
        if L == 0:
            self._green_end = 0
            self._yellow_end = 0
            return

        # 红/黄边界：距尾部 RED_MAX_SIZE 字
        yellow_end = max(0, L - self.RED_MAX_SIZE)
        if yellow_end > 0:
            prefix = ''.join(list(self._chars)[:yellow_end])
            b = self._find_semantic_boundary(prefix, min_chars=5)
            if b > 0:
                yellow_end = b

        # ★ 无 LLM：绿区恒为 0，黄区稳定头部直接 commit
        if not self._llm_enabled:
            self._yellow_end = yellow_end
            self._green_end = 0
            self._last_green_modified = time.time()
            return

        # 有 LLM：绿/黄边界
        # - 充裕：绿区 ≥ MIN_COMMIT_CHARS，黄区 = MAX_YELLOW_SIZE
        # - 有限：绿区 = MIN_COMMIT_CHARS（黄区自动缩减）
        # - 不足：等更多文本
        if yellow_end >= self.MIN_COMMIT_CHARS + self.MAX_YELLOW_SIZE:
            green_end = yellow_end - self.MAX_YELLOW_SIZE
        elif yellow_end >= self.MIN_COMMIT_CHARS:
            green_end = self.MIN_COMMIT_CHARS
        else:
            green_end = 0

        # 对齐到语义边界
        if green_end > 0:
            prefix = ''.join(list(self._chars)[:green_end])
            b = self._find_semantic_boundary(prefix, min_chars=5)
            if b > 0:
                green_end = b
            # ★ v3.8.5 避开英文词/数字中间：无标点回退和 60 字硬切分
            #   都可能落在 ASCII 连续序列内（"lin|ux"），绿区尾部残词
            #   会诱使 LLM 顺下文续写（v3.8.4 实录事故诱因）
            green_end = self._snap_out_of_ascii_run(
                list(self._chars), green_end)

        self._yellow_end = yellow_end
        self._green_end = green_end
        # ★ v3.8 冻结钳制：冻结文本必须整体留在绿区内
        # （防语义边界对齐使绿区回缩，冻结尾部掉回黄区后又被改写）
        if self._frozen_green_len > 0:
            self._green_end = max(
                self._green_end,
                min(self._frozen_green_len, self._yellow_end))
        self._last_green_modified = time.time()

    # ============ 提交 ============

    def _try_commit(self, relaxed: bool = False):
        """尝试提交稳定头部文本。

        ★ LLM 状态感知：
        - 无 LLM：从黄区头部提交（黄区稳定头部直接 commit）
        - 有 LLM：从绿区头部提交（绿区 = LLM 润色后的稳定文本）

        ★ v3.6 句子优先：relaxed=False 时只在强边界（句号/叹号/问号）处
        切分上屏，边界后的残句留在绿区与后续新文字合并再润；
        relaxed=True（停顿超时/绿区超限）才允许逗号/语气词兜底。
        """
        if not self._llm_enabled:
            # 无 LLM：黄区稳定头部直接提交（无回炉收益，保持逗号级切分）
            if self._yellow_end < self.MIN_COMMIT_CHARS:
                return
            yellow_text = self.yellow_text
            commit_point = self._find_commit_point(yellow_text, relaxed=True)
            if commit_point <= 0:
                return
            self._do_commit(yellow_text[:commit_point])
        else:
            # 有 LLM（v3.7）：整个绿区送润，切点在润色结果的可靠标点上决定
            # （原文标点可能有假句号/漏句号，在原文上切会把半句当整句）
            if self._green_end < self.MIN_COMMIT_CHARS:
                return
            green = self.green_text
            if self._request_refine is not None:
                # 门控：绿区含任意边界/已放宽/已超限才送润，
                # 避免无边界短文本反复空跑 LLM
                has_boundary = any(
                    (c in self.STRONG_BOUNDARIES
                     or c in self.MEDIUM_BOUNDARIES
                     or c in self.WEAK_BOUNDARIES) for c in green)
                if has_boundary or relaxed or len(green) >= self.FORCE_COMMIT_SIZE:
                    self._request_refine(green, relaxed)
            else:
                # 未注入润色回调：回退原文切分直接提交
                commit_point = self._find_commit_point(green, relaxed=relaxed)
                if commit_point > 0:
                    self._do_commit(green[:commit_point])

    def _do_commit(self, text: str):
        """提交文本，弹出字符，递增计数器，通知音频裁剪。

        ★ 关键修复：
        - commit_len 使用 strip 后的长度，与实际提交文本一致
        - 提交后通过 _trim_audio_callback 通知 ASR 引擎裁剪音频
        - preedit 不再包含已提交内容（即提交即清空）
        """
        if not text or not text.strip():
            return

        commit_text = text.strip()
        commit_len = len(commit_text)  # ★ 用 strip 后长度，保证弹出数一致

        for _ in range(commit_len):
            if self._chars:
                self._chars.popleft()

        self._green_end = max(0, self._green_end - commit_len)
        self._yellow_end = max(self._green_end, self._yellow_end - commit_len)
        self._frozen_green_len = max(0, self._frozen_green_len - commit_len)

        self._committed_chars += commit_len
        self._last_commit_text = commit_text
        self._last_commit_raw = commit_text

        self._last_commit_time = time.time()
        self._notify_commit(commit_text)

        # ★ 通知 ASR 引擎裁剪已提交部分对应的音频
        # v3.2: 同时传入 buffer 剩余文本（离线纠正后的权威文本），
        # 用于精确计算裁剪比例，替代易膨胀失真的 _accumulated_raw
        if self._trim_audio_callback:
            try:
                self._trim_audio_callback(commit_len, commit_text, self.full_text)
            except Exception:
                logger.warning("trim_audio_callback failed", exc_info=True)

        # 提交后重新划分三区（可能还有安全文本）
        self._recalc_zones()
        self._try_commit()

    def commit_refined(self, raw_text: str, refined_text: str) -> bool:
        """提交 LLM 润色后的文本：按原文长度弹出字符，上屏润色文本。

        ★ 音频裁剪权重用原文（raw_text 与音频一一对应），
        避免 LLM 增删字导致裁剪比例失真（欠裁剪→重复上屏）。

        返回是否提交成功（等待 LLM 期间绿区头部被离线纠正改写时返回 False）。
        """
        raw_len = len(raw_text)
        if raw_len == 0:
            return False
        # 校验：buffer 头部必须仍与送润时的原文一致
        if self.full_text[:raw_len] != raw_text:
            logger.warning(
                f"commit_refined dropped: buffer head changed during LLM wait "
                f"(expect '{raw_text[:15]}...', now '{self.full_text[:15]}...')")
            return False

        refined = refined_text.strip() or raw_text.strip()
        if not refined:
            return False

        for _ in range(raw_len):
            if self._chars:
                self._chars.popleft()

        self._green_end = max(0, self._green_end - raw_len)
        self._yellow_end = max(self._green_end, self._yellow_end - raw_len)
        self._frozen_green_len = max(0, self._frozen_green_len - raw_len)

        self._committed_chars += len(refined)
        self._last_commit_text = refined
        self._last_commit_raw = raw_text.strip()

        self._last_commit_time = time.time()
        self._notify_commit(refined)

        # 音频裁剪：commit 权重按原文计算（与音频对应）
        if self._trim_audio_callback:
            try:
                self._trim_audio_callback(raw_len, raw_text, self.full_text)
            except Exception:
                logger.warning("trim_audio_callback failed", exc_info=True)

        self._recalc_zones()
        self._try_commit()
        return True

    # ============ 兜底 ============

    def emergency_check(self):
        """每秒执行：处理长停顿、黄区超时、绿区超限等边界情况。"""
        now = time.time()

        # ★ 无 LLM：黄区稳定超时 → 直接尝试提交（黄区头部即为稳定文本）
        if not self._llm_enabled:
            yellow_len = self._yellow_end
            if yellow_len >= self.MIN_COMMIT_CHARS and self._last_yellow_modified > 0:
                if now - self._last_yellow_modified > self.YELLOW_STABLE_TIMEOUT:
                    self._try_commit()
            return

        # 有 LLM：绿区稳定超时 → 重试提交（★ 长停顿：放宽到逗号级兜底）
        if self._green_end > 0 and self._last_green_modified > 0:
            if now - self._last_green_modified > self.STABLE_TIME_THRESHOLD:
                self._try_commit(relaxed=True)

        # 有 LLM：黄区稳定超时 → 重新划分（将稳定黄区推绿）
        yellow_len = self._yellow_end - self._green_end
        if yellow_len > 0 and self._last_yellow_modified > 0:
            if now - self._last_yellow_modified > self.YELLOW_STABLE_TIMEOUT:
                self._recalc_zones()
                self._try_commit(relaxed=True)

        # 有 LLM：绿区超限 → 走 _try_commit 强制提交
        # ★ v3.4：旧版直接 _do_commit 原文绕过了 LLM 润色通道，
        # 导致 "vebco与火爆" 这类残次词原文上屏（正确润色结果晚到被丢弃）。
        # 现统一走 _try_commit → _request_refine；润色在途时静默等待，
        # 完成后 commit_refined 会连锁触发下一轮提交，绿区不会无限膨胀。
        if self._green_end > self.FORCE_COMMIT_SIZE:
            self._try_commit(relaxed=True)

    # ============ 语义边界 ============

    @staticmethod
    def _snap_out_of_ascii_run(chars, pos: int) -> int:
        """若边界落在 ASCII 字母/数字连续序列中间，往回退到词首。"""
        while (0 < pos < len(chars)
               and chars[pos - 1].isascii() and chars[pos - 1].isalnum()
               and chars[pos].isascii() and chars[pos].isalnum()):
            pos -= 1
        return pos

    def _find_semantic_boundary(self, text: str, min_chars: int = 5) -> int:
        """在文本中从尾部向前找语义边界，返回边界后位置。

        ★ 修复：
        - WEAK_BOUNDARIES 与 MEDIUM 一致使用 i > min_chars + 5 约束
        - 新增 ASCII 序列保护：不在 a-zA-Z0-9 连续序列中间截断
        """
        for i in range(len(text) - 1, min_chars - 1, -1):
            # ★ ASCII 序列保护：不切分连续 ASCII 字符
            if (i < len(text) - 1
                    and text[i].isascii()
                    and text[i + 1].isascii()
                    and text[i].isalnum()
                    and text[i + 1].isalnum()):
                continue  # 跳过：切点在 ASCII 序列中间（如 "LL/M"、"lin/ux"）

            if text[i] in self.STRONG_BOUNDARIES:
                return i + 1
            if text[i] in self.MEDIUM_BOUNDARIES and i > min_chars + 5:
                return i + 1
            if text[i] in self.WEAK_BOUNDARIES and i > min_chars + 5:
                return i + 1

        # 无标点回退：长文本硬切分（防止连续语音永不提交）
        if len(text) >= self.FORCE_COMMIT_SIZE:
            return self.FORCE_COMMIT_SIZE
        return 0

    def _find_commit_point(self, green_text: str, relaxed: bool = False) -> int:
        """在绿区文本中找提交点。

        ★ v3.6 句子优先策略：
        1. 从尾部回溯找最后一个强边界（。！？；）→ 整句上屏，
           边界后残句留在绿区与后续新文字合并再润（周而复始）
        2. 无强边界且未放宽 → 继续等待（不用逗号碎片强切）
        3. relaxed（停顿超时/超限）或绿区已超限 → 逗号/语气词逐级兜底，
           再不行硬切（防无标点长语音永不提交）
        """
        # 1) 句子优先：最后一个强边界（至少成句 5 字）
        for i in range(len(green_text) - 1, 4, -1):
            if green_text[i] in self.STRONG_BOUNDARIES:
                return i + 1

        # 2) 无强边界：未放宽且未超限 → 等更多文本
        if not relaxed and len(green_text) < self.FORCE_COMMIT_SIZE:
            return 0

        # 3) 兜底：逗号/语气词等次级边界
        boundary = self._find_semantic_boundary(green_text, min_chars=5)
        if boundary >= self.MIN_COMMIT_CHARS:
            return boundary

        # 4) 无任何边界但已超限 → 硬切（防连续无标点语音永不提交）
        if len(green_text) >= self.FORCE_COMMIT_SIZE:
            return self.FORCE_COMMIT_SIZE

        return 0

    # ============ 渲染 ============

    def render_segments(self) -> List[Tuple[str, str]]:
        if self._showing_placeholder:
            return [("…", "gray")]
        result: List[Tuple[str, str]] = []
        if self._green_end > 0:
            result.append((self.green_text, "green"))
        if self._yellow_end > self._green_end:
            result.append((self.yellow_text, "yellow"))
        if len(self._chars) > self._yellow_end:
            result.append((self.red_text, "red"))
        return result

    def show_placeholder(self):
        self._showing_placeholder = True


class PTTPipelineV3:
    """PTT 语音识别流水线 v3.6"""

    LLM_REFINE_TIMEOUT = 8.0   # 绿区润色超时（秒），超时回退提交原文
    LLM_FINAL_TIMEOUT = 6.0    # 松手终审超时（秒）——真实 LLM 单次 5~7s，3s 永远吃不到结果
    FINAL_SPLIT_THRESHOLD = 40  # 终审文本超此长度则切段并行润色
    FINAL_CHUNK_SIZE = 35       # 终审切段目标长度——关思考后单段 ~1s 吐完，
                                # 切段主要为并行降尾延，35 字兼顾跨段衔接质量
    PREV_CONTEXT_CHARS = 40    # 送润时携带的已定稿上文尾部长度
    NEXT_CONTEXT_CHARS = 30    # 送润时携带的后续粗识别下文长度

    def __init__(self, server, llm_optimizer=None):
        self.server = server
        self.llm_optimizer = llm_optimizer
        self.buffer = CandidateBuffer()
        self.buffer._notify_commit = self._on_commit
        # ★ 根据 LLM 是否可用设置绿区开关 + 润色回调
        self.buffer.set_llm_enabled(llm_optimizer is not None)
        self.buffer._request_refine = self._schedule_refine
        self._emergency_timer: Optional[asyncio.Task] = None
        self._prev_offline_text = ""
        # ★ 已上屏定稿文本尾部（跨段衔接的上文，_on_commit 累积）
        self._committed_tail: str = ""
        # ★ 润色任务与 finalize 锁
        self._refine_task: Optional[asyncio.Task] = None
        self._finalizing: bool = False

    def reset(self):
        # 保留 trim 回调和 LLM 开关（跨 reset 周期复用）
        saved_trim_cb = self.buffer._trim_audio_callback
        saved_llm = self.buffer._llm_enabled
        self.buffer = CandidateBuffer()
        self.buffer._notify_commit = self._on_commit
        self.buffer._trim_audio_callback = saved_trim_cb
        self.buffer.set_llm_enabled(saved_llm)
        self.buffer._request_refine = self._schedule_refine
        self._prev_offline_text = ""
        self._committed_tail = ""
        # 取消在途润色（旧 buffer 已废弃）
        if self._refine_task and not self._refine_task.done():
            self._refine_task.cancel()
        self._refine_task = None

    async def commit_now(self):
        """回车键强制提交：立即提交 buffer 全部内容（不等语义边界）。

        与 finalize 的区别：commit_now 不做 LLM 润色，不重置 buffer 状态，
        只是把当前 buffer 内容推上去并清空。适用于用户主动按回车确认的场景。
        """
        full = self.buffer.full_text
        if not full or not full.strip():
            # 空 buffer：发 reset 清掉候选框
            if self.server:
                await self.server.broadcast({"type": "reset"})
            return

        stripped = full.strip()
        # 直接提交，不走 _do_commit 的语义边界检查
        self.buffer._do_commit(stripped)
        await self._update_display()

    async def on_intermediate(self, text: str):
        if not text:
            return
        self.buffer.update_streaming(text)
        await self._update_display()

    async def on_offline_correction(self, text: str, generation: int):
        if not text or generation <= 0:
            return
        # ★ finalize 正在收尾：不再接受离线纠正，避免与终审快照竞态重复提交
        if self._finalizing:
            return
        self.buffer.apply_offline_correction(text)
        self._prev_offline_text = text
        await self._update_display()

    def _schedule_refine(self, raw_text: str, relaxed: bool = False):
        """绿区就绪时由 buffer._try_commit 同步调用：调度异步润色任务。

        v3.7: raw_text 是整个绿区原文，提交切点由润色结果决定。
        """
        if self._finalizing:
            return
        if not self.llm_optimizer:
            # LLM 不可用（例如运行中被关闭）：原文切分直接提交
            cut = self.buffer._find_commit_point(raw_text, relaxed=True)
            if cut > 0:
                self.buffer._do_commit(raw_text[:cut])
            return
        if self._refine_task and not self._refine_task.done():
            return  # 已有润色在途，完成后会重新触发 _try_commit
        # ★ 跨段衔接上下文：上文=已定稿尾部，下文=本段之后的粗识别文本
        prev_ctx = self._committed_tail[-self.PREV_CONTEXT_CHARS:]
        next_ctx = self.buffer.full_text[
            len(raw_text):len(raw_text) + self.NEXT_CONTEXT_CHARS]
        try:
            self._refine_task = asyncio.create_task(
                self._refine_and_commit(raw_text, prev_ctx, next_ctx, relaxed))
        except RuntimeError:
            # 无 event loop（同步测试环境）：回退原文切分提交
            cut = self.buffer._find_commit_point(raw_text, relaxed=True)
            if cut > 0:
                self.buffer._do_commit(raw_text[:cut])

    @staticmethod
    def _strip_context_echo(refined: str, prev_ctx: str,
                            next_ctx: str = "") -> str:
        """防回显剥离：LLM 偶尔会把上文结尾或标签拼进输出头部，
        或把下文开头续写进输出尾部。"""
        # 标签回显："【待校对段...】" 之类的头部标记
        if refined.startswith('【'):
            end = refined.find('】')
            if 0 < end < 20:
                refined = refined[end + 1:].lstrip('\n ')
        # 上文尾部回显：去掉输出头部与已定稿上文尾部的重叠
        base = prev_ctx.rstrip('。，！？；、：,.!?;: ')
        if base:
            max_check = min(20, len(base), len(refined) - 1)
            for n in range(max_check, 0, -1):
                if refined.startswith(base[-n:]):
                    refined = refined[n:].lstrip('。，！？；、：,.!?;: ')
                    break
        # ★ v3.8.4 下文头部回显：LLM 把 next_context 开头续写进了输出尾部
        #   （典型诱因：待校对段尾部是残词，模型顺着下文补全）
        tail = next_ctx.lstrip('。，！？；、：,.!?;: ')
        if tail:
            max_check = min(20, len(tail), len(refined) - 1)
            for n in range(max_check, 3, -1):
                if refined.rstrip('。，！？；、：,.!?;: ').endswith(tail[:n]):
                    refined = refined.rstrip('。，！？；、：,.!?;: ')[:-n]
                    break
        return refined

    @staticmethod
    def _refined_cut(refined: str, relaxed: bool, overflowed: bool) -> int:
        """在润色结果上找提交切点（标点由 LLM 规范过，可靠）。

        句子优先：最后一个强边界；放宽/超限时逗号/语气词兜底；
        超限且无任何边界时全量提交（绿区本身就是安全区）。
        """
        CB = CandidateBuffer
        for i in range(len(refined) - 1, 4, -1):
            if refined[i] in CB.STRONG_BOUNDARIES:
                return i + 1
        if not (relaxed or overflowed):
            return 0
        for i in range(len(refined) - 1, 4, -1):
            if (refined[i] in CB.MEDIUM_BOUNDARIES
                    or refined[i] in CB.WEAK_BOUNDARIES):
                return i + 1
        return len(refined) if overflowed else 0

    @staticmethod
    def _map_refined_to_raw(raw: str, refined: str, cut: int) -> int:
        """把润色文本上的切点映射回原文位置（序列对齐）。

        LLM 会增删改字，不能直接用下标；用 difflib 匹配块定位，
        切点落在非匹配区时取下一个匹配块起点，兜底按长度比例估算。
        音频裁剪/buffer 弹出都以返回的原文前缀为准。
        """
        sm = difflib.SequenceMatcher(None, refined, raw, autojunk=False)
        for a, b, size in sm.get_matching_blocks():
            if a <= cut <= a + size:
                return b + (cut - a)
            if a > cut:
                return b
        return min(len(raw), round(cut * len(raw) / max(1, len(refined))))

    async def _refine_and_commit(self, raw_text: str, prev_ctx: str = "",
                                 next_ctx: str = "", relaxed: bool = False):
        """LLM 润色整个绿区，在润色结果上切句提交；超时/失败回退原文切分。

        v3.7 流程：
        1. 整个绿区原文 + 上下文 → LLM 润色
        2. 在润色结果（标点可靠）上找最后一个强边界作切点
        3. difflib 将切点映射回原文位置 r：提交润色前段、弹出原文前 r 字
        4. 原文残句留在绿区，与后续新文字合并进下一轮润色（周而复始）
        """
        refined: Optional[str] = None
        try:
            refined = await asyncio.wait_for(
                self.llm_optimizer.optimize(
                    raw_text, prev_context=prev_ctx, next_context=next_ctx),
                timeout=self.LLM_REFINE_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("LLM refine timed out, falling back to raw split")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"LLM refine failed: {e}")

        if self._finalizing:
            return  # finalize 会统一处理剩余文本

        overflowed = len(raw_text) >= CandidateBuffer.FORCE_COMMIT_SIZE
        text = (refined or "").strip()
        if text:
            text = self._strip_context_echo(text, prev_ctx, next_ctx)

        if not text:
            # LLM 超时/失败：回退在原文上切分提交原文（不卡管线）
            cut = self.buffer._find_commit_point(
                raw_text, relaxed=relaxed or overflowed)
            if cut > 0 and self.buffer.commit_refined(
                    raw_text[:cut], raw_text[:cut]):
                await self._update_display()
            return

        b = self._refined_cut(text, relaxed, overflowed)
        if b <= 0:
            return  # 润色结果里还没有完整句子 → 留着下轮合并再润
        r = self._map_refined_to_raw(raw_text, text, b)
        if r <= 0:
            return

        # ★ v3.8.4 越界提交守卫：用 r 字原文只能兑换相近长度的润色文本。
        #   若 b 远超 r，说明模型把 next_context 抄进了输出（漏网之鱼），
        #   直接提交会把还在黄区的内容提前上屏 → 黄区成熟后重复提交
        #   （实录：raw 21 字兑换 38 字，"叫做雨。/叫做宇皇"双重上屏）
        if b > r + max(8, r // 2):
            logger.warning(
                f"LLM refine echo guard: raw {r} chars → refined {b} chars, "
                f"suspect next-context echo, falling back to raw split")
            cut = self.buffer._find_commit_point(
                raw_text, relaxed=relaxed or overflowed)
            if cut > 0 and self.buffer.commit_refined(
                    raw_text[:cut], raw_text[:cut]):
                await self._update_display()
            return

        if self.buffer.commit_refined(raw_text[:r], text[:b]):
            logger.info(
                f"PTT pipeline: LLM refined commit "
                f"(raw {r}/{len(raw_text)} → refined {b}/{len(text)} chars)")
            await self._update_display()
        # 头部已变化 → 丢弃本次结果，下一轮离线纠正会重新触发

    async def finalize(self):
        """PTT 松键：所有剩余文本推绿，尝试 LLM 润色，提交。

        ★ v3.3：全程持有 _finalizing 锁 —— LLM 等待期间离线纠正/应急定时器
        不得再改写或提交 buffer，否则终审快照会把已提交内容重复上屏。
        """
        self._finalizing = True
        try:
            # 取消在途的绿区润色（finalize 统一处理全部剩余文本）
            if self._refine_task and not self._refine_task.done():
                self._refine_task.cancel()
            self._refine_task = None

            # 全部推绿
            self.buffer._yellow_end = len(self.buffer._chars)
            self.buffer._green_end = self.buffer._yellow_end
            self.buffer._last_green_modified = time.time()

            # ★ 立即渲染全绿 preedit：终审等待期间给用户"文本已定稿"反馈
            await self._update_display()

            # 获取待提交文本
            text_to_commit = self.buffer.full_text

            if text_to_commit and self.llm_optimizer:
                text_to_commit = await self._refine_final(text_to_commit)

            if text_to_commit:
                stripped = text_to_commit.strip()
                if stripped:
                    if self.server:
                        await self.server.broadcast(
                            {"type": "final", "text": stripped}
                        )
                # ★ 用 stripped 文本替换 buffer 并提交，避免 strip 前后长度不一致导致丢字
                self.buffer._chars.clear()
                for ch in stripped:
                    self.buffer._chars.append(ch)
                self.buffer._green_end = len(stripped)
                self.buffer._yellow_end = self.buffer._green_end
                self.buffer._do_commit(stripped)
            else:
                if self.server:
                    await self.server.broadcast({"type": "reset"})

            self.reset()
        finally:
            self._finalizing = False

    async def _refine_final(self, raw: str) -> str:
        """松手终审：长文本按语义边界切成 ≤35 字的段并行润色。
    
        单请求耗时 ≈ 网络往返 + 输出 token 流式解码（实测约 10 字/s），
        短段并行才能在超时预算内吐完。
    
        ★ v3.8.1 部分抢救：不再全有全无——按段收割结果，超时未完成的段
        才回退原文（旧版 gather 整体超时，98 字全部原文上屏，
        "物邦图/LLOM/泛ASR" 就是这么漏过去的）。
        """
        chunks = self._split_final_chunks(raw)
        # ★ 跨段衔接：每段都带上文（已定稿尾部 / 前一 chunk 原文尾部）
        # 和下文（后一 chunk 头部），并行请求互不等待
        prev_ctxs, next_ctxs = [], []
        for i, c in enumerate(chunks):
            if i == 0:
                prev_ctxs.append(self._committed_tail[-self.PREV_CONTEXT_CHARS:])
            else:
                prev_ctxs.append(chunks[i - 1][-self.PREV_CONTEXT_CHARS:])
            if i + 1 < len(chunks):
                next_ctxs.append(chunks[i + 1][:self.NEXT_CONTEXT_CHARS])
            else:
                next_ctxs.append("")
        tasks = [
            asyncio.ensure_future(self.llm_optimizer.optimize(
                c, prev_context=p, next_context=nx, urgent=True))
            for c, p, nx in zip(chunks, prev_ctxs, next_ctxs)]
        try:
            await asyncio.wait(tasks, timeout=self.LLM_FINAL_TIMEOUT)
        except Exception as e:
            logger.warning(f"LLM finalize wait failed: {e}")
        parts, salvaged = [], 0
        for i, t in enumerate(tasks):
            res = None
            if t.done() and not t.cancelled() and t.exception() is None:
                res = t.result()
            else:
                t.cancel()
            part = (res or "").strip()
            if part:
                part = self._strip_context_echo(
                    part, prev_ctxs[i], next_ctxs[i])
            if part:
                salvaged += 1
            part = part or chunks[i]
            if i > 0:
                # 后段头部残留标点清理（拼接处防双标点）
                part = part.lstrip('。，！？；、：,.!?;: ') or part
            parts.append(part)
        refined = "".join(parts).strip()
        if salvaged < len(chunks):
            logger.warning(
                f"LLM finalize partial: {salvaged}/{len(chunks)} chunk(s) "
                f"refined, rest committed as raw")
        elif refined and refined != raw:
            logger.info(
                f"PTT pipeline: LLM final optimized "
                f"({len(raw)}→{len(refined)} chars, {len(chunks)} chunk(s))")
        return refined or raw
    
    def _split_final_chunks(self, raw: str) -> List[str]:
        """终审切段：每段 ≤FINAL_CHUNK_SIZE+10 字，优先在语义边界处切。"""
        if len(raw) <= self.FINAL_SPLIT_THRESHOLD:
            return [raw]
        chunks = []
        rest = raw
        while len(rest) > self.FINAL_CHUNK_SIZE + 10:
            head = rest[:self.FINAL_CHUNK_SIZE + 10]
            b = self.buffer._find_semantic_boundary(head, min_chars=10)
            if not (10 <= b <= len(head)):
                b = self.FINAL_CHUNK_SIZE  # 无边界：硬切
            chunks.append(rest[:b])
            rest = rest[b:]
        if rest:
            chunks.append(rest)
        return chunks

    async def _update_display(self):
        """推送 preedit 到 fcitx5 端。"""
        segments = self.buffer.render_segments()
        if self.server:
            msg: dict = {"type": "preedit"}
            for text, style in segments:
                msg[style] = text
            await self.server.broadcast(msg)

    def _on_commit(self, text: str):
        """同步回调（由 CandidateBuffer._do_commit 同步调用）。

        _do_commit 是同步方法，若 _on_commit 为 async 则协程会被创建但永不执行。
        这里通过 create_task 将异步 broadcast 调度到 event loop。
        """
        if text:
            # ★ 累积已定稿尾部（跨段衔接的上文，只保留必要长度）
            self._committed_tail = (
                self._committed_tail + text)[-self.PREV_CONTEXT_CHARS * 2:]
        if self.server and text:
            try:
                async def _push():
                    await self.server.broadcast({
                        "type": "commit",
                        "text": text,
                    })
                    # ★ commit 后立即补发当前 preedit：C++ 端 commitText 会
                    #   清空整个 preedit（防残留 flush），若等下一条 ASR
                    #   intermediate（~60ms）才重绘，候选区会闪空一下
                    await self._update_display()
                asyncio.create_task(_push())
            except RuntimeError:
                pass  # 无 event loop（关机时可能发生）

    def start_emergency_timer(self):
        async def tick():
            while True:
                try:
                    # ★ finalize 期间静默，避免与终审快照竞态提交
                    if not self._finalizing:
                        self.buffer.emergency_check()
                except Exception:
                    pass
                await asyncio.sleep(1.0)
        self._emergency_timer = asyncio.create_task(tick())

    def stop_emergency_timer(self):
        if self._emergency_timer:
            self._emergency_timer.cancel()
            self._emergency_timer = None
