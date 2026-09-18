from __future__ import annotations

import asyncio
import sys
import types
import unittest
from types import SimpleNamespace
from typing import Any


def _install_stubs() -> None:
    if "astrbot.api" in sys.modules:
        return

    def _pkg(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    astrbot = _pkg("astrbot")
    api = _pkg("astrbot.api")
    event_mod = _pkg("astrbot.api.event")
    star_mod = _pkg("astrbot.api.star")
    provider_mod = _pkg("astrbot.api.provider")
    msg_mod = _pkg("astrbot.api.message_components")

    class _Logger:
        def info(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

    api.logger = _Logger()

    class filter:  # noqa: N801
        @staticmethod
        def on_llm_response(priority=0):
            def deco(fn):
                fn._priority = priority
                fn._hook = "on_llm_response"
                return fn

            return deco

        @staticmethod
        def on_decorating_result(priority=0):
            def deco(fn):
                fn._priority = priority
                fn._hook = "on_decorating_result"
                return fn

            return deco

    event_mod.filter = filter
    event_mod.AstrMessageEvent = object

    class Star:
        def __init__(self, context=None):
            self.context = context

    def register(*args, **kwargs):
        def deco(cls):
            cls._register_args = args
            cls._register_kwargs = kwargs
            return cls

        return deco

    star_mod.Context = object
    star_mod.Star = Star
    star_mod.register = register

    class LLMResponse:
        def __init__(self, completion_text="", reasoning_content=None, result_chain=None):
            self._completion_text = completion_text
            self.reasoning_content = reasoning_content
            self.result_chain = result_chain

        @property
        def completion_text(self):
            if self.result_chain and getattr(self.result_chain, "chain", None):
                parts = []
                for c in self.result_chain.chain:
                    if hasattr(c, "text") and c.text is not None:
                        parts.append(c.text)
                return "".join(parts)
            return self._completion_text

        @completion_text.setter
        def completion_text(self, value):
            if self.result_chain and getattr(self.result_chain, "chain", None):
                self.result_chain.chain = [
                    c for c in self.result_chain.chain if not isinstance(c, Plain)
                ]
                self.result_chain.chain.insert(0, Plain(value))
            else:
                self._completion_text = value

    provider_mod.LLMResponse = LLMResponse

    class Plain:
        def __init__(self, text=""):
            self.text = text

    msg_mod.Plain = Plain

    sys.modules["astrbot"].api = api


_install_stubs()

from astrbot.api.message_components import Plain  # noqa: E402
from astrbot.api.provider import LLMResponse  # noqa: E402

sys.path.insert(0, "/var/minis/workspace/astrbot_plugin_hide_thinking")
from main import (  # noqa: E402
    HideThinking,
    clean_reply_text,
    strip_injected_reasoning,
    strip_think_tags,
)


class FakeEvent:
    def __init__(self, chain=None, extra=None):
        self._extras = dict(extra or {})
        self._result = SimpleNamespace(chain=list(chain or []))

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_result(self):
        return self._result


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestStripThinkTags(unittest.TestCase):
    def test_pair_think(self):
        src = "<think>先想一下</think>你好"
        self.assertEqual(strip_think_tags(src), "你好")

    def test_pair_thinking(self):
        src = "<thinking>\n推理过程\n</thinking>\n今天天气不错"
        self.assertEqual(strip_think_tags(src).strip(), "今天天气不错")

    def test_chinese_tag(self):
        src = "<思考>内部</思考>正文"
        self.assertEqual(strip_think_tags(src), "正文")

    def test_unclosed_open_drops_rest(self):
        src = "前<think>后面全是思考"
        self.assertEqual(strip_think_tags(src), "前")

    def test_orphan_close_removed(self):
        src = "正文</think>还在"
        self.assertEqual(strip_think_tags(src), "正文还在")

    def test_unrelated_xml_kept(self):
        src = "<div>保留</div>文字"
        self.assertEqual(strip_think_tags(src), src)

    def test_markdown_block(self):
        src = "```thinking\n内部思考\n```\n答案"
        self.assertEqual(strip_think_tags(src).strip(), "答案")

    def test_nested(self):
        src = "<think>外<think>内</think>还在外</think>完"
        self.assertEqual(strip_think_tags(src), "完")

    def test_empty(self):
        self.assertEqual(strip_think_tags(""), "")
        self.assertEqual(strip_think_tags(None or ""), "")


class TestStripInjected(unittest.TestCase):
    def test_core_prefix(self):
        src = "🤔 思考: 一大段推理\n\n────\n真正回复"
        self.assertEqual(strip_injected_reasoning(src).strip(), "真正回复")

    def test_r1_filter_prefix(self):
        src = "🤔思考：内部推理\n\n真正回复"
        self.assertEqual(strip_injected_reasoning(src).strip(), "真正回复")

    def test_normal_emoji_kept(self):
        src = "你好🤔 这题难"
        self.assertEqual(strip_injected_reasoning(src), src)


class TestCleanReply(unittest.TestCase):
    def test_both(self):
        src = "🤔 思考: xx\n\n────\n<think>t</think>你好"
        self.assertEqual(clean_reply_text(src), "你好")

    def test_only_thinking_becomes_empty(self):
        src = "<think>只有思考</think>"
        self.assertEqual(clean_reply_text(src), "")


class TestPluginHooks(unittest.TestCase):
    def setUp(self):
        self.plugin = HideThinking(context=None, config={"enabled": True})

    def test_clears_extra_and_resp(self):
        event = FakeEvent(extra={"_llm_reasoning_content": "秘密思考"})
        resp = LLMResponse(completion_text="你好", reasoning_content="秘密思考")
        _run(self.plugin.on_llm_response(event, resp))
        self.assertIsNone(resp.reasoning_content)
        self.assertNotIn("_llm_reasoning_content", event.get_extra())
        self.assertEqual(resp.completion_text, "你好")

    def test_strips_completion_tags(self):
        event = FakeEvent()
        resp = LLMResponse(completion_text="<think>x</think>对外说")
        _run(self.plugin.on_llm_response(event, resp))
        self.assertEqual(resp.completion_text, "对外说")

    def test_decorating_drops_injected_plain(self):
        chain = [
            Plain("🤔 思考: 内部\n\n────\n"),
            Plain("对外正文"),
        ]
        event = FakeEvent(chain=chain, extra={"_llm_reasoning_content": "内部"})
        _run(self.plugin.on_decorating_result(event))
        texts = [c.text for c in event.get_result().chain]
        self.assertEqual(texts, ["对外正文"])
        self.assertNotIn("_llm_reasoning_content", event.get_extra())

    def test_decorating_drops_lark_card(self):
        card = SimpleNamespace(data={"type": "lark_collapsible_panel_reasoning", "content": "x"})
        event = FakeEvent(chain=[card, Plain("正文")])
        _run(self.plugin.on_decorating_result(event))
        self.assertEqual(len(event.get_result().chain), 1)
        self.assertEqual(event.get_result().chain[0].text, "正文")

    def test_disabled_noop(self):
        plugin = HideThinking(context=None, config={"enabled": False})
        event = FakeEvent(extra={"_llm_reasoning_content": "还在"})
        resp = LLMResponse(completion_text="<think>x</think>y", reasoning_content="还在")
        _run(plugin.on_llm_response(event, resp))
        self.assertEqual(resp.reasoning_content, "还在")
        self.assertEqual(event.get_extra("_llm_reasoning_content"), "还在")

    def test_no_hardcode_group_or_keyword(self):
        import inspect
        import os

        src = inspect.getsource(HideThinking)
        self.assertNotIn("946193448", src)
        main_path = os.path.join(
            os.path.dirname(__file__), "..", "main.py"
        )
        with open(main_path, encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn("sk-", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
