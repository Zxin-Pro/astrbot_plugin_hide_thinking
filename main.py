from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

# 模型常见思维链标签（英文 + 中文）
_THINK_TAG_NAMES = frozenset(
    {
        "think",
        "thinking",
        "reasoning",
        "thought",
        "redacted_reasoning",
        "analysis",
        "思考",
        "思维",
        "推理",
    }
)

_ANY_TAG = re.compile(
    r"<\s*(/?)\s*([a-zA-Z_\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff-]*)\b[^>]*>",
    re.IGNORECASE,
)

_MD_THINK_BLOCK = re.compile(
    r"```(?:thinking|think|reasoning|thought|analysis|思考)[^\n]*\n[\s\S]*?```",
    re.IGNORECASE,
)

# AstrBot 本体: "🤔 思考: ...\n\n────\n"
# 官方 r1-filter: "🤔思考：...\n\n"
_INJECTED_HEAD = re.compile(
    r"^\s*🤔\s*思考[:：][\s\S]*?(?:\n\s*────\s*\n|\n{2,})",
)

_EVENT_REASONING_KEY = "_llm_reasoning_content"

# 模型结束符 / 控制符漏进正文：<|eos|> <|im_end|> </s> 等
_SPECIAL_TOKEN = re.compile(
    r"<\|[^|>]{1,64}\|>"
    r"|</s>"
    r"|<s>"
    r"|</?eos>"
    r"|\[EOS\]",
    re.IGNORECASE,
)

# 整段复读折叠：半段太短不处理，避免误伤「哈哈哈哈」
_MIN_DUP_UNIT = 8


def _is_think_tag(name: str) -> bool:
    return name.lower() in _THINK_TAG_NAMES or name in _THINK_TAG_NAMES


def strip_think_tags(text: str) -> str:
    """删掉思维链标签及其内部内容，只留正文。"""
    if not text:
        return text

    text = _MD_THINK_BLOCK.sub("", text)

    out: list[str] = []
    depth = 0
    last = 0
    for match in _ANY_TAG.finditer(text):
        raw_name = match.group(2) or ""
        if not _is_think_tag(raw_name):
            continue
        is_close = match.group(1) == "/"
        start, end = match.span()
        if depth == 0:
            out.append(text[last:start])
            if is_close:
                last = end
            else:
                depth = 1
                last = end
        else:
            if is_close:
                depth = max(0, depth - 1)
            else:
                depth += 1
            last = end
    if depth == 0:
        out.append(text[last:])
    return "".join(out)


def strip_injected_reasoning(text: str) -> str:
    """去掉已经被拼进正文开头的「🤔 思考:」块。"""
    if not text:
        return text
    return _INJECTED_HEAD.sub("", text, count=1)


def strip_special_tokens(text: str) -> str:
    """删掉漏进正文的结束符 / 控制符，例如 <|eos|> <|im_end|> </s>。"""
    if not text:
        return text
    return _SPECIAL_TOKEN.sub("", text)


def tidy_text(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def collapse_duplicated_text(text: str) -> str:
    """整段是同一句话粘两遍时只留一句。

    覆盖：
    - 无分隔：早啊宝宝……早啊宝宝……
    - 中间空白 / 换行
    """
    if not text:
        return text
    s = text
    for _ in range(4):
        t = s.strip()
        n = len(t)
        if n < _MIN_DUP_UNIT * 2:
            return t
        folded = None
        for k in range(n // 2, _MIN_DUP_UNIT - 1, -1):
            if t[:k] == t[-k:] and t[k:-k].strip() == "":
                folded = t[:k]
                break
        if folded is None:
            return t
        s = folded
    return s.strip()


def clean_reply_text(
    text: str,
    *,
    strip_tags: bool = True,
    strip_injected: bool = True,
    strip_special: bool = True,
    collapse_duplicate: bool = True,
) -> str:
    if not text:
        return text
    if strip_injected:
        text = strip_injected_reasoning(text)
    if strip_tags:
        text = strip_think_tags(text)
    if strip_special:
        text = strip_special_tokens(text)
    if collapse_duplicate:
        text = collapse_duplicated_text(text)
    return tidy_text(text)


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "是"}


def _is_lark_reasoning_comp(comp: Any) -> bool:
    data = getattr(comp, "data", None)
    return isinstance(data, dict) and data.get("type") == "lark_collapsible_panel_reasoning"


@register(
    "astrbot_plugin_hide_thinking",
    "Zxin-Pro",
    "隐藏思考/结束符，并折叠整段复读",
    "1.2.0",
    "https://github.com/Zxin-Pro/astrbot_plugin_hide_thinking",
)
class HideThinking(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}

    def _enabled(self) -> bool:
        return _to_bool(self.config.get("enabled", True), True)

    def _strip_tags(self) -> bool:
        return _to_bool(self.config.get("strip_tags", True), True)

    def _strip_injected(self) -> bool:
        return _to_bool(self.config.get("strip_injected", True), True)

    def _strip_special(self) -> bool:
        return _to_bool(self.config.get("strip_special", True), True)

    def _collapse_duplicate(self) -> bool:
        return _to_bool(self.config.get("collapse_duplicate", True), True)

    def _clean(self, text: str) -> str:
        return clean_reply_text(
            text,
            strip_tags=self._strip_tags(),
            strip_injected=self._strip_injected(),
            strip_special=self._strip_special(),
            collapse_duplicate=self._collapse_duplicate(),
        )

    def _clear_reasoning(self, event: AstrMessageEvent, resp: LLMResponse | None) -> None:
        if resp is not None and hasattr(resp, "reasoning_content"):
            resp.reasoning_content = None
        extras = event.get_extra()
        if isinstance(extras, dict):
            extras.pop(_EVENT_REASONING_KEY, None)

    def _scrub_chain(self, chain: list[Any]) -> list[Any]:
        new_chain: list[Any] = []
        for comp in chain:
            if _is_lark_reasoning_comp(comp):
                continue
            if isinstance(comp, Plain) and comp.text is not None:
                cleaned = self._clean(comp.text)
                if not cleaned:
                    continue
                if cleaned != comp.text:
                    comp.text = cleaned
            new_chain.append(comp)
        return new_chain

    @filter.on_llm_response(priority=-100)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self._enabled() or resp is None:
            return
        try:
            text = getattr(resp, "completion_text", None)
            if text:
                cleaned = self._clean(str(text))
                if cleaned != str(text):
                    resp.completion_text = cleaned
            self._clear_reasoning(event, resp)
        except Exception:
            logger.error("[HideThinking] 处理 LLM 响应失败", exc_info=True)

    @filter.on_decorating_result(priority=-100)
    async def on_decorating_result(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        try:
            self._clear_reasoning(event, None)
            result = event.get_result()
            if not result or not getattr(result, "chain", None):
                return
            result.chain = self._scrub_chain(list(result.chain))
        except Exception:
            logger.error("[HideThinking] 处理发送链失败", exc_info=True)
