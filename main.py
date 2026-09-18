from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

try:  # 工具/插件主动发送走 Context.send_message，需要一并接管
    from astrbot.core.star.context import Context as _StarContext
except Exception:  # pragma: no cover - 老版本兜底
    _StarContext = None

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
# 跨条去重：分段发出后，后一条把前一条接在尾巴上
_RECENT_TTL = 60.0
_RECENT_MAX = 8

# 模型把工具调用当正文吐出来：<tool_call>…</tool_call>
_TOOL_BLOCK = re.compile(
    r"<\s*(tool_call|function_call|invoke)\s*>([\s\S]*?)</\s*\1\s*>",
    re.IGNORECASE,
)
_TOOL_UNCLOSED = re.compile(
    r"<\s*(?:tool_call|function_call|invoke)\s*>[\s\S]*$",
    re.IGNORECASE,
)
_TOOL_SCAFFOLD = re.compile(
    r"^\s*(?:"
    r"</?\s*(?:tool_call|function_call|invoke)\s*>"
    r"|</?\s*parameter(?:\s*>[^<\n]*)?>"
    r"|<\s*parameter\s*>[^<\n]*>"
    r"|send_message_to_user"
    r")\s*$",
    re.IGNORECASE,
)


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


def _extract_plain_from_obj(obj: Any) -> str:
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return _extract_plain_from_obj(json.loads(s))
            except Exception:
                return s
        return s
    if isinstance(obj, dict):
        if obj.get("type") == "plain" and obj.get("text"):
            return str(obj.get("text") or "").strip()
        for key in ("text", "content", "message", "messages"):
            if key in obj:
                got = _extract_plain_from_obj(obj[key])
                if got:
                    return got
        return ""
    if isinstance(obj, list):
        parts = [_extract_plain_from_obj(item) for item in obj]
        return "\n".join(p for p in parts if p)
    return ""


_JSON_BLOB = re.compile(r"(\[[\s\S]*\]|\{[\s\S]*\})")


def _plain_from_tool_inner(inner: str) -> str:
    inner = (inner or "").strip()
    if not inner:
        return ""
    blob = _JSON_BLOB.search(inner)
    if blob:
        got = _extract_plain_from_obj(blob.group(1))
        if got:
            return got
    got = _extract_plain_from_obj(inner)
    if got and got != inner:
        return got
    return ""


def extract_tool_call_text(text: str) -> str:
    """整段 <tool_call> 里抽出人话；分段发出的外壳碎片丢掉。"""
    t = (text or "").strip()
    if not t:
        return t
    if _TOOL_SCAFFOLD.match(t):
        return ""

    def _replace_block(match: re.Match) -> str:
        return _plain_from_tool_inner(match.group(2))

    t = _TOOL_BLOCK.sub(_replace_block, t)
    t = _TOOL_UNCLOSED.sub("", t)
    t = t.strip()
    if _TOOL_SCAFFOLD.match(t):
        return ""
    if t.startswith("[") or t.startswith("{"):
        extracted = _extract_plain_from_obj(t)
        if extracted:
            return extracted
    lower = t.lower()
    if "<parameter" in lower or "send_message_to_user" in lower or "<tool_call" in lower:
        got = _plain_from_tool_inner(t)
        if got:
            return got
        return ""
    return t


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


def _compact(text: str) -> str:
    """去掉空白后再比，避免「有空格 / 没空格」被当成两句。"""
    return re.sub(r"\s+", "", text or "")


def _ws_count(text: str) -> int:
    return sum(1 for ch in text or "" if ch.isspace())


def dedupe_lines_compact(text: str) -> str:
    """同一段文本里出现只差空格的重复行时，留空格更自然的那条。"""
    if not text or "\n" not in text:
        return text
    out: list[str] = []
    index: dict[str, int] = {}
    for line in text.splitlines():
        c = _compact(line)
        if not c:
            out.append(line)
            continue
        idx = index.get(c)
        if idx is None:
            index[c] = len(out)
            out.append(line)
        elif _ws_count(line) > _ws_count(out[idx]):
            out[idx] = line
    return "\n".join(out)


