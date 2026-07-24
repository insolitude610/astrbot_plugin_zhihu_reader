from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.agent.message import TextPart

from astrbot_plugin_zhihu_reader import main as plugin_main
from astrbot_plugin_zhihu_reader.reader import ZhihuDocument


class FakeReader:
    def __init__(
        self,
        *,
        cookie: str = "",
        timeout: float = 20.0,
        cache_ttl: int = 600,
        max_comments: int = 10,
        max_output_chars: int = 8_000,
    ) -> None:
        self.options = {
            "cookie": cookie,
            "timeout": timeout,
            "cache_ttl": cache_ttl,
            "max_comments": max_comments,
            "max_output_chars": max_output_chars,
        }
        self.calls: list[tuple[str, bool, int]] = []
        self.closed = False

    async def read(
        self,
        url: str,
        *,
        include_comments: bool = True,
        max_comments: int = 10,
    ) -> ZhihuDocument:
        self.calls.append((url, include_comments, max_comments))
        return ZhihuDocument(
            "[Zhihu answer]\nAnswer: temporary source body\n"
            "Comments captured: 1\n1. Reader: representative comment"
        )

    async def close(self) -> None:
        self.closed = True


class FakeConversationManager:
    def __init__(self, conversation: object) -> None:
        self.conversation = conversation
        self.created = False

    async def get_curr_conversation_id(self, origin: str) -> str | None:
        return "conversation-1"

    async def new_conversation(
        self, origin: str, *, platform_id: str
    ) -> str:
        self.created = True
        return "conversation-1"

    async def get_conversation(
        self, origin: str, conversation_id: str
    ) -> object:
        return self.conversation


class FakeContext:
    def __init__(self, conversation: object) -> None:
        self.conversation_manager = FakeConversationManager(conversation)


class FakeEvent:
    unified_msg_origin = "test:friend:1"

    def __init__(self) -> None:
        self.extras: dict[str, Any] = {}

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    def get_platform_id(self) -> str:
        return "test-platform"

    def request_llm(self, **kwargs: Any) -> ProviderRequest:
        return ProviderRequest(**kwargs)

    def plain_result(self, text: str) -> str:
        return text


class ZhihuReaderPluginTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(
        self,
        config: dict[str, Any] | None = None,
    ) -> tuple[plugin_main.ZhihuReaderPlugin, object]:
        conversation = object()
        original_reader = plugin_main.ZhihuReader
        plugin_main.ZhihuReader = FakeReader
        try:
            plugin = plugin_main.ZhihuReaderPlugin(
                FakeContext(conversation),
                config or {},
            )
        finally:
            plugin_main.ZhihuReader = original_reader
        return plugin, conversation

    async def test_auto_injection_is_temporary_and_uses_default_budget(self) -> None:
        plugin, _ = self.make_plugin()
        event = FakeEvent()
        request = ProviderRequest(
            prompt=(
                "总结 https://www.zhihu.com/appview/pin/1090928962359820288?utm_psn=test "
                "以及 https://zhuanlan.zhihu.com/p/789"
            )
        )

        await plugin.inject_zhihu_material(event, request)

        self.assertEqual(plugin.max_content_chars, 8_000)
        self.assertEqual(plugin.max_inject_chars, 12_000)
        self.assertEqual(plugin.max_urls, 1)
        self.assertEqual(len(plugin.reader.calls), 1)
        self.assertEqual(
            plugin.reader.calls[0],
            (
                "https://www.zhihu.com/pin/1090928962359820288",
                True,
                10,
            ),
        )
        self.assertEqual(len(request.extra_user_content_parts), 1)
        part = request.extra_user_content_parts[0]
        self.assertTrue(getattr(part, "_no_save", False))
        self.assertIn("temporary source body", part.text)
        self.assertLessEqual(len(part.text), 12_000)

    async def test_auto_injection_reads_urls_from_quoted_content(self) -> None:
        plugin, _ = self.make_plugin()
        event = FakeEvent()
        request = ProviderRequest(prompt="请总结这条引用")
        request.extra_user_content_parts.append(
            TextPart(
                text="<Quoted Message>知乎链接：https://www.zhihu.com/pin/123</Quoted Message>"
            )
        )

        await plugin.inject_zhihu_material(event, request)

        self.assertEqual(
            plugin.reader.calls[0],
            ("https://www.zhihu.com/pin/123", True, 10),
        )

    async def test_command_accepts_markdown_wrapped_url(self) -> None:
        plugin, _ = self.make_plugin()
        event = FakeEvent()

        results = [
            result
            async for result in plugin.read_zhihu_command(
                event,
                "[https://www.zhihu.com/pin/123](https://www.zhihu.com/pin/123)",
            )
        ]

        self.assertIsInstance(results[0], ProviderRequest)
        self.assertEqual(
            plugin.reader.calls[0],
            ("https://www.zhihu.com/pin/123", True, 10),
        )

    async def test_empty_document_is_reported_as_fetch_failure(self) -> None:
        plugin, _ = self.make_plugin()
        event = FakeEvent()

        async def empty_read(*args: Any, **kwargs: Any) -> ZhihuDocument:
            return ZhihuDocument("")

        plugin.reader.read = empty_read
        results = [
            result
            async for result in plugin.read_zhihu_command(
                event,
                "https://www.zhihu.com/pin/123",
            )
        ]

        self.assertIn("知乎内容抓取失败", results[0])
        self.assertNotIn("无法识别", results[0])

    async def test_command_requests_llm_in_current_conversation(self) -> None:
        plugin, conversation = self.make_plugin()
        event = FakeEvent()

        results = [
            result
            async for result in plugin.read_zhihu_command(
                event,
                "https://www.zhihu.com/question/123/answer/456",
            )
        ]

        self.assertEqual(len(results), 1)
        request = results[0]
        self.assertIsInstance(request, ProviderRequest)
        self.assertIs(request.conversation, conversation)
        self.assertIn("总结", request.prompt)
        self.assertNotIn("temporary source body", request.prompt)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        source_part = request.extra_user_content_parts[0]
        self.assertTrue(getattr(source_part, "_no_save", False))
        self.assertIn("representative comment", source_part.text)
        self.assertTrue(
            event.get_extra(plugin_main._SKIP_AUTO_INJECT_KEY, False)
        )

    async def test_command_creates_conversation_when_missing(self) -> None:
        plugin, conversation = self.make_plugin()
        manager = plugin.context.conversation_manager

        async def no_current(origin: str) -> None:
            return None

        manager.get_curr_conversation_id = no_current
        event = FakeEvent()
        results = [
            result
            async for result in plugin.read_zhihu_command(
                event,
                "https://zhuanlan.zhihu.com/p/789",
            )
        ]

        self.assertTrue(manager.created)
        self.assertIs(results[0].conversation, conversation)

    async def test_terminate_closes_reader(self) -> None:
        plugin, _ = self.make_plugin()
        await plugin.terminate()
        self.assertTrue(plugin.reader.closed)


if __name__ == "__main__":
    unittest.main()
