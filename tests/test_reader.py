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

        pin = parse_zhihu_url(
            "https://www.zhihu.com/pin/1090928962359820288?utm_psn=test"
        )
        self.assertEqual(
            (pin.kind, pin.resource_id, pin.canonical_url),
            (
                "pin",
                1090928962359820288,
                "https://www.zhihu.com/pin/1090928962359820288",
            ),
        )

        appview_pin = parse_zhihu_url(
            "https://www.zhihu.com/appview/pin/1196005326011543552"
        )
        self.assertEqual(
            (appview_pin.kind, appview_pin.resource_id),
            ("pin", 1196005326011543552),
        )

        mobile_pin = parse_zhihu_url(
            "https://www.zhihu.com/mobile/pin/1449471996145315840"
        )
        self.assertEqual(
            (mobile_pin.kind, mobile_pin.resource_id),
            ("pin", 1449471996145315840),
        )

    def test_rejects_host_confusion_userinfo_and_unknown_paths(self) -> None:
        invalid_urls = [
            "https://www.zhihu.com.evil.example/question/1",
            "https://evil.example/?next=https://www.zhihu.com/question/1",
            "https://attacker@www.zhihu.com/question/1",
            "https://www.zhihu.com:443/question/1",
            "https://www.zhihu.com/people/someone",
            "https://www.zhihu.com/pins/1090928962359820288",
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
            "also https://zhuanlan.zhihu.com/p/4 and "
            "https://www.zhihu.com/pin/5."
        )
        self.assertEqual(
            urls,
            [
                "https://www.zhihu.com/question/1/answer/2",
                "https://zhuanlan.zhihu.com/p/4",
                "https://www.zhihu.com/pin/5",
            ],
        )


class ZhihuReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_answer_and_bounded_nested_comments(self) -> None:
        root_offsets: list[str] = []
        child_offsets: list[str] = []

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
                offset = request.url.params["offset"]
                root_offsets.append(offset)
                self.assertEqual(request.url.params["order_by"], "score")
                if offset == "":
                    return json_response(
                        {
                            "data": [
                                {
                                    "id": 100,
                                    "member": {"name": "Root user"},
                                    "content": "<p>Root comment</p>",
                                    "like_count": 5,
                                    "created_time": 1_700_000_200,
                                    "child_comment_count": 3,
                                    "child_comment_next_offset": "embedded_cursor",
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
                            "paging": {
                                "is_end": False,
                                "next": (
                                    "https://www.zhihu.com/api/v4/comment_v5/"
                                    "answers/456/root_comment?limit=5&offset="
                                    "root_cursor&order_by=score"
                                ),
                            },
                        }
                    )
                return json_response(
                    {
                        "data": [
                            {
                                "id": 100,
                                "member": {"name": "Root user"},
                                "content": "<p>Root comment</p>",
                            },
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
                offset = request.url.params["offset"]
                child_offsets.append(offset)
                if offset == "embedded_cursor":
                    return json_response(
                        {
                            "data": [
                                {
                                    "id": 102,
                                    "member": {"name": "Child two"},
                                    "content": "<p>Second reply</p>",
                                    "like_count": 1,
                                }
                            ],
                            "paging": {
                                "is_end": False,
                                "next": (
                                    "https://www.zhihu.com/api/v4/comment_v5/"
                                    "comment/100/child_comment?limit=2&offset="
                                    "child_cursor"
                                ),
                            },
                        }
                    )
                return json_response(
                    {
                        "data": [
                            {
                                "id": 103,
                                "member": {"name": "Child three"},
                                "content": "<p>Third reply</p>",
                            }
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
                max_child_comments=3,
                max_comment_pages=3,
            )
            result = await reader.read(
                "https://www.zhihu.com/question/123/answer/456", max_comments=5
            )

        self.assertIn("[Zhihu answer]", result)
        self.assertIn("Question: Why test?", result)
        self.assertIn("Hello world.", result)
        self.assertIn("Second\nline", result)
        self.assertIn("Root comment", result)
        self.assertEqual(result.count("Root comment"), 1)
        self.assertIn("First reply", result)
        self.assertIn("Second reply", result)
        self.assertIn("Third reply", result)
        self.assertIn("Another comment", result)
        self.assertEqual(root_offsets, ["", "root_cursor"])
        self.assertEqual(child_offsets, ["embedded_cursor", "child_cursor"])

    async def test_comment_api_error_does_not_drop_answer_body(self) -> None:
        comment_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal comment_calls
            if request.url.path == "/api/v4/answers/456":
                return json_response(
                    {
                        "question": {"title": "Readable answer"},
                        "author": {"name": "Alice"},
                        "content": "<p>The answer body remains available.</p>",
                    }
                )
            if request.url.path.endswith("/root_comment"):
                comment_calls += 1
                return json_response({}, status_code=403)
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=10)
            result = await reader.read("https://www.zhihu.com/answer/456")
            second = await reader.read("https://www.zhihu.com/answer/456")

        self.assertIn("The answer body remains available.", result)
        self.assertNotIn("Comments captured:", result)
        self.assertIn("Comments unavailable: Zhihu denied access", result)
        self.assertEqual(result, second)
        self.assertEqual(comment_calls, 2)

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
                self.assertEqual(request.url.params["offset"], "")
                return json_response({}, status_code=403)
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=10)
            result = await reader.read("https://www.zhihu.com/answer/456")

        self.assertIn("Root stays", result)
        self.assertNotIn("Reply -", result)
        self.assertIn("Comments partially unavailable: Zhihu denied access", result)

    async def test_denied_cookie_retries_public_comments_anonymously(self) -> None:
        comment_cookies: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/answers/456":
                return json_response(
                    {
                        "question": {"title": "Readable answer"},
                        "content": "<p>Body</p>",
                        "comment_count": 1,
                    }
                )
            if request.url.path.endswith("/root_comment"):
                cookie = request.headers.get("cookie")
                comment_cookies.append(cookie)
                if cookie:
                    return json_response({}, status_code=401)
                return json_response(
                    {
                        "data": [
                            {
                                "id": 100,
                                "member": {"name": "Public commenter"},
                                "content": "<p>Public comment</p>",
                            }
                        ],
                        "paging": {"is_end": True},
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            cookies={"z_c0": "jar-cookie"},
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=expired-cookie",
                max_comments=10,
            )
            result = await reader.read("https://www.zhihu.com/answer/456")

        self.assertEqual(comment_cookies, ["z_c0=expired-cookie", ""])
        self.assertIn("Public comment", result)
        self.assertNotIn("Comments unavailable:", result)

    async def test_unsafe_comment_cursor_keeps_partial_result_uncached(self) -> None:
        comment_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal comment_calls
            self.assertEqual(request.url.host, "www.zhihu.com")
            if request.url.path == "/api/v4/answers/456":
                return json_response(
                    {
                        "question": {"title": "Readable answer"},
                        "content": "<p>Body</p>",
                        "comment_count": 2,
                    }
                )
            if request.url.path.endswith("/root_comment"):
                comment_calls += 1
                return json_response(
                    {
                        "data": [
                            {
                                "id": 100,
                                "member": {"name": "First commenter"},
                                "content": "<p>First comment remains</p>",
                            }
                        ],
                        "paging": {
                            "is_end": False,
                            "next": (
                                "https://attacker.example/api/v4/comment_v5/"
                                "answers/456/root_comment?offset=stolen"
                            ),
                        },
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=10)
            first = await reader.read("https://www.zhihu.com/answer/456")
            second = await reader.read("https://www.zhihu.com/answer/456")

        self.assertEqual(first, second)
        self.assertEqual(comment_calls, 2)
        self.assertIn("First comment remains", first)
        self.assertIn("Comments partially unavailable:", first)
        self.assertIn("did not provide a safe next cursor", first)

    async def test_empty_score_order_retries_latest_comments(self) -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/answers/456":
                return json_response(
                    {
                        "question": {"title": "Readable answer"},
                        "content": "<p>Body</p>",
                        "comment_count": 0,
                    }
                )
            if request.url.path.endswith("/root_comment"):
                order = request.url.params["order_by"]
                offset = request.url.params["offset"]
                requests.append((order, offset))
                if order == "score":
                    return json_response(
                        {"data": [], "paging": {"is_end": True}}
                    )
                if offset == "":
                    return json_response(
                        {
                            "data": [
                                {
                                    "id": 100,
                                    "member": {"name": "Latest user"},
                                    "content": "<p>Latest comment</p>",
                                }
                            ],
                            "paging": {
                                "is_end": False,
                                "next": (
                                    "https://www.zhihu.com/api/v4/comment_v5/"
                                    "answers/456/root_comment?limit=10&offset="
                                    "latest_cursor&order_by=ts"
                                ),
                            },
                        }
                    )
                return json_response(
                    {
                        "data": [
                            {
                                "id": 101,
                                "member": {"name": "Second latest user"},
                                "content": "<p>Second latest comment</p>",
                            }
                        ],
                        "paging": {"is_end": True},
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=10)
            result = await reader.read("https://www.zhihu.com/answer/456")

        self.assertEqual(
            requests,
            [("score", ""), ("ts", ""), ("ts", "latest_cursor")],
        )
        self.assertIn("Latest comment", result)
        self.assertIn("Second latest comment", result)
        self.assertNotIn("Comments unavailable:", result)

    async def test_comment_response_total_prevents_false_empty_cache(self) -> None:
        answer_calls = 0
        comment_requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal answer_calls
            if request.url.path == "/api/v4/answers/456":
                answer_calls += 1
                return json_response(
                    {
                        "question": {"title": "Readable answer"},
                        "content": "<p>Body</p>",
                    }
                )
            if request.url.path.endswith("/root_comment"):
                comment_requests.append(
                    (
                        request.url.params["order_by"],
                        request.url.params["offset"],
                    )
                )
                return json_response(
                    {
                        "counts": {"total_counts": 3},
                        "data": [],
                        "paging": {"is_end": False, "totals": 3},
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=10)
            first = await reader.read("https://www.zhihu.com/answer/456")
            second = await reader.read("https://www.zhihu.com/answer/456")

        self.assertEqual(first, second)
        self.assertEqual(answer_calls, 2)
        self.assertEqual(
            comment_requests,
            [("score", ""), ("ts", ""), ("score", ""), ("ts", "")],
        )
        self.assertIn("Zhihu reports 3 comments", first)

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

    async def test_question_reads_comments_for_captured_answers(self) -> None:
        comment_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/questions/321":
                return json_response(
                    {
                        "title": "Question with answer comments",
                        "answer_count": 2,
                        "comment_count": 0,
                    }
                )
            if request.url.path == "/api/v4/questions/321/answers":
                return json_response(
                    {
                        "data": [
                            {
                                "id": 10,
                                "author": {"name": "High"},
                                "content": "<p>High answer</p>",
                                "voteup_count": 100,
                                "comment_count": 2,
                            },
                            {
                                "id": 20,
                                "author": {"name": "Low"},
                                "content": "<p>Low answer</p>",
                                "voteup_count": 10,
                                "comment_count": 0,
                            },
                        ]
                    }
                )
            if request.url.path.endswith("/root_comment"):
                comment_paths.append(request.url.path)
                resource_id = request.url.path.split("/")[-2]
                return json_response(
                    {
                        "data": [
                            {
                                "id": int(resource_id) + 100,
                                "member": {"name": f"Commenter {resource_id}"},
                                "content": f"<p>Comment for {resource_id}</p>",
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
                max_comments=3,
                max_question_answers=2,
            )
            result = await reader.read("https://www.zhihu.com/question/321")

        self.assertEqual(
            comment_paths,
            [
                "/api/v4/comment_v5/answers/10/root_comment",
                "/api/v4/comment_v5/answers/20/root_comment",
                "/api/v4/comment_v5/questions/321/root_comment",
            ],
        )
        self.assertIn("Answer 1 comments captured: 1", result)
        self.assertIn("Comment for 10", result)
        self.assertIn("Answer 2 comments captured: 1", result)
        self.assertIn("Comment for 20", result)
        self.assertIn("Question comments captured: 1", result)
        self.assertIn("Comment for 321", result)

    async def test_question_missing_answer_id_is_diagnostic_and_uncached(self) -> None:
        question_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal question_calls
            if request.url.path == "/api/v4/questions/321":
                question_calls += 1
                return json_response(
                    {
                        "title": "Question with incomplete answer metadata",
                        "answer_count": 1,
                        "comment_count": 0,
                    }
                )
            if request.url.path == "/api/v4/questions/321/answers":
                return json_response(
                    {
                        "data": [
                            {
                                "author": {"name": "Missing ID"},
                                "content": "<p>Readable answer</p>",
                                "comment_count": 0,
                            }
                        ]
                    }
                )
            if request.url.path.endswith("/root_comment"):
                return json_response(
                    {"data": [], "paging": {"is_end": True}}
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=2)
            first = await reader.read("https://www.zhihu.com/question/321")
            second = await reader.read("https://www.zhihu.com/question/321")

        self.assertEqual(first, second)
        self.assertEqual(question_calls, 2)
        self.assertIn(
            "Answer 1 comments unavailable: Zhihu omitted the answer ID.",
            first,
        )

    async def test_reads_structured_pin_content_and_comments(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/api/v4/pins/1090928962359820288":
                return json_response(
                    {
                        "author": {"name": "Pin author"},
                        "content": [
                            {
                                "type": "text",
                                "content": "<p>A readable <strong>thought</strong>.</p>",
                            },
                            {
                                "type": "image",
                                "width": 640,
                                "height": 480,
                                "url": "https://pic.example/image.jpg",
                            },
                            {
                                "type": "video",
                                "duration": 12,
                                "video_id": "1090928925277990912",
                            },
                            {
                                "type": "link_card",
                                "data_draft_title": "Reference",
                                "url": "https://example.com/reference",
                            },
                        ],
                        "source_pin_id": "1016771952047792128",
                        "like_count": 4,
                        "reaction_count": 12,
                        "repin_count": 2,
                        "comment_count": 1,
                        "created": 1_700_000_000,
                        "updated": 1_700_000_100,
                    }
                )
            if request.url.path == (
                "/api/v4/comment_v5/pins/1090928962359820288/root_comment"
            ):
                self.assertEqual(request.url.params["offset"], "")
                return json_response(
                    {
                        "data": [
                            {
                                "id": 900,
                                "member": {"name": "Pin commenter"},
                                "content": "<p>Thought comment</p>",
                                "like_count": 3,
                            }
                        ],
                        "paging": {"is_end": True},
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=2)
            result = await reader.read(
                "https://www.zhihu.com/pin/1090928962359820288"
            )

        self.assertEqual(
            requested_paths,
            [
                "/api/v4/pins/1090928962359820288",
                "/api/v4/comment_v5/pins/1090928962359820288/root_comment",
            ],
        )
        self.assertIn("[Zhihu thought]", result)
        self.assertIn("Author: Pin author", result)
        self.assertIn("A readable thought.", result)
        self.assertIn("[Image: 640x480]", result)
        self.assertIn("[Video: 12 seconds]", result)
        self.assertIn(
            "[Link card: Reference - https://example.com/reference]",
            result,
        )
        self.assertIn(
            "Repin source: https://www.zhihu.com/pin/1016771952047792128",
            result,
        )
        self.assertIn("Comments captured: 1", result)
        self.assertIn("Thought comment", result)

    async def test_pin_uses_content_html_fallback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/pins/1196005326011543552":
                return json_response(
                    {
                        "author": {"name": "Photo author"},
                        "content": [],
                        "content_html": "<div>Photo thought</div><img alt='cat'>",
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=0)
            result = await reader.read(
                "https://www.zhihu.com/pin/1196005326011543552"
            )

        self.assertIn("Photo thought", result)
        self.assertIn("[Image: cat]", result)

    async def test_deleted_pin_reports_its_reason(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/pins/1090928962359820288":
                return json_response(
                    {
                        "is_deleted": True,
                        "deleted_reason": "<p>This thought was removed.</p>",
                    }
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(client=client, max_comments=0)
            with self.assertRaisesRegex(
                ZhihuRequestError,
                "This thought was removed",
            ):
                await reader.read(
                    "https://www.zhihu.com/pin/1090928962359820288"
                )

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

    async def test_authenticated_article_page_fallback_reads_initial_data(
        self,
    ) -> None:
        article_id = 2063248023589741612
        requests: list[tuple[str, str]] = []
        initial_data = {
            "initialState": {
                "entities": {
                    "articles": {
                        str(article_id): {
                            "title": "VIP article",
                            "author": {"name": "Member author"},
                            "content": "<p>Complete member-only body.</p>",
                            "comment_count": 0,
                            "voteup_count": 8,
                        }
                    }
                }
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.url.host, request.url.path))
            self.assertEqual(request.headers.get("cookie"), "z_c0=vip-cookie")
            if request.url.host == "www.zhihu.com":
                return json_response({}, status_code=403)
            if request.url.host == "zhuanlan.zhihu.com":
                self.assertIn("text/html", request.headers["accept"])
                self.assertEqual(request.headers["sec-fetch-dest"], "document")
                page = (
                    "<!doctype html><html><body>"
                    '<script id="js-initialData" type="text/json">'
                    f"{json.dumps(initial_data)}"
                    "</script></body></html>"
                )
                return httpx.Response(200, content=page.encode("utf-8"))
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                authenticated_article_fallback=True,
            )
            result = await reader.read(
                f"https://zhuanlan.zhihu.com/p/{article_id}"
            )

        self.assertEqual(
            requests,
            [
                ("www.zhihu.com", f"/api/v4/articles/{article_id}"),
                ("zhuanlan.zhihu.com", f"/p/{article_id}"),
            ],
        )
        self.assertIn("Title: VIP article", result)
        self.assertIn("Complete member-only body.", result)
        self.assertNotIn("Preview only", result)

    async def test_paid_api_preview_uses_authorized_page_content(self) -> None:
        article_id = 2063248023589741612
        requested_hosts: list[str] = []
        initial_data = {
            "initialState": {
                "entities": {
                    "articles": {
                        str(article_id): {
                            "title": "Purchased article",
                            "author": {"name": "Member author"},
                            "content": "<p>The complete purchased article body.</p>",
                            "is_paid": True,
                            "has_purchased": True,
                            "can_read": True,
                            "comment_count": 0,
                        }
                    }
                }
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(request.url.host)
            if request.url.host == "www.zhihu.com":
                return json_response(
                    {
                        "title": "Purchased article",
                        "content": "<p>Visible teaser from the API.</p>",
                        "excerpt": "<p>Visible teaser from the API.</p>",
                        "is_paid": True,
                        "has_purchased": False,
                        "can_read": False,
                        "comment_count": 0,
                    }
                )
            if request.url.host == "zhuanlan.zhihu.com":
                page = (
                    '<script id="js-initialData" type="text/json">'
                    f"{json.dumps(initial_data)}"
                    "</script>"
                )
                return httpx.Response(200, content=page.encode("utf-8"))
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                authenticated_article_fallback=True,
            )
            result = await reader.read(
                f"https://zhuanlan.zhihu.com/p/{article_id}"
            )

        self.assertEqual(requested_hosts, ["www.zhihu.com", "zhuanlan.zhihu.com"])
        self.assertIn("The complete purchased article body.", result)
        self.assertNotIn("Visible teaser from the API.", result)
        self.assertNotIn("Access: Preview only", result)

    async def test_paid_prompt_without_fallback_is_uncached_preview(self) -> None:
        api_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls
            self.assertEqual(request.url.host, "www.zhihu.com")
            api_calls += 1
            return json_response(
                {
                    "title": "Paid preview",
                    "content": "<p>开通盐选会员，阅读全文。</p>",
                    "is_paid": True,
                    "has_purchased": False,
                    "comment_count": 0,
                }
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                cache_ttl_seconds=60,
                authenticated_article_fallback=False,
            )
            first = await reader.read("https://zhuanlan.zhihu.com/p/987")
            second = await reader.read("https://zhuanlan.zhihu.com/p/987")

        self.assertEqual(first, second)
        self.assertEqual(api_calls, 2)
        self.assertIn("Access: Preview only", first)
        self.assertIn("Article preview:", first)
        self.assertIn("开通盐选会员，阅读全文。", first)

    async def test_locked_article_page_is_uncached_preview(self) -> None:
        api_calls = 0
        page_calls = 0
        article_id = 987
        initial_data = {
            "initialState": {
                "entities": {
                    "articles": {
                        str(article_id): {
                            "title": "Still locked",
                            "content": "<p>购买后解锁全文。</p>",
                            "is_paid": True,
                            "is_locked": True,
                            "has_purchased": False,
                            "comment_count": 0,
                        }
                    }
                }
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls, page_calls
            if request.url.host == "www.zhihu.com":
                api_calls += 1
                return json_response(
                    {
                        "title": "Still locked",
                        "content": "<p>Visible API preview.</p>",
                        "excerpt": "<p>Visible API preview.</p>",
                        "is_paid": True,
                        "can_read": False,
                        "comment_count": 0,
                    }
                )
            if request.url.host == "zhuanlan.zhihu.com":
                page_calls += 1
                page = (
                    '<script id="js-initialData" type="text/json">'
                    f"{json.dumps(initial_data)}"
                    "</script>"
                )
                return httpx.Response(200, content=page.encode("utf-8"))
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                cache_ttl_seconds=60,
                authenticated_article_fallback=True,
            )
            first = await reader.read(
                f"https://zhuanlan.zhihu.com/p/{article_id}"
            )
            second = await reader.read(
                f"https://zhuanlan.zhihu.com/p/{article_id}"
            )

        self.assertEqual(first, second)
        self.assertEqual((api_calls, page_calls), (2, 2))
        self.assertIn("Access: Preview only", first)
        self.assertIn("Article preview:", first)

    async def test_paid_page_without_entitlement_is_uncached_preview(self) -> None:
        api_calls = 0
        page_calls = 0
        article_id = 987
        initial_data = {
            "initialState": {
                "entities": {
                    "articles": {
                        str(article_id): {
                            "title": "Unconfirmed paid access",
                            "content": (
                                "<p>A short opening scene that stops before the "
                                "rest of the story.</p>"
                            ),
                            "is_paid": True,
                            "comment_count": 0,
                        }
                    }
                }
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls, page_calls
            if request.url.host == "www.zhihu.com":
                api_calls += 1
                return json_response({}, status_code=403)
            if request.url.host == "zhuanlan.zhihu.com":
                page_calls += 1
                page = (
                    '<script id="js-initialData" type="text/json">'
                    f"{json.dumps(initial_data)}"
                    "</script>"
                )
                return httpx.Response(200, content=page.encode("utf-8"))
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                cache_ttl_seconds=60,
                authenticated_article_fallback=True,
            )
            first = await reader.read(
                f"https://zhuanlan.zhihu.com/p/{article_id}"
            )
            second = await reader.read(
                f"https://zhuanlan.zhihu.com/p/{article_id}"
            )

        self.assertEqual(first, second)
        self.assertEqual((api_calls, page_calls), (2, 2))
        self.assertIn("Access: Preview only", first)
        self.assertIn("Article preview:", first)
        self.assertIn("A short opening scene", first)

    async def test_authorized_paid_api_content_skips_page_fallback(self) -> None:
        for access_field in ("has_purchased", "can_read"):
            with self.subTest(access_field=access_field):
                requested_hosts: list[str] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    requested_hosts.append(request.url.host)
                    if request.url.host == "www.zhihu.com":
                        return json_response(
                            {
                                "title": "Authorized paid article",
                                "content": "<p>Paid full body from the API.</p>",
                                "is_paid": True,
                                access_field: True,
                                "comment_count": 0,
                            }
                        )
                    return json_response({}, status_code=500)

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as client:
                    reader = ZhihuReader(
                        client=client,
                        cookie="z_c0=vip-cookie",
                        max_comments=0,
                        authenticated_article_fallback=True,
                    )
                    result = await reader.read(
                        "https://zhuanlan.zhihu.com/p/987"
                    )

                self.assertEqual(requested_hosts, ["www.zhihu.com"])
                self.assertIn("Paid full body from the API.", result)
                self.assertNotIn("Access: Preview only", result)

    async def test_positive_read_access_overrides_unpurchased_flag(self) -> None:
        requested_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(request.url.host)
            if request.url.host == "www.zhihu.com":
                return json_response(
                    {
                        "title": "Readable subscription article",
                        "content": "<p>The complete readable paid article.</p>",
                        "is_paid": True,
                        "has_purchased": False,
                        "can_read": True,
                        "comment_count": 0,
                    }
                )
            return json_response({}, status_code=500)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                authenticated_article_fallback=True,
            )
            result = await reader.read("https://zhuanlan.zhihu.com/p/987")

        self.assertEqual(requested_hosts, ["www.zhihu.com"])
        self.assertIn("The complete readable paid article.", result)
        self.assertNotIn("Access: Preview only", result)

    async def test_unreadable_purchased_api_teaser_is_uncached_preview(
        self,
    ) -> None:
        api_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls
            self.assertEqual(request.url.host, "www.zhihu.com")
            api_calls += 1
            return json_response(
                {
                    "title": "Currently unreadable purchase",
                    "content": (
                        "<p>A brief introductory passage ending before the "
                        "substantive discussion.</p>"
                    ),
                    "is_paid": True,
                    "has_purchased": True,
                    "can_read": False,
                    "comment_count": 0,
                }
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                cache_ttl_seconds=60,
                authenticated_article_fallback=False,
            )
            first = await reader.read("https://zhuanlan.zhihu.com/p/987")
            second = await reader.read("https://zhuanlan.zhihu.com/p/987")

        self.assertEqual(first, second)
        self.assertEqual(api_calls, 2)
        self.assertIn("Access: Preview only", first)
        self.assertIn("Article preview:", first)
        self.assertIn("A brief introductory passage", first)

    async def test_authorized_api_purchase_prompt_is_uncached_preview(
        self,
    ) -> None:
        api_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls
            self.assertEqual(request.url.host, "www.zhihu.com")
            api_calls += 1
            return json_response(
                {
                    "title": "Misleading access flags",
                    "content": "<p>开通盐选会员，解锁全文。</p>",
                    "is_paid": True,
                    "has_purchased": True,
                    "can_read": True,
                    "comment_count": 0,
                }
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                cache_ttl_seconds=60,
                authenticated_article_fallback=False,
            )
            first = await reader.read("https://zhuanlan.zhihu.com/p/987")
            second = await reader.read("https://zhuanlan.zhihu.com/p/987")

        self.assertEqual(first, second)
        self.assertEqual(api_calls, 2)
        self.assertIn("Access: Preview only", first)
        self.assertIn("Article preview:", first)
        self.assertIn("开通盐选会员，解锁全文。", first)

    async def test_authenticated_article_fallback_is_opt_in(self) -> None:
        requested_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(request.url.host)
            return json_response({}, status_code=403)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
            )
            with self.assertRaisesRegex(
                ZhihuRequestError,
                "Enable authenticated article fallback",
            ):
                await reader.read("https://zhuanlan.zhihu.com/p/987")

        self.assertEqual(requested_hosts, ["www.zhihu.com"])

    async def test_blocked_article_page_returns_uncached_preview(self) -> None:
        api_calls = 0
        page_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls, page_calls
            if request.url.host == "www.zhihu.com":
                api_calls += 1
                return json_response(
                    {
                        "title": "Preview article",
                        "content": "",
                        "excerpt": "<p>Visible preview.</p>",
                    }
                )
            if request.url.host == "zhuanlan.zhihu.com":
                page_calls += 1
                return httpx.Response(403)
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                cache_ttl_seconds=60,
                authenticated_article_fallback=True,
            )
            first = await reader.read("https://zhuanlan.zhihu.com/p/987")
            second = await reader.read("https://zhuanlan.zhihu.com/p/987")

        self.assertEqual(first, second)
        self.assertEqual((api_calls, page_calls), (2, 2))
        self.assertIn("Access: Preview only", first)
        self.assertIn("Visible preview.", first)
        self.assertIn("browser verification may be required", first)

    async def test_article_page_challenge_is_not_executed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "www.zhihu.com":
                return json_response({}, status_code=403)
            if request.url.host == "zhuanlan.zhihu.com":
                return httpx.Response(
                    200,
                    content=(
                        '<html><head><meta id="zh-zse-ck" content="opaque">'
                        "</head><body><script>challenge()</script></body></html>"
                    ).encode("utf-8"),
                )
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                authenticated_article_fallback=True,
            )
            with self.assertRaisesRegex(
                ZhihuRequestError,
                "interactive browser verification",
            ):
                await reader.read("https://zhuanlan.zhihu.com/p/987")

    async def test_article_page_challenge_rejects_valid_initial_data(self) -> None:
        article_id = 987
        initial_data = {
            "initialState": {
                "entities": {
                    "articles": {
                        str(article_id): {
                            "title": "Data behind challenge",
                            "content": "<p>This must not be accepted.</p>",
                            "has_purchased": True,
                            "can_read": True,
                        }
                    }
                }
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "www.zhihu.com":
                return json_response({}, status_code=403)
            if request.url.host == "zhuanlan.zhihu.com":
                page = (
                    '<html><head><meta id="zh-zse-ck" content="opaque">'
                    "</head><body>"
                    '<script id="js-initialData" type="text/json">'
                    f"{json.dumps(initial_data)}"
                    "</script></body></html>"
                )
                return httpx.Response(200, content=page.encode("utf-8"))
            return json_response({}, status_code=404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                authenticated_article_fallback=True,
            )
            with self.assertRaisesRegex(
                ZhihuRequestError,
                "interactive browser verification",
            ):
                await reader.read(
                    f"https://zhuanlan.zhihu.com/p/{article_id}"
                )

    async def test_short_free_article_equal_to_excerpt_is_cached(self) -> None:
        api_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls
            self.assertEqual(request.url.host, "www.zhihu.com")
            api_calls += 1
            return json_response(
                {
                    "title": "Short free article",
                    "content": "<p>A complete short free article.</p>",
                    "excerpt": "<p>A complete short free article.</p>",
                    "is_paid": False,
                    "comment_count": 0,
                }
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                cache_ttl_seconds=60,
                authenticated_article_fallback=True,
            )
            first = await reader.read("https://zhuanlan.zhihu.com/p/987")
            second = await reader.read("https://zhuanlan.zhihu.com/p/987")

        self.assertEqual(first, second)
        self.assertEqual(api_calls, 1)
        self.assertIn("Article:\nA complete short free article.", first)
        self.assertNotIn("Access: Preview only", first)

    async def test_article_page_redirect_is_not_followed(self) -> None:
        requested_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(request.url.host)
            if request.url.host == "www.zhihu.com":
                return json_response({}, status_code=403)
            return httpx.Response(
                302,
                headers={"Location": "https://attacker.example/private"},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                authenticated_article_fallback=True,
            )
            with self.assertRaisesRegex(
                ZhihuRequestError,
                "redirect was not followed",
            ):
                await reader.read("https://zhuanlan.zhihu.com/p/987")

        self.assertEqual(
            requested_hosts,
            ["www.zhihu.com", "zhuanlan.zhihu.com"],
        )

    async def test_article_page_response_size_limit_is_enforced(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "www.zhihu.com":
                return json_response({}, status_code=403)
            return httpx.Response(200, content=b"x" * 1_000)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            reader = ZhihuReader(
                client=client,
                cookie="z_c0=vip-cookie",
                max_comments=0,
                max_response_bytes=100,
                authenticated_article_fallback=True,
            )
            with self.assertRaisesRegex(
                ZhihuRequestError,
                "safety limit",
            ):
                await reader.read("https://zhuanlan.zhihu.com/p/987")

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
