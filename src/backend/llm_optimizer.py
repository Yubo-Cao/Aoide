"""LLM text optimizer — streaming OpenAI-compatible API"""
import asyncio
import json
import logging
import time
from typing import Optional
import httpx

logger = logging.getLogger("yuhuang.llm")


class LLMOptimizer:
    """Call LLM to optimize speech recognition text"""

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "",
        model: str = "qwen2.5-7b-instruct",
        temperature: float = 0.3,
        max_tokens: int = 2000,
        system_prompt: str = "",
        optimize_delay: float = 0.5,
        auto_commit_delay: float = 0.2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.optimize_delay = optimize_delay
        self.auto_commit_delay = auto_commit_delay
        self.system_prompt = system_prompt or self._default_prompt()
        # ★ v3.8.2 连接复用：避免每次请求重新 TCP+TLS 握手（省 0.2~0.4s）
        self._client: Optional[httpx.AsyncClient] = None
        self._client_base_url: str = ""
        # ★ v3.8.3 关思考自适应梯子：当前尝试到第几档（收敛后记住）
        self._think_off_idx: int = 0
        self._think_warned: bool = False

    # 各家"关闭思考"开关不统一，按命中面逐档尝试：
    # 0: DeepSeek V3.2+/V4、智谱 GLM-4.5+ 等
    # 1: 通义 Qwen3(DashScope)、腾讯混元等
    # 2: vLLM 本地跑 Qwen3 系列（chat_template_kwargs 透传模板）
    # 3: 放弃参数（非思考模型本来就不思考；或确实关不掉，只能告警）
    # 升档时机：服务端 400 拒收 → 立即换下一档重试；
    #           200 但仍检测到 reasoning_content（宽松服务端静默忽略
    #           未知字段）→ 下次请求自动升档
    _THINK_OFF_LADDER = (
        {"thinking": {"type": "disabled"}},
        {"enable_thinking": False},
        {"chat_template_kwargs": {"enable_thinking": False}},
        {},
    )

    @staticmethod
    def _default_prompt() -> str:
        return (
            "你是中文语音识别（ASR）文本的实时校对助手。输入是 ASR 原始输出，可能存在：\n"
            "- 同音/近音字错误（人名、术语被写成同音别字）\n"
            "- 中英混说时英文术语被拆错或拼错（如 lininux 实为 Linux）\n"
            "- 英文短语被转写成发音相近的另一个英文词（如 web coding 实为"
            " vibe coding）\n"
            "- 音译成汉字的外来词（如 乌邦图 实为 Ubuntu）\n"
            "- 口头语、结巴重复\n"
            "- 标点缺失、错误或重复（如 \"，。\"）\n\n"
            "你的任务：\n"
            "1. 结合上下文语义推断专有名词、产品名、技术术语的正确写法：\n"
            "   谈技术时音似英文术语的词归一到通行英文写法（如 泛ASR→FunASR、"
            "LLOM→LLM），无法确定时保留原文\n"
            "2. 对英文词组和中文名称都保持发音怀疑：若某词不是该语境下的通行说法，"
            "而存在发音相近、更符合话题的常见术语或知名产品/品牌名，应替换为"
            "后者（如谈输入法时 多宝/豆宝 实为产品名 豆包）\n"
            "3. 根据上下文纠正明显的同音字错误\n"
            "4. 删除无意义的口头语，修复结巴重复\n"
            "5. 规范标点（不允许连续标点）\n"
            "6. 保持原意和口语风格，不增加原文没有的内容\n"
            "7. 除非确有依据，不要改写本就正确的内容\n\n"
            "只输出校对后的文本，不要任何解释、前缀或引号。"
        )

    def update_config(self, **kwargs):
        """Update runtime configuration (called when fcitx5 config changes)"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                if getattr(self, key) != value and key in ("model", "base_url"):
                    # 换模型/换端点：关思考梯子重新从第一档摸索
                    self._think_off_idx = 0
                    self._think_warned = False
                setattr(self, key, value)
                logger.info(f"LLM config updated: {key}={value}")

    async def optimize(self, text: str, prev_context: str = "",
                       next_context: str = "",
                       urgent: bool = False) -> Optional[str]:
        """一次性的文本优化（绿区润色 / 最终提交）

        ★ v3.5 跨段衔接：
        prev_context: 已上屏定稿文本的尾部 —— 让 LLM 知道本段开头如何衔接
        （标点、重字），但禁止输出上文。
        next_context: 后续未定稿的粗识别文本 —— 提供语义依据（如术语纠错
        需要后文佐证），但禁止输出下文。
        ★ v3.8.1 urgent：终审路径跳过 optimize_delay（松手后每 0.1s 都是
        用户在等，超时预算不容白睡）
        """
        if not text or not text.strip():
            return None
        if not prev_context and not next_context:
            return await self._call_llm(
                f"请校对这段语音识别文本：\n\n{text}", urgent=urgent
            )
        parts = []
        if prev_context:
            parts.append(
                f"【上文｜已上屏定稿，仅供衔接参考，禁止输出】\n{prev_context}")
        parts.append(f"【待校对段｜只输出这一段校对后的文本】\n{text}")
        if next_context:
            parts.append(
                f"【下文｜未定稿粗识别，仅供语义参考，禁止输出】\n{next_context}")
        user_msg = (
            "请校对语音识别文本中的【待校对段】，结合上下文纠错，"
            "并保证与上文衔接自然（开头不重复上文结尾的字词和标点）。"
            "输出必须在待校对段结束处停笔：即使待校对段结尾是半截的残词，"
            "也保持截断原样，绝不可用下文续写补全。"
            "只输出待校对段的校对结果：\n\n" + "\n\n".join(parts)
        )
        return await self._call_llm(user_msg, urgent=urgent)

    async def stream_optimize(self, new_raw: str, context: str = "") -> Optional[str]:
        """流式增量优化: 结合候选中已有的文本，优化新增的语音识别文本

        Args:
            new_raw: 新增的原始 ASR 文本
            context: 候选框中已有的未上屏文本
        Returns:
            优化后的完整候选文本（用于替换候选框）
        """
        combined = context + new_raw
        if not combined.strip():
            return None

        prompt = (
            "You are optimizing Chinese speech recognition text in real-time.\n"
            "Below is the complete text just transcribed.\n"
            "Optimize it: fix punctuation, remove fillers, correct stutters.\n"
            "Keep meaning unchanged. Output only the optimized text.\n\n"
            f"Text:\n{combined}"
        )
        return await self._call_llm(prompt)

    async def _call_llm(self, user_msg: str, urgent: bool = False) -> Optional[str]:
        """底层 LLM API 调用 (流式)

        urgent=True（终审）跳过 optimize_delay 防抖延迟。

        ★ v3.8.2/v3.8.3 提速三件套（根治"吐字慢"假象）：
        - 关闭思考模式：混合思考模型（DeepSeek V4 等）默认 thinking
          开启，思维链走 reasoning_content 被本解析器丢弃，看起来就是
          几秒没产出；各家开关写法不同，用 _THINK_OFF_LADDER 自适应
        - 连接复用：长驻 AsyncClient，免每次 TCP+TLS 握手
        - TTFT/思考流观测：首 token 耗时、思考字数进日志，慢在哪一眼可见
        """
        if not user_msg or not user_msg.strip():
            return None

        if self.optimize_delay > 0 and not urgent:
            await asyncio.sleep(self.optimize_delay)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        think_off = self._THINK_OFF_LADDER[self._think_off_idx]
        payload.update(think_off)

        t0 = time.monotonic()
        try:
            client = self._get_client()
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                if response.status_code == 400 and think_off:
                    # 服务端拒收当前档开关 → 换下一档重试并记住
                    self._think_off_idx += 1
                    logger.info(
                        f"LLM server rejected think-off param "
                        f"{list(think_off)}, trying level {self._think_off_idx}"
                    )
                    await response.aread()
                    return await self._call_llm(user_msg, urgent=True)
                response.raise_for_status()

                optimized_parts = []
                reasoning_chars = 0
                ttft = -1.0
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        # ★ 思考流观测：不进结果，但计入耗时归因
                        reasoning_chars += len(delta.get("reasoning_content") or "")
                        content = delta.get("content", "")
                        if content:
                            if ttft < 0:
                                ttft = time.monotonic() - t0
                            optimized_parts.append(content)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue

                result = "".join(optimized_parts).strip()
                if result:
                    if reasoning_chars:
                        # 200 但思考仍在：服务端静默忽略了当前档开关 → 升档
                        if self._think_off_idx < len(self._THINK_OFF_LADDER) - 1:
                            self._think_off_idx += 1
                            logger.info(
                                f"LLM still thinking ({reasoning_chars} chars), "
                                f"escalating think-off to level {self._think_off_idx}"
                            )
                        elif not self._think_warned:
                            self._think_warned = True
                            logger.warning(
                                f"Cannot disable thinking mode on model "
                                f"'{self.model}' — consider a non-thinking "
                                f"model for lower latency"
                            )
                    extra = (f", thinking {reasoning_chars} chars!"
                             if reasoning_chars else "")
                    logger.info(
                        f"LLM result ({time.monotonic() - t0:.1f}s, "
                        f"ttft {ttft:.1f}s, {len(result)} chars{extra}): "
                        f"{result[:40]}..."
                    )
                    return result

        except httpx.TimeoutException:
            logger.warning("LLM request timed out")
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM HTTP error: {e.response.status_code}")
        except Exception as e:
            logger.error(f"LLM optimization error: {e}")
            self._client = None  # 连接异常：下次重建

        return None

    def _get_client(self) -> httpx.AsyncClient:
        """长驻 HTTP 连接（base_url 变更时重建）。"""
        if self._client is None or self._client_base_url != self.base_url:
            self._client = httpx.AsyncClient(timeout=30.0, trust_env=False)
            self._client_base_url = self.base_url
        return self._client
