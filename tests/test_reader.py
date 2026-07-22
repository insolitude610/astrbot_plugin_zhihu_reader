from __future__ import annotations

import json
import unittest

import httpx

from reader import (
    InvalidZhihuUrlError,
    ZhihuReader,
    ZhihuRequestError,
    extract_zhihu_urls,
    html_to_text,
    parse_zhihu_url,
)


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


class ParseZhihuUrlTests(unittest.TestCase):
    def test_supported_url_shapes(self) -> None:
        answer = parse_zhihu_url(
            "https://www.zhihu.com/question/123/answer/456?utm_source=test#fragment"
        )
        self.assertEqual(answer.kind, "answer")
        self.assertEqual(answer.resource_id, 456)
        self.assertEqual(answer.question_id, 123)

        article = parse_zhihu_url("https://zhuanlan.zhihu.com/p/987/")
        self.assertEqual((article.kind, article.resource_id), ("article", 987))

        tardis = parse_zhihu_url("https://www.zhihu.com/tardis/zm/art/654")
        self.assertEqual((tardis.kind, tardis.resource_id), ("article", 654))

        question = parse_zhihu_url("http://zhihu.com/question/321")
        self.assertEqual((question.kind, question.resource_id), ("question", 321))

    def test_rejects_host_confusion_userinfo_and_unknown_paths(self) -> None:
        invalid_urls = [
            "https://www.zhihu.com.evil.example/question/1",
            "https://evil.example/?next=https://www.zhihu.com/question/1",
            "https://attacker@www.zhihu.com/question/1",
            "https://www.zhihu.com:443/question/1",
            "https://www.zhihu.com/people/someone",
            "javascript:https://www.zhihu.com/question/1",
        ]
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(InvalidZhihuUrlError):
                parse_zhihu_url(url)

    def test_html_to_text_ignores_scripts_and_preserves_structure(self) -> None:
        text = html_to_text(
            "<p>Hello <strong>world</strong>.</p>"
            "<script>steal()</script><ul><li>One</li><li>Two</li></ul>"
        )
        self.assertIn("Hello world.", text)
        self.assertIn("- One", text)
        self.assertIn("- Two", text)
        self.assertNotIn("steal", text)

    def test_extract_urls_keeps_only_valid_content_links(self) -> None:
        urls = extract_zhihu_urls(
            "Read https://www.zhihu.com/question/1/answer/2, "
            "ignore https://www.zhihu.com.evil.test/question/3 and "
            "also https://zhuanlan.zhihu.com/p/4."
        )
        self.assertEqual(
            urls,
            [
                "https://www.zhihu.com/question/1/answer/2",
                "https://zhuanlan.zhihu.com/p/4",
            ],
        )


class ZhihuReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_answer_and_bounded_nested_comments(self) -> None:
        root_offsets: list[int] = []
        child_offsets: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "www.zhihu.com")
            self.assertEqual(request.headers.get("cookie"), "z_c0=test-cookie")
            if request.url.path == "/api/v4/answers/456":
                return json_response(
                    {
                        "id": 456,
                        "question": {"id": 123, "title": "Why test?"},
                        "author": {"name": "Alice"},
                        "content": (
                            "<p>Hello <strong>world</strong>.</p>"
                            "<p>Second<br>line</p>"
                        ),
                        "voteup_count": 88,
                        "comment_count": 3,
                        "created_time": 1_700_000_000,
                        "updated_time": 1_700_000_100,
                    }
                )
            if (
                request.url.path
                == "/api/v4/comment_v5/answers/456/root_comment"
            ):
                offset = int(request.url.params["offset"])
                root_offsets.append(offset)
                self.assertEqual(request.url.params["order_by"], "score")
                if offset == 0:
                    return json_response(
                        {
                            "data": [
                                {
                                    "id": 100,
                                    "member": {"name": "Root user"},
                                    "content": "<p>Root comment</p>",
                                    "like_count": 5,
                                    "created_time": 1_700_000_200,
                                    "child_comment_count": 2,
                                    "child_comments": [
                                        {
                                            "id": 101,
                                            "author": {"member": {"name": "Child one"}},
                                            "content": "<p>First reply</p>",
                                            "like_count": 2,
                                            "created_time": 1_700_000_300,
                                        }
                                    ],
                                }
                            ],
                            "paging": {"is_end": False, "next": "https://evil.test/"},
                        }
                    )
                return json_response(
                    {
                        "data": [
                            {
                                "id": 200,
                                "author": {"name": "Second root"},
                                "content": "<p>Another comment</p>",
                                "like_count": 1,
                            }
                        ],
                        "paging": {"is_end": True},
                    }
                )
            if (
                request.url.path
                == "/api/v4/comment_v5/comment/100/child_comment"
            ):
                child_offsets.append(int(request.url.params["offset"]))
                return json_response(
                    {
                        "data": [
                            {
                                "id": 101,
                                "author": {"member": {"name": "Child one"}},
                                "content": "<p>First reply</p>",
                            },
                            {
                                "id": 102,
                                "member": {"name": "Child two"},
                                "content": "<p>Second reply</p>",
                                "like_count": 1,
                            },
                        ],
                        "paging": {"is_end": True},
                    }
                )
            return json_response({}, status_code=404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=test-cookie",
                max_comments=50,
                max_child_comments=2,
                max_comment_pages=3,
            )
            result = await reader.read(
                "https://www.zhihu.com/question/123/answer/456", max_comments=4
            )

        self.assertIn("[Zhihu answer]", result)
        self.assertIn("Question: Why test?", result)
        self.assertIn("Hello world.", result)
        self.assertIn("Second\nline", result)
        self.assertIn("Root comment", result)
        self.assertIn("First reply", result)
        self.assertIn("Second reply", result)
        self.assertIn("Another comment", result)
        self.assertEqual(root_offsets, [0, 1])
        self.assertEqual(child_offsets, [0])

    async def test_comment_api_error_does_not_drop_answer_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/answers/456":
                return json_response(
                    {
                        "question": {"title": "Readable answer"},
                        "author": {"name": "Alice"},
                        "content": "<p>The answer body remains available.</p>",
                    }
                )
            if request.url.path.endswith("/root_comment"):
                return json_response({}, status_code=403)
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=10)
            result = await reader.read("https://www.zhihu.com/answer/456")

        self.assertIn("The answer body remains available.", result)
        self.assertNotIn("Comments captured:", result)

    async def test_child_comment_error_keeps_root_comment(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/answers/456":
                return json_response(
                    {
                        "question": {"title": "Readable answer"},
                        "content": "<p>Body</p>",
                    }
                )
            if request.url.path.endswith("/root_comment"):
                return json_response(
                    {
                        "data": [
                            {
                                "id": 100,
                                "member": {"name": "Root user"},
                                "content": "<p>Root stays</p>",
                                "child_comment_count": 1,
                                "child_comments": [],
                            }
                        ],
                        "paging": {"is_end": True},
                    }
                )
            if request.url.path.endswith("/child_comment"):
                return json_response({}, status_code=403)
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=10)
            result = await reader.read("https://www.zhihu.com/answer/456")

        self.assertIn("Root stays", result)
        self.assertNotIn("Reply -", result)

    async def test_long_answer_preserves_comment_budget(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/answers/456":
                return json_response(
                    {
                        "question": {"title": "Long answer"},
                        "content": f"<p>{'body ' * 2_000}</p>",
                    }
                )
            if request.url.path.endswith("/root_comment"):
                return json_response(
                    {
                        "data": [
                            {
                                "id": 100,
                                "member": {"name": "Commenter"},
                                "content": "<p>Representative comment survives.</p>",
                            }
                        ],
                        "paging": {"is_end": True},
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                max_comments=10,
                max_output_chars=1_000,
            )
            result = await reader.read("https://www.zhihu.com/answer/456")

        self.assertLessEqual(len(result), 1_000)
        self.assertIn("[Content truncated.]", result)
        self.assertIn("Representative comment survives.", result)

    async def test_question_answers_are_sorted_by_upvotes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/questions/321":
                return json_response(
                    {
                        "title": "Which answer wins?",
                        "detail": "<p>Question detail</p>",
                        "answer_count": 2,
                        "comment_count": 0,
                        "follower_count": 7,
                    }
                )
            if request.url.path == "/api/v4/questions/321/answers":
                return json_response(
                    {
                        "data": [
                            {
                                "author": {"name": "Low"},
                                "content": "<p>Low answer</p>",
                                "voteup_count": 2,
                            },
                            {
                                "author": {"name": "High"},
                                "content": "<p>High answer</p>",
                                "voteup_count": 200,
                            },
                        ],
                        "paging": {"is_end": True},
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client, max_comments=0, max_question_answers=2
            )
            result = await reader.read("https://www.zhihu.com/question/321")

        self.assertIn("Question detail", result)
        self.assertLess(result.index("Author: High"), result.index("Author: Low"))
        self.assertIn("Upvotes: 200", result)

    async def test_article_uses_excerpt_when_content_is_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/articles/987":
                return json_response(
                    {
                        "title": "Fallback article",
                        "author": {"name": "Writer"},
                        "content": "",
                        "excerpt": "<p>Excerpt fallback</p>",
                        "voteup_count": 9,
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client, max_comments=0, timeout=5, cache_ttl=60
            )
            result = await reader.read("https://zhuanlan.zhihu.com/p/987")

        self.assertIn("[Zhihu article]", result)
        self.assertIn("Excerpt fallback", result)
        self.assertEqual(result.to_text(max_chars=10), str(result)[:10])

    async def test_ttl_cache_avoids_duplicate_requests(self) -> None:
        calls = 0
        now = [100.0]

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return json_response(
                {
                    "question": {"title": "Cached question"},
                    "author": {"name": "Cache author"},
                    "content": "<p>Cached body</p>",
                }
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cache_ttl_seconds=10,
                clock=lambda: now[0],
            )
            first = await reader.read(
                "https://www.zhihu.com/answer/456", include_comments=False
            )
            second = await reader.read(
                "https://www.zhihu.com/answer/456", include_comments=False
            )
            now[0] = 111.0
            third = await reader.read(
                "https://www.zhihu.com/answer/456", include_comments=False
            )

        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(calls, 2)

    async def test_redirect_is_not_followed(self) -> None:
        requested_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(request.url.host)
            return httpx.Response(
                302, headers={"Location": "https://attacker.example/secret"}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            reader = ZhihuReader(client=client, max_comments=0)
            with self.assertRaisesRegex(ZhihuRequestError, "not followed"):
                await reader.read("https://www.zhihu.com/answer/1")

        self.assertEqual(requested_hosts, ["www.zhihu.com"])

    async def test_response_size_limit_is_enforced(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response({"content": "x" * 1000})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client, max_comments=0, max_response_bytes=100
            )
            with self.assertRaisesRegex(ZhihuRequestError, "safety limit"):
                await reader.read("https://www.zhihu.com/answer/1")


if __name__ == "__main__":
    unittest.main()