def _prefix_until_compact(text: str, keep_compact: str) -> str:
    """截到 compact 前缀刚好等于 keep_compact 的最短原文前缀。"""
    if not keep_compact:
        return ""
    acc: list[str] = []
    seen = 0
    for ch in text:
        acc.append(ch)
        if not ch.isspace():
            seen += 1
            if seen == len(keep_compact):
                return "".join(acc).rstrip()
    return "".join(acc).rstrip()


def strip_recent_overlap(text: str, recents: list[str], min_len: int = _MIN_DUP_UNIT) -> str:
    """后一条是刚发过的句子（可差空格），或把前一句接在尾巴上时，剪掉。"""
    t = (text or "").strip()
    if not t:
        return t
    t_c = _compact(t)
    for prev in recents:
        p = (prev or "").strip()
        p_c = _compact(p)
        if len(p_c) < min_len:
            continue
        if t_c == p_c:
            return ""
        if t.endswith(p):
            t = t[: -len(p)].rstrip()
            t_c = _compact(t)
            if not t:
                return ""
            continue
        if t_c.endswith(p_c):
            keep_c = t_c[: -len(p_c)]
            if not keep_c:
                return ""
            t = _prefix_until_compact(t, keep_c)
            t_c = _compact(t)
            if not t:
                return ""
    return t


def clean_reply_text(
    text: str,
    *,
    strip_tags: bool = True,
    strip_injected: bool = True,
    strip_special: bool = True,
    collapse_duplicate: bool = True,
    strip_tool_call: bool = True,
) -> str:
    if not text:
        return text
    if strip_injected:
        text = strip_injected_reasoning(text)
    if strip_tool_call:
        text = extract_tool_call_text(text)
    if strip_tags:
        text = strip_think_tags(text)
    if strip_special:
        text = strip_special_tokens(text)
    if collapse_duplicate:
        text = dedupe_lines_compact(text)
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
    "隐藏思考/结束符/工具调用泄漏，并折叠整段复读",
    "1.8.0",
    "https://github.com/Zxin-Pro/astrbot_plugin_hide_thinking",
)
class HideThinking(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self._patched: list[tuple[Any, str, Any]] = []
        self._patching = False
        self._recent: dict[str, list[tuple[float, str]]] = {}
        self._pending: dict[str, tuple[Any, Any, Any, Any]] = {}
        try:
            self._dedup_window = max(0.0, float(self.config.get("dedup_window", 2)))
        except Exception:
            self._dedup_window = 2.0

    async def initialize(self):
        try:
            self._install_send_patch()
        except Exception:
            logger.error("[HideThinking] 安装 send 补丁失败", exc_info=True)

    async def terminate(self):
        try:
            for sid in list(self._pending.keys()):
                await self._flush_sid(sid)
        except Exception:
            logger.error("[HideThinking] 终止冲刷缓冲失败", exc_info=True)
        try:
            self._remove_send_patch()
        except Exception:
            logger.error("[HideThinking] 卸载 send 补丁失败", exc_info=True)

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

    def _strip_tool_call(self) -> bool:
        return _to_bool(self.config.get("strip_tool_call", True), True)

    def _session_key(self, event: Any) -> str:
        getter = getattr(event, "unified_msg_origin", None)
        if callable(getter):
            try:
                val = getter()
                if val:
                    return str(val)
            except Exception:
                pass
        elif getter:
            return str(getter)
        return "default"

    def _purge_recent(self, sid: str, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        items = [
            (ts, txt)
            for ts, txt in self._recent.get(sid, [])
            if now - ts <= _RECENT_TTL and txt
        ]
        items = items[-_RECENT_MAX:]
        if items:
            self._recent[sid] = items
        else:
            self._recent.pop(sid, None)
        return [txt for _, txt in reversed(items)]

    def _remember(self, sid: str, text: str) -> None:
        text = (text or "").strip()
        if len(text) < _MIN_DUP_UNIT:
            return
        now = time.monotonic()
        items = [
            (ts, txt)
            for ts, txt in self._recent.get(sid, [])
            if now - ts <= _RECENT_TTL
        ]
        items.append((now, text))
        self._recent[sid] = items[-_RECENT_MAX:]

    def _apply_recent_sid(self, sid: str, chain: list[Any]) -> list[Any]:
        if not self._collapse_duplicate() or not chain:
            return chain
        recents = self._purge_recent(sid)
        new_chain: list[Any] = []
        outgoing: list[str] = []
        for comp in chain:
            if isinstance(comp, Plain) and comp.text is not None:
                cleaned = strip_recent_overlap(comp.text, recents + outgoing)
                if not cleaned:
                    continue
                if cleaned != comp.text:
                    comp.text = cleaned
                outgoing.append(cleaned)
            new_chain.append(comp)
        for text in outgoing:
            self._remember(sid, text)
        return new_chain

    def _clean(self, text: str) -> str:
        return clean_reply_text(
            text,
            strip_tags=self._strip_tags(),
            strip_injected=self._strip_injected(),
            strip_special=self._strip_special(),
            collapse_duplicate=self._collapse_duplicate(),
            strip_tool_call=self._strip_tool_call(),
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

    def _scrub_message(self, message: Any, sid: str | None = None) -> Any:
        if not self._enabled() or message is None:
            return message
        chain = getattr(message, "chain", None)
        if chain is None:
            return message
        new_chain = self._scrub_chain(list(chain))
        if sid is not None:
            new_chain = self._apply_recent_sid(sid, new_chain)
        try:
            message.chain = new_chain
        except Exception:
            return message
        return message

    @staticmethod
    def _is_pure_plain(message: Any) -> bool:
        chain = getattr(message, "chain", None)
        if not chain:
            return False
        return all(isinstance(c, Plain) for c in chain)

    @staticmethod
    def _plain_text_of(message: Any) -> str:
        chain = getattr(message, "chain", None) or []
        return "".join(
            c.text for c in chain if isinstance(c, Plain) and c.text is not None
        )

    async def _flush_pending_message(
        self, sid: str, flush: Any, message: Any
    ) -> None:
        chain = getattr(message, "chain", None)
        if chain is not None:
            chain = self._apply_recent_sid(sid, list(chain))
            if not chain:
                return
            try:
                message.chain = chain
            except Exception:
                pass
        await flush(message)

    async def _flush_sid(self, sid: str) -> None:
        item = self._pending.pop(sid, None)
        if not item:
            return
        flush, message, task = item
        if task is not None:
            task.cancel()
        await self._flush_pending_message(sid, flush, message)

    async def _buffer_message(self, sid: str, flush: Any, message: Any) -> bool:
        """纯文本消息进缓冲；同句不同空格只发更自然的那条。返回 True 表示已接管。"""
        item = self._pending.pop(sid, None)
        if item is not None:
            p_flush, p_msg, p_task = item
            if p_task is not None:
                p_task.cancel()
            p_text = self._plain_text_of(p_msg)
            i_text = self._plain_text_of(message)
            if p_text and _compact(p_text) == _compact(i_text):
                if _ws_count(i_text) > _ws_count(p_text):
                    await self._flush_pending_message(sid, flush, message)
                else:
                    await self._flush_pending_message(sid, p_flush, p_msg)
                return True
            await self._flush_pending_message(sid, p_flush, p_msg)

        async def _later() -> None:
            await asyncio.sleep(self._dedup_window)
            await self._flush_sid(sid)

        try:
            task = asyncio.create_task(_later())
        except Exception:
            task = None
            await self._flush_pending_message(sid, flush, message)
            return True
        self._pending[sid] = (flush, message, task)
        return True

    def _iter_event_classes(self):
        seen: set[int] = set()
        stack = [AstrMessageEvent]
        while stack:
            cls = stack.pop()
            cid = id(cls)
            if cid in seen:
                continue
            seen.add(cid)
            yield cls
            try:
                stack.extend(cls.__subclasses__())
            except Exception:
                continue

    def _patch_method(self, cls: Any, name: str, factory) -> None:
        orig = cls.__dict__.get(name)
        if orig is None or getattr(orig, "_hide_thinking_patched", False):
            return
        wrapped = factory(orig)
        wrapped._hide_thinking_patched = True
        setattr(cls, name, wrapped)
        self._patched.append((cls, name, orig))

    def _install_send_patch(self) -> None:
        if self._patched:
            return
        plugin = self

        def send_factory(orig):
            async def patched_send(event, message, *args, **kwargs):
                if plugin._enabled() and not plugin._patching:
                    plugin._patching = True
                    try:
                        message = plugin._scrub_message(message)
                        chain = getattr(message, "chain", None)
                        if chain is not None and len(chain) == 0:
                            return None
                    except Exception:
                        logger.error("[HideThinking] send 清洗失败", exc_info=True)
                    finally:
                        plugin._patching = False
                    sid = plugin._session_key(event)
                    if (
                        plugin._dedup_window > 0
                        and plugin._collapse_duplicate()
                        and plugin._is_pure_plain(message)
                    ):
                        await plugin._buffer_message(
                            sid, lambda m: orig(event, m), message
                        )
                        return None
                    try:
                        chain = getattr(message, "chain", None)
                        if chain is not None:
                            chain = plugin._apply_recent_sid(sid, list(chain))
                            if not chain:
                                return None
                            message.chain = chain
                    except Exception:
                        logger.error("[HideThinking] send 去重失败", exc_info=True)
                return await orig(event, message, *args, **kwargs)

            return patched_send

        def ctx_send_factory(orig):
            async def patched_ctx_send(ctx_self, session, message_chain, *args, **kwargs):
                if plugin._enabled() and not plugin._patching:
                    plugin._patching = True
                    try:
                        message_chain = plugin._scrub_message(message_chain)
                        chain = getattr(message_chain, "chain", None)
                        if chain is not None and len(chain) == 0:
                            return True
                    except Exception:
                        logger.error("[HideThinking] ctx send 清洗失败", exc_info=True)
                    finally:
                        plugin._patching = False
                    try:
                        sid = str(session)
                    except Exception:
                        sid = None
                    if sid:
                        if (
                            plugin._dedup_window > 0
                            and plugin._collapse_duplicate()
                            and plugin._is_pure_plain(message_chain)
                        ):
                            await plugin._buffer_message(
                                sid,
                                lambda m: orig(ctx_self, session, m),
                                message_chain,
                            )
                            return True
                        try:
                            chain = getattr(message_chain, "chain", None)
                            if chain is not None:
                                chain = plugin._apply_recent_sid(sid, list(chain))
                                if not chain:
                                    return True
                                message_chain.chain = chain
                        except Exception:
                            logger.error(
                                "[HideThinking] ctx send 去重失败", exc_info=True
                            )
                return await orig(ctx_self, session, message_chain, *args, **kwargs)

            return patched_ctx_send

        def stream_factory(orig):
            async def patched_send_streaming(event, generator, *args, **kwargs):
                if not plugin._enabled() or generator is None:
                    return await orig(event, generator, *args, **kwargs)
                sid = plugin._session_key(event)

                async def cleaned_gen():
                    async for chain in generator:
                        try:
                            chain = plugin._scrub_message(chain, sid)
                            inner = getattr(chain, "chain", None)
                            if inner is not None and len(inner) == 0:
                                continue
                        except Exception:
                            logger.error("[HideThinking] 流式清洗失败", exc_info=True)
                        yield chain

                return await orig(event, cleaned_gen(), *args, **kwargs)

            return patched_send_streaming

        for cls in self._iter_event_classes():
            self._patch_method(cls, "send", send_factory)
            self._patch_method(cls, "send_streaming", stream_factory)
        if _StarContext is not None:
            self._patch_method(_StarContext, "send_message", ctx_send_factory)
        logger.info("[HideThinking] 已拦截 %s 个 send 出口", len(self._patched))

    def _remove_send_patch(self) -> None:
        for cls, name, orig in reversed(self._patched):
            try:
                setattr(cls, name, orig)
            except Exception:
                continue
        self._patched.clear()

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
