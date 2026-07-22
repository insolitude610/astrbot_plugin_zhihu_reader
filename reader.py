from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Literal, Mapping
from urllib.parse import parse_qs, urlsplit

import httpx


_LOGGER = logging.getLogger(__name__)
_API_ORIGIN = "https://www.zhihu.com"
_ARTICLE_ORIGIN = "https://zhuanlan.zhihu.com"
_SUPPORTED_HOSTS = {"zhihu.com", "www.zhihu.com", "zhuanlan.zhihu.com"}
_RESOURCE_ID = r"([1-9]\d{0,29})"
_HARD_MAX_COMMENTS = 50
_SAFE_API_ENDPOINTS = (
    re.compile(
        r"/api/v4/(?:answers|articles|pins|questions)/[1-9]\d{0,29}"
    ),
    re.compile(r"/api/v4/questions/[1-9]\d{0,29}/answers"),
    re.compile(
        r"/api/v4/comment_v5/(?:answers|articles|pins|questions)/"
        r"[1-9]\d{0,29}/root_comment"
    ),
    re.compile(
        r"/api/v4/comment_v5/comment/[1-9]\d{0,29}/child_comment"
    ),
)


class ZhihuReaderError(Exception):
    """Base error suitable for showing to a bot user."""


class InvalidZhihuUrlError(ZhihuReaderError, ValueError):
    """Raised when a URL is not a supported Zhihu content URL."""


class ZhihuRequestError(ZhihuReaderError):
    """Raised when Zhihu cannot be queried safely or successfully."""


class _ZhihuAccessDeniedError(ZhihuRequestError):
    """Raised when Zhihu rejects the current authentication state."""


class ZhihuDocument(str):
    """Formatted Zhihu material with a bounded text rendering method."""

    def to_text(self, max_chars: int | None = None) -> str:
        """Return the formatted text, optionally truncated to a hard limit."""
        text = str(self)
        if max_chars is None or len(text) <= max_chars:
            return text
        if max_chars <= 0:
            return ""
        marker = "\n\n[Output truncated.]"
        if max_chars <= len(marker):
            return text[:max_chars]
        return text[: max_chars - len(marker)].rstrip() + marker


@dataclass(frozen=True, slots=True)
class ZhihuTarget:
    """A validated Zhihu resource extracted from an untrusted URL."""

    kind: Literal["answer", "article", "pin", "question"]
    resource_id: int
    question_id: int | None = None

    @property
    def canonical_url(self) -> str:
        """Return a canonical public URL built only from validated numeric IDs."""
        if self.kind == "answer":
            if self.question_id is not None:
                return (
                    f"https://www.zhihu.com/question/{self.question_id}"
                    f"/answer/{self.resource_id}"
                )
            return f"https://www.zhihu.com/answer/{self.resource_id}"
        if self.kind == "article":
            return f"https://zhuanlan.zhihu.com/p/{self.resource_id}"
        if self.kind == "pin":
            return f"https://www.zhihu.com/pin/{self.resource_id}"
        return f"https://www.zhihu.com/question/{self.resource_id}"


@dataclass(frozen=True, slots=True)
class _Comment:
    author: str
    text: str
    votes: int
    created_at: str
    children: tuple[_Comment, ...] = ()


@dataclass(frozen=True, slots=True)
class _CommentsResult:
    comments: tuple[_Comment, ...] = ()
    error: str | None = None

    @property
    def captured_count(self) -> int:
        return sum(1 + len(comment.children) for comment in self.comments)


@dataclass(frozen=True, slots=True)
class _RenderedDocument:
    text: str
    comments_complete: bool = True
    cacheable: bool = True


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    text: str


class _HTMLTextExtractor(HTMLParser):
    """Convert API-provided HTML into compact text without loading any assets."""

    _BLOCK_TAGS = {
        "article",
        "blockquote",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
    _IGNORED_TAGS = {"script", "style", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "img":
            attributes = dict(attrs)
            alt = str(attributes.get("alt") or "").strip()
            self.parts.append(f"[Image: {alt}]" if alt else "[Image]")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


class _ArticleInitialDataParser(HTMLParser):
    """Capture Zhihu's JSON initial-data script without executing page code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.has_zse_challenge = False
        self._capturing = False
        self._parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {key.lower(): value for key, value in attrs}
        if tag.lower() == "meta" and (
            str(attributes.get("id") or "").lower() == "zh-zse-ck"
        ):
            self.has_zse_challenge = True
        if tag.lower() != "script":
            return
        script_id = str(attributes.get("id") or "").lower()
        if script_id == "js-initialdata":
            self._capturing = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.scripts.append("".join(self._parts))
            self._capturing = False
            self._parts = []


def html_to_text(value: object) -> str:
    """Convert Zhihu HTML or plain text into normalized readable text."""
    if value is None:
        return ""
    source = str(value)
    parser = _HTMLTextExtractor()
    try:
        parser.feed(source)
        parser.close()
        source = "".join(parser.parts)
    except Exception:
        source = re.sub(r"<[^>]*>", " ", source)

    normalized_lines: list[str] = []
    previous_blank = True
    for raw_line in source.replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t\f\v ]+", " ", raw_line).strip()
        if line:
            normalized_lines.append(line)
            previous_blank = False
        elif not previous_blank:
            normalized_lines.append("")
            previous_blank = True
    return "\n".join(normalized_lines).strip()


def parse_zhihu_url(url: str) -> ZhihuTarget:
    """Parse a supported Zhihu URL without making a network request.

    Accepted forms include question answers, questions, Zhuanlan articles,
    Zhihu thoughts, and mobile Tardis article URLs. The original URL is never
    used as a request destination.

    Args:
        url: The user-provided URL.

    Returns:
        A validated target containing numeric resource IDs.

    Raises:
        InvalidZhihuUrlError: If the URL is malformed or unsupported.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidZhihuUrlError(
            "Please provide a Zhihu answer, article, thought, or question URL."
        )

    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise InvalidZhihuUrlError("The Zhihu URL is malformed.") from exc

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or host not in _SUPPORTED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise InvalidZhihuUrlError(
            "Only direct Zhihu answer, article, thought, and question URLs are "
            "supported."
        )

    path = re.sub(r"/{2,}", "/", parsed.path)
    if host == "zhuanlan.zhihu.com":
        match = re.fullmatch(rf"/p/{_RESOURCE_ID}/?", path)
        if match:
            return ZhihuTarget("article", int(match.group(1)))
        raise InvalidZhihuUrlError("This Zhuanlan URL does not identify an article.")

    match = re.fullmatch(
        rf"/question/{_RESOURCE_ID}/answer/{_RESOURCE_ID}/?", path
    )
    if match:
        return ZhihuTarget(
            "answer", int(match.group(2)), question_id=int(match.group(1))
        )

    match = re.fullmatch(rf"/answer/{_RESOURCE_ID}/?", path)
    if match:
        return ZhihuTarget("answer", int(match.group(1)))

    match = re.fullmatch(rf"/question/{_RESOURCE_ID}/?", path)
    if match:
        return ZhihuTarget("question", int(match.group(1)))

    match = re.fullmatch(rf"/(?:appview/|mobile/)?pin/{_RESOURCE_ID}/?", path)
    if match:
        return ZhihuTarget("pin", int(match.group(1)))

    match = re.fullmatch(
        rf"/tardis/(?:[A-Za-z0-9_-]+/)*art/{_RESOURCE_ID}/?", path
    )
    if match:
        return ZhihuTarget("article", int(match.group(1)))

    raise InvalidZhihuUrlError("The URL does not identify a supported Zhihu resource.")


def parse_url(url: str) -> ZhihuTarget | None:
    """Return a parsed target, or ``None`` for compatibility with plugin callers."""
    try:
        return parse_zhihu_url(url)
    except InvalidZhihuUrlError:
        return None


def extract_zhihu_urls(text: str) -> list[str]:
    """Extract validated direct Zhihu content URLs from arbitrary message text."""
    if not isinstance(text, str) or not text:
        return []

    urls: list[str] = []
    trailing = ".,;:!?)]}>\"'\u3002\uff0c\uff1b\uff1a\uff01\uff1f\uff09\u3011\u300b"
    for match in re.finditer(r"https?://[^\s<>\"']+", text, flags=re.IGNORECASE):
        candidate = match.group(0).rstrip(trailing)
        if parse_url(candidate) is not None:
            urls.append(candidate)
        if len(urls) >= 20:
            break
    return urls


class ZhihuReader:
    """Fetch and format Zhihu resources through fixed official API endpoints."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        cookie: str = "",
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 2_000_000,
        max_comments: int = 20,
        max_child_comments: int = 3,
        max_comment_pages: int = 2,
        max_question_answers: int = 3,
        max_output_chars: int = 30_000,
        cache_ttl_seconds: float = 300.0,
        max_cache_entries: int = 128,
        authenticated_article_fallback: bool = False,
        clock: Callable[[], float] = time.monotonic,
        timeout: float | None = None,
        cache_ttl: float | None = None,
    ) -> None:
        if timeout is not None:
            timeout_seconds = timeout
        if cache_ttl is not None:
            cache_ttl_seconds = cache_ttl
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if max_comments < 0 or max_child_comments < 0:
            raise ValueError("comment limits cannot be negative")
        if max_comment_pages <= 0 or max_question_answers <= 0:
            raise ValueError("page and answer limits must be positive")
        if max_output_chars <= 0 or cache_ttl_seconds < 0:
            raise ValueError("output and cache limits are invalid")
        if max_cache_entries <= 0:
            raise ValueError("max_cache_entries must be positive")
        if "\r" in cookie or "\n" in cookie:
            raise ValueError("cookie cannot contain line breaks")

        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._max_comments = max_comments
        self._max_child_comments = max_child_comments
        self._max_comment_pages = max_comment_pages
        self._max_question_answers = max_question_answers
        self._max_output_chars = max_output_chars
        self._cache_ttl_seconds = cache_ttl_seconds
        self._max_cache_entries = max_cache_entries
        self._authenticated_article_fallback = bool(
            authenticated_article_fallback
        )
        self._configured_cookie = bool(cookie.strip())
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            ),
            "Referer": "https://www.zhihu.com/",
        }
        if cookie.strip():
            self._headers["Cookie"] = cookie.strip()

        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        )

    async def __aenter__(self) -> ZhihuReader:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally-created HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    async def close(self) -> None:
        """Compatibility alias used by the AstrBot plugin lifecycle."""
        await self.aclose()

    def clear_cache(self) -> None:
        """Clear all cached formatted results."""
        self._cache.clear()

    async def read(
        self,
        url: str,
        *,
        include_comments: bool = True,
        max_comments: int | None = None,
    ) -> ZhihuDocument:
        """Read a supported Zhihu URL and return bounded LLM reference text."""
        target = parse_zhihu_url(url)
        comment_limit = self._max_comments if max_comments is None else max_comments
        if comment_limit < 0:
            raise ValueError("max_comments cannot be negative")
        if not include_comments:
            comment_limit = 0
        comment_limit = min(comment_limit, _HARD_MAX_COMMENTS)
        cache_key = f"{target.kind}:{target.resource_id}:comments:{comment_limit}"
        now = self._clock()
        cached = self._cache.get(cache_key)
        if cached and cached.expires_at > now:
            return ZhihuDocument(cached.text)
        if cached:
            self._cache.pop(cache_key, None)

        if target.kind == "answer":
            rendered = await self._read_answer(target, comment_limit)
        elif target.kind == "article":
            rendered = await self._read_article(target, comment_limit)
        elif target.kind == "pin":
            rendered = await self._read_pin(target, comment_limit)
        else:
            rendered = await self._read_question(target, comment_limit)

        result = self._truncate(rendered.text)
        if (
            self._cache_ttl_seconds > 0
            and rendered.comments_complete
            and rendered.cacheable
        ):
            expired_keys = [
                key for key, entry in self._cache.items() if entry.expires_at <= now
            ]
            for key in expired_keys:
                self._cache.pop(key, None)
            if len(self._cache) >= self._max_cache_entries:
                oldest_key = min(
                    self._cache, key=lambda key: self._cache[key].expires_at
                )
                self._cache.pop(oldest_key, None)
            self._cache[cache_key] = _CacheEntry(
                expires_at=now + self._cache_ttl_seconds,
                text=result,
            )
        return ZhihuDocument(result)

    async def _fetch_json(
        self,
        endpoint: str,
        *,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not any(pattern.fullmatch(endpoint) for pattern in _SAFE_API_ENDPOINTS):
            raise ZhihuRequestError("An unsafe Zhihu API endpoint was rejected.")
        request_url = f"{_API_ORIGIN}{endpoint}"

        try:
            async with self._client.stream(
                "GET",
                request_url,
                params=params,
                headers=self._headers if headers is None else headers,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise ZhihuRequestError(
                        "Zhihu returned an unexpected redirect; it was not followed."
                    )
                if response.status_code in {401, 403}:
                    raise _ZhihuAccessDeniedError(
                        "Zhihu denied access. Configure a valid Cookie and try again."
                    )
                if response.status_code == 404:
                    raise ZhihuRequestError(
                        "The requested Zhihu content was not found."
                    )
                if response.status_code == 429:
                    raise ZhihuRequestError(
                        "Zhihu is rate limiting requests. Please try again later."
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise ZhihuRequestError(
                        f"Zhihu returned HTTP {response.status_code}. "
                        "Please try again later."
                    )

                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > self._max_response_bytes:
                            raise ZhihuRequestError(
                                "Zhihu returned more data than the configured "
                                "safety limit."
                            )
                    except ValueError:
                        pass

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise ZhihuRequestError(
                            "Zhihu returned more data than the configured safety limit."
                        )
        except ZhihuReaderError:
            raise
        except httpx.TimeoutException as exc:
            raise ZhihuRequestError(
                "Reading Zhihu timed out. Please try again later."
            ) from exc
        except httpx.HTTPError as exc:
            raise ZhihuRequestError(
                "Could not connect to Zhihu. Please try again later."
            ) from exc

        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ZhihuRequestError(
                "Zhihu returned data that could not be parsed."
            ) from exc
        if not isinstance(payload, dict):
            raise ZhihuRequestError("Zhihu returned an unexpected JSON structure.")
        return payload

    async def _fetch_comment_json(
        self,
        endpoint: str,
        *,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Fetch a comment page, retrying public comments without a bad Cookie."""
        try:
            return await self._fetch_json(endpoint, params=params)
        except _ZhihuAccessDeniedError:
            has_client_cookies = any(True for _ in self._client.cookies.jar)
            if not self._headers.get("Cookie") and not has_client_cookies:
                raise
            anonymous_headers = dict(self._headers)
            anonymous_headers["Cookie"] = ""
            _LOGGER.info(
                "Zhihu comment request for %s was denied; retrying anonymously",
                endpoint,
            )
            return await self._fetch_json(
                endpoint,
                params=params,
                headers=anonymous_headers,
            )

    async def _fetch_authenticated_article_page(
        self, target: ZhihuTarget
    ) -> dict[str, Any]:
        """Read one fixed Zhuanlan page using an explicitly configured Cookie."""
        if not self._authenticated_article_fallback:
            raise ZhihuRequestError(
                "Authenticated article-page fallback is disabled."
            )
        if not self._configured_cookie:
            raise ZhihuRequestError(
                "Authenticated article-page fallback requires a complete browser "
                "Cookie."
            )

        request_url = f"{_ARTICLE_ORIGIN}/p/{target.resource_id}"
        headers = dict(self._headers)
        headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                "Referer": "https://zhuanlan.zhihu.com/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Upgrade-Insecure-Requests": "1",
            }
        )

        try:
            async with self._client.stream(
                "GET",
                request_url,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise ZhihuRequestError(
                        "Zhihu redirected the authenticated article page; the "
                        "redirect was not followed."
                    )
                if response.status_code == 401:
                    raise ZhihuRequestError(
                        "Zhihu rejected the configured login Cookie. Refresh the "
                        "complete Cookie from a browser that can open the article."
                    )
                if response.status_code == 403:
                    raise ZhihuRequestError(
                        "Zhihu blocked the authenticated article page. The account "
                        "may lack access, the Cookie may be incomplete, or browser "
                        "verification may be required."
                    )
                if response.status_code == 404:
                    raise ZhihuRequestError(
                        "The requested Zhihu article page was not found."
                    )
                if response.status_code == 429:
                    raise ZhihuRequestError(
                        "Zhihu is rate limiting article-page requests. Please try "
                        "again later."
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise ZhihuRequestError(
                        f"Zhihu returned HTTP {response.status_code} for the "
                        "article page."
                    )

                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > self._max_response_bytes:
                            raise ZhihuRequestError(
                                "Zhihu returned more article-page data than the "
                                "configured safety limit."
                            )
                    except ValueError:
                        pass

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise ZhihuRequestError(
                            "Zhihu returned more article-page data than the "
                            "configured safety limit."
                        )
        except ZhihuReaderError:
            raise
        except httpx.TimeoutException as exc:
            raise ZhihuRequestError(
                "Reading the authenticated Zhihu article page timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise ZhihuRequestError(
                "Could not connect to the authenticated Zhihu article page."
            ) from exc

        try:
            page_html = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ZhihuRequestError(
                "Zhihu returned an article page that could not be decoded."
            ) from exc
        return self._article_payload_from_html(page_html, target.resource_id)

    @staticmethod
    def _article_payload_from_html(
        page_html: str, article_id: int
    ) -> dict[str, Any]:
        parser = _ArticleInitialDataParser()
        try:
            parser.feed(page_html)
            parser.close()
        except Exception as exc:
            raise ZhihuRequestError(
                "Zhihu returned an article page that could not be parsed."
            ) from exc

        if parser.has_zse_challenge:
            raise ZhihuRequestError(
                "Zhihu requires interactive browser verification for this article; "
                "the plugin does not bypass that challenge."
            )

        for raw_script in parser.scripts:
            try:
                initial_data = json.loads(raw_script.strip())
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(initial_data, Mapping):
                continue
            state = initial_data.get("initialState")
            if not isinstance(state, Mapping):
                state = initial_data
            entities = state.get("entities")
            if not isinstance(entities, Mapping):
                continue
            articles = entities.get("articles")
            if not isinstance(articles, Mapping):
                continue
            article = articles.get(str(article_id))
            if isinstance(article, Mapping):
                return dict(article)

        raise ZhihuRequestError(
            "The authenticated Zhihu page did not contain readable article data."
        )

    @classmethod
    def _article_content_is_preview(
        cls,
        payload: Mapping[str, object],
        content: str,
    ) -> bool:
        """Conservatively distinguish full article text from gated previews."""
        access_containers: list[Mapping[str, object]] = [payload]
        for key in (
            "paid_info",
            "permission",
            "rights",
            "access_info",
            "purchase_info",
        ):
            value = payload.get(key)
            if isinstance(value, Mapping):
                access_containers.append(value)

        readability_fields = {
            "can_read",
            "content_available",
        }
        entitlement_fields = {
            "has_permission",
            "has_purchased",
            "has_rights",
            "is_authorized",
            "is_entitled",
            "is_purchased",
        }
        restricted_fields = {
            "content_limited",
            "is_limited",
            "is_locked",
            "is_paywalled",
            "is_preview",
            "only_excerpt",
            "requires_purchase",
        }
        paid_fields = {
            "is_member_only",
            "is_paid",
            "is_paying",
            "is_vip_content",
            "requires_payment",
        }

        readability_granted = False
        readability_denied = False
        entitlement_granted = False
        paid_content = False
        explicitly_free = False

        def flag_is(value: object, expected: bool) -> bool:
            if isinstance(value, bool):
                return value is expected
            if isinstance(value, (int, float)):
                return (value != 0) is expected
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"true", "1", "yes"}:
                    return expected
                if normalized in {"false", "0", "no"}:
                    return not expected
            return False

        for container in access_containers:
            for key in readability_fields:
                if key not in container:
                    continue
                value = container.get(key)
                if flag_is(value, False):
                    readability_denied = True
                if flag_is(value, True):
                    readability_granted = True
            for key in entitlement_fields:
                if key in container and flag_is(container.get(key), True):
                    entitlement_granted = True
            if any(flag_is(container.get(key), True) for key in restricted_fields):
                return True
            for key in paid_fields:
                if key not in container:
                    continue
                value = container.get(key)
                if flag_is(value, True):
                    paid_content = True
                elif key in {
                    "is_member_only",
                    "is_paid",
                    "is_vip_content",
                    "requires_payment",
                } and flag_is(value, False):
                    explicitly_free = True

        if len(content) <= 2_000:
            lowered = content.lower()
            preview_markers = (
                "查看完整内容",
                "阅读全文",
                "继续阅读",
                "开通盐选",
                "盐选会员",
                "购买后",
                "付费内容",
                "解锁全文",
                "subscribe to read",
                "unlock the full",
            )
            if any(marker in lowered for marker in preview_markers):
                return True

        # Direct readability is authoritative. Purchase history can grant access
        # only when Zhihu did not explicitly say the current content is unreadable.
        if readability_denied:
            return True
        if readability_granted or entitlement_granted:
            return False
        if explicitly_free and not paid_content:
            return False

        excerpt = html_to_text(payload.get("excerpt"))
        if excerpt:
            normalized_content = re.sub(r"\s+", " ", content).strip()
            normalized_excerpt = re.sub(r"\s+", " ", excerpt).strip()
            similar_prefix = (
                normalized_content.startswith(normalized_excerpt)
                or normalized_excerpt.startswith(normalized_content)
            )
            if normalized_content == normalized_excerpt or (
                similar_prefix
                and len(normalized_content)
                <= max(600, len(normalized_excerpt) * 2 + 100)
            ):
                return True

        return paid_content

    async def _read_answer(
        self, target: ZhihuTarget, comment_limit: int
    ) -> _RenderedDocument:
        payload = await self._fetch_json(
            f"/api/v4/answers/{target.resource_id}",
            params={
                "include": (
                    "content,excerpt,voteup_count,comment_count,created_time,"
                    "updated_time,author.name,question.title,question.id"
                )
            },
        )
        content = html_to_text(payload.get("content") or payload.get("excerpt"))
        if not content:
            raise ZhihuRequestError("The Zhihu answer did not contain readable text.")

        question = payload.get("question")
        question_data = question if isinstance(question, Mapping) else {}
        title = html_to_text(question_data.get("title")) or "Untitled question"
        author = self._author_name(payload.get("author"))
        reported_comments = self._integer(payload.get("comment_count"))
        comments = await self._safe_comments(
            "answer",
            target.resource_id,
            comment_limit,
            expected_count=reported_comments,
        )
        lines = [
            "[Zhihu answer]",
            f"Question: {title}",
            f"Author: {author}",
            f"Upvotes: {self._integer(payload.get('voteup_count'))}",
            f"Comment count: {reported_comments}",
            f"Created: {self._timestamp(payload.get('created_time'))}",
            f"Updated: {self._timestamp(payload.get('updated_time'))}",
            f"Source: {target.canonical_url}",
            "",
            "Answer:",
            content,
        ]
        return _RenderedDocument(
            self._compose_with_comments(lines, comments),
            comments_complete=comments.error is None,
        )

    async def _read_article(
        self, target: ZhihuTarget, comment_limit: int
    ) -> _RenderedDocument:
        payload: dict[str, Any] = {}
        api_error: ZhihuRequestError | None = None
        try:
            payload = await self._fetch_json(
                f"/api/v4/articles/{target.resource_id}",
                params={
                    "include": (
                        "content,excerpt,title,voteup_count,comment_count,created,"
                        "updated,author.name"
                    )
                },
            )
        except _ZhihuAccessDeniedError as exc:
            api_error = exc

        content = html_to_text(payload.get("content"))
        content_is_preview = bool(
            content and self._article_content_is_preview(payload, content)
        )
        if content and not content_is_preview:
            return await self._render_article(
                target,
                payload,
                comment_limit,
                content,
            )

        excerpt = html_to_text(payload.get("excerpt"))
        fallback_error: ZhihuRequestError | None = None
        page_payload: dict[str, Any] = {}
        page_content = ""
        if self._authenticated_article_fallback and self._configured_cookie:
            try:
                page_payload = await self._fetch_authenticated_article_page(target)
            except ZhihuRequestError as exc:
                fallback_error = exc
            else:
                page_content = html_to_text(page_payload.get("content"))
                page_is_preview = bool(
                    page_content
                    and self._article_content_is_preview(
                        page_payload,
                        page_content,
                    )
                )
                if page_content and not page_is_preview:
                    merged_payload = dict(payload)
                    merged_payload.update(page_payload)
                    return await self._render_article(
                        target,
                        merged_payload,
                        comment_limit,
                        page_content,
                    )
                if page_content:
                    fallback_error = ZhihuRequestError(
                        "The authenticated Zhihu page contained only an article "
                        "preview."
                    )
                else:
                    fallback_error = ZhihuRequestError(
                        "The authenticated Zhihu page did not contain readable "
                        "article text."
                    )

        preview_content = content or excerpt or page_content
        if preview_content:
            preview_payload = dict(payload)
            if page_content and not content and not excerpt:
                preview_payload.update(page_payload)
            access_note = (
                str(fallback_error)
                if fallback_error is not None
                else "Zhihu returned only a preview for this article."
            )
            return await self._render_article(
                target,
                preview_payload,
                comment_limit,
                preview_content,
                preview=True,
                access_note=access_note,
            )

        if fallback_error is not None:
            raise fallback_error
        if api_error is not None:
            if not self._configured_cookie:
                raise ZhihuRequestError(
                    "Zhihu denied this article request. If the article requires "
                    "an account, configure a complete browser Cookie."
                ) from api_error
            if not self._authenticated_article_fallback:
                raise ZhihuRequestError(
                    "Zhihu denied the article API. Enable authenticated article "
                    "fallback to try the regular page with the configured Cookie."
                ) from api_error
            raise api_error
        raise ZhihuRequestError(
            "The Zhihu article did not contain readable text."
        )

    async def _render_article(
        self,
        target: ZhihuTarget,
        payload: Mapping[str, object],
        comment_limit: int,
        content: str,
        *,
        preview: bool = False,
        access_note: str | None = None,
    ) -> _RenderedDocument:
        reported_comments = self._integer(payload.get("comment_count"))
        comments = await self._safe_comments(
            "article",
            target.resource_id,
            comment_limit,
            expected_count=reported_comments,
        )
        lines = [
            "[Zhihu article]",
            f"Title: {html_to_text(payload.get('title')) or 'Untitled article'}",
            f"Author: {self._author_name(payload.get('author'))}",
            f"Upvotes: {self._integer(payload.get('voteup_count'))}",
            f"Comment count: {reported_comments}",
            "Created: "
            f"{self._timestamp(payload.get('created') or payload.get('created_time'))}",
            "Updated: "
            f"{self._timestamp(payload.get('updated') or payload.get('updated_time'))}",
        ]
        if preview:
            lines.append("Access: Preview only; the full article was not available.")
        if access_note:
            lines.append(f"Access detail: {access_note}")
        lines.extend(
            [
                f"Source: {target.canonical_url}",
                "",
                "Article preview:" if preview else "Article:",
                content,
            ]
        )
        return _RenderedDocument(
            self._compose_with_comments(lines, comments),
            comments_complete=comments.error is None,
            cacheable=not preview,
        )

    async def _read_pin(
        self, target: ZhihuTarget, comment_limit: int
    ) -> _RenderedDocument:
        payload = await self._fetch_json(f"/api/v4/pins/{target.resource_id}")
        if payload.get("is_deleted") is True:
            reason = html_to_text(payload.get("deleted_reason"))
            raise ZhihuRequestError(reason or "The Zhihu thought was deleted.")

        content = self._pin_content(payload)
        if not content:
            raise ZhihuRequestError(
                "The Zhihu thought did not contain readable content."
            )

        reported_comments = self._integer(payload.get("comment_count"))
        comments = await self._safe_comments(
            "pin",
            target.resource_id,
            comment_limit,
            expected_count=reported_comments,
        )
        lines = [
            "[Zhihu thought]",
            f"Author: {self._author_name(payload.get('author'))}",
            f"Likes: {self._integer(payload.get('like_count'))}",
            f"Reactions: {self._integer(payload.get('reaction_count'))}",
            f"Reposts: {self._integer(payload.get('repin_count'))}",
            f"Comment count: {reported_comments}",
        ]
        if payload.get("page_view_count") is not None:
            lines.append(f"Views: {self._integer(payload.get('page_view_count'))}")
        lines.extend(
            [
                f"Created: {self._timestamp(payload.get('created'))}",
                f"Updated: {self._timestamp(payload.get('updated'))}",
            ]
        )
        source_pin_id = self._positive_id(payload.get("source_pin_id"))
        if source_pin_id is not None:
            lines.append(
                f"Repin source: https://www.zhihu.com/pin/{source_pin_id}"
            )
        lines.extend(
            [
                f"Source: {target.canonical_url}",
                "",
                "Thought:",
                content,
            ]
        )
        return _RenderedDocument(
            self._compose_with_comments(lines, comments),
            comments_complete=comments.error is None,
        )

    @classmethod
    def _pin_content(cls, payload: Mapping[str, object]) -> str:
        """Render structured pin blocks without serializing raw API objects."""
        raw_blocks = payload.get("content")
        rendered_blocks: list[str] = []
        if isinstance(raw_blocks, list):
            for raw_block in raw_blocks:
                if not isinstance(raw_block, Mapping):
                    continue
                block_type = str(raw_block.get("type") or "").strip().lower()
                if block_type == "text":
                    title = html_to_text(raw_block.get("title"))
                    body = html_to_text(
                        raw_block.get("content") or raw_block.get("own_text")
                    )
                    text_parts = [part for part in (title, body) if part]
                    if text_parts:
                        rendered_blocks.append("\n".join(text_parts))
                elif block_type == "image":
                    label = html_to_text(
                        raw_block.get("title") or raw_block.get("alt")
                    )
                    width = cls._integer(raw_block.get("width"))
                    height = cls._integer(raw_block.get("height"))
                    details = [part for part in (label,) if part]
                    if width and height:
                        details.append(f"{width}x{height}")
                    rendered_blocks.append(
                        f"[Image: {', '.join(details)}]" if details else "[Image]"
                    )
                elif block_type in {"link", "link_card"}:
                    title = html_to_text(
                        raw_block.get("data_draft_title")
                        or raw_block.get("title")
                    )
                    target = str(
                        raw_block.get("url")
                        or raw_block.get("original_url")
                        or raw_block.get("data_draft_url")
                        or ""
                    ).strip()
                    label = "Link card" if block_type == "link_card" else "Link"
                    details = [title] if title else []
                    if target and target != title:
                        details.append(target)
                    rendered_blocks.append(
                        f"[{label}: {' - '.join(details)}]"
                        if details
                        else f"[{label}]"
                    )
                elif block_type == "video":
                    title = html_to_text(raw_block.get("title"))
                    duration = cls._integer(raw_block.get("duration"))
                    details = [part for part in (title,) if part]
                    if duration:
                        details.append(f"{duration} seconds")
                    rendered_blocks.append(
                        f"[Video: {', '.join(details)}]" if details else "[Video]"
                    )
                else:
                    fallback = html_to_text(
                        raw_block.get("content")
                        or raw_block.get("title")
                        or raw_block.get("description")
                    )
                    if fallback:
                        rendered_blocks.append(fallback)

        structured = "\n\n".join(rendered_blocks).strip()
        if structured:
            return structured
        return html_to_text(
            payload.get("content_html") or payload.get("excerpt_title")
        )

    async def _read_question(
        self, target: ZhihuTarget, comment_limit: int
    ) -> _RenderedDocument:
        payload = await self._fetch_json(
            f"/api/v4/questions/{target.resource_id}",
            params={
                "include": (
                    "title,detail,excerpt,answer_count,comment_count,follower_count"
                )
            },
        )
        answers_payload = await self._fetch_json(
            f"/api/v4/questions/{target.resource_id}/answers",
            params={
                "include": (
                    "content,excerpt,voteup_count,comment_count,created_time,"
                    "updated_time,author.name"
                ),
                "limit": min(20, max(10, self._max_question_answers * 3)),
                "offset": 0,
                "platform": "desktop",
                "sort_by": "default",
            },
        )
        raw_answers = answers_payload.get("data")
        if not isinstance(raw_answers, list):
            raise ZhihuRequestError("Zhihu returned an invalid answer list.")
        answers = [answer for answer in raw_answers if isinstance(answer, Mapping)]
        answers.sort(
            key=lambda answer: self._integer(answer.get("voteup_count")), reverse=True
        )
        answers = answers[: self._max_question_answers]

        description = html_to_text(payload.get("detail") or payload.get("excerpt"))
        reported_question_comments = self._integer(payload.get("comment_count"))
        lines = [
            "[Zhihu question]",
            f"Question: {html_to_text(payload.get('title')) or 'Untitled question'}",
            f"Answers: {self._integer(payload.get('answer_count'))}",
            f"Followers: {self._integer(payload.get('follower_count'))}",
            f"Comment count: {reported_question_comments}",
            f"Source: {target.canonical_url}",
        ]
        if description:
            lines.extend(["", "Description:", description])

        comment_targets: list[tuple[str, str, int, int]] = []
        comment_lines: list[str] = []
        comments_complete = True

        lines.extend(["", f"Top answers captured: {len(answers)}"])
        for index, answer in enumerate(answers, start=1):
            body = html_to_text(answer.get("content") or answer.get("excerpt"))
            if not body:
                body = "[No readable answer text]"
            answer_comment_count = self._integer(answer.get("comment_count"))
            lines.extend(
                [
                    "",
                    f"Answer {index}",
                    f"Author: {self._author_name(answer.get('author'))}",
                    f"Upvotes: {self._integer(answer.get('voteup_count'))}",
                    f"Comments: {answer_comment_count}",
                    body,
                ]
            )
            answer_id = self._positive_id(answer.get("id"))
            if comment_limit > 0:
                if answer_id is None:
                    comments_complete = False
                    comment_lines.extend(
                        [
                            "",
                            f"Answer {index} comments unavailable: "
                            "Zhihu omitted the answer ID.",
                        ]
                    )
                else:
                    comment_targets.append(
                        (
                            f"Answer {index}",
                            "answer",
                            answer_id,
                            answer_comment_count,
                        )
                    )

        if comment_limit > 0:
            comment_targets.append(
                (
                    "Question",
                    "question",
                    target.resource_id,
                    reported_question_comments,
                )
            )

        remaining = comment_limit
        for target_index, (label, kind, resource_id, expected_count) in enumerate(
            comment_targets
        ):
            if remaining <= 0:
                break
            targets_left = len(comment_targets) - target_index
            quota = max(1, remaining // targets_left)
            result = await self._safe_comments(
                kind,
                resource_id,
                quota,
                expected_count=expected_count,
            )
            if result.comments:
                self._append_comments(
                    comment_lines,
                    result.comments,
                    heading=f"{label} comments captured",
                )
            if result.error:
                comments_complete = False
                availability = (
                    "partially unavailable" if result.comments else "unavailable"
                )
                comment_lines.extend(
                    ["", f"{label} comments {availability}: {result.error}"]
                )
            remaining = max(0, remaining - result.captured_count)

        return _RenderedDocument(
            self._compose_with_comment_lines(lines, comment_lines),
            comments_complete=comments_complete,
        )

    async def _safe_comments(
        self,
        kind: str,
        resource_id: int,
        comment_limit: int,
        *,
        expected_count: int = 0,
    ) -> _CommentsResult:
        if comment_limit == 0:
            return _CommentsResult()
        try:
            result = await self._read_comments(
                kind,
                resource_id,
                comment_limit,
                expected_count=expected_count,
            )
        except ZhihuReaderError as exc:
            message = str(exc)
            _LOGGER.warning(
                "Zhihu %s comments unavailable for %s: %s",
                kind,
                resource_id,
                message,
            )
            return _CommentsResult(error=message)

        if result.error:
            _LOGGER.warning(
                "Zhihu %s comments were only partially available for %s: %s",
                kind,
                resource_id,
                result.error,
            )
        if expected_count > 0 and not result.comments and not result.error:
            message = (
                f"Zhihu reports {expected_count} comments but returned no readable "
                "comments."
            )
            _LOGGER.warning(
                "Zhihu %s comments unavailable for %s: %s",
                kind,
                resource_id,
                message,
            )
            return _CommentsResult(error=message)
        return result

    async def _read_comments(
        self,
        kind: str,
        resource_id: int,
        comment_limit: int,
        *,
        expected_count: int = 0,
    ) -> _CommentsResult:
        plural = {
            "answer": "answers",
            "article": "articles",
            "pin": "pins",
            "question": "questions",
        }.get(kind)
        if plural is None:
            raise ZhihuRequestError("Unsupported comment resource type.")

        endpoint = f"/api/v4/comment_v5/{plural}/{resource_id}/root_comment"
        comments: list[_Comment] = []
        errors: list[str] = []
        seen_comment_ids: set[int] = set()
        captured_count = 0
        reported_count = expected_count
        offset = ""
        seen_offsets = {offset}
        order_by = "score"
        for page_index in range(self._max_comment_pages):
            page_limit = min(20, max(1, comment_limit - captured_count))
            try:
                payload = await self._fetch_comment_json(
                    endpoint,
                    params={
                        "limit": page_limit,
                        "offset": offset,
                        "order_by": order_by,
                    },
                )
            except ZhihuReaderError as exc:
                if not comments:
                    raise
                errors.append(str(exc))
                break
            reported_count = max(
                reported_count,
                self._comment_total_count(payload),
            )
            data = payload.get("data")
            if not isinstance(data, list):
                errors.append("Zhihu returned an invalid comment list.")
                break
            if page_index == 0 and not data:
                order_by = "ts"
                try:
                    payload = await self._fetch_comment_json(
                        endpoint,
                        params={
                            "limit": page_limit,
                            "offset": offset,
                            "order_by": order_by,
                        },
                    )
                except ZhihuReaderError as exc:
                    if not comments:
                        raise
                    errors.append(str(exc))
                    break
                reported_count = max(
                    reported_count,
                    self._comment_total_count(payload),
                )
                data = payload.get("data")
                if not isinstance(data, list):
                    errors.append("Zhihu returned an invalid comment list.")
                    break
            if not data:
                break

            for raw_comment in data:
                if captured_count >= comment_limit:
                    break
                if not isinstance(raw_comment, Mapping):
                    continue
                root_comment_id = self._positive_id(raw_comment.get("id"))
                if (
                    root_comment_id is not None
                    and root_comment_id in seen_comment_ids
                ):
                    continue
                text = html_to_text(raw_comment.get("content"))
                if not text:
                    continue
                if root_comment_id is not None:
                    seen_comment_ids.add(root_comment_id)

                captured_count += 1
                children: list[_Comment] = []
                seen_child_ids: set[int] = set()
                raw_children: object = raw_comment.get("child_comments", [])
                if isinstance(raw_children, Mapping):
                    raw_children = raw_children.get("data", [])
                if isinstance(raw_children, list):
                    for raw_child in raw_children[: self._max_child_comments]:
                        if captured_count >= comment_limit:
                            break
                        if not isinstance(raw_child, Mapping):
                            continue
                        child_text = html_to_text(raw_child.get("content"))
                        if not child_text:
                            continue
                        children.append(
                            self._comment_from_mapping(raw_child, child_text)
                        )
                        child_id = self._positive_id(raw_child.get("id"))
                        if child_id is not None:
                            seen_child_ids.add(child_id)
                        captured_count += 1

                declared_children = self._integer(
                    raw_comment.get("child_comment_count")
                )
                remaining_children = min(
                    self._max_child_comments - len(children),
                    comment_limit - captured_count,
                )
                if (
                    root_comment_id is not None
                    and declared_children > len(children)
                    and remaining_children > 0
                ):
                    initial_child_offset = self._normalize_comment_offset(
                        raw_comment.get("child_comment_next_offset")
                    ) or ""
                    child_result = await self._read_child_comments(
                        root_comment_id,
                        remaining_children,
                        seen_child_ids,
                        initial_offset=initial_child_offset,
                    )
                    children.extend(child_result.comments)
                    captured_count += len(child_result.comments)
                    if child_result.error:
                        errors.append(child_result.error)

                comments.append(
                    _Comment(
                        author=self._comment_author_name(raw_comment),
                        text=text,
                        votes=self._integer(
                            raw_comment.get("vote_count")
                            if raw_comment.get("vote_count") is not None
                            else raw_comment.get("like_count")
                        ),
                        created_at=self._timestamp(raw_comment.get("created_time")),
                        children=tuple(children),
                    )
                )

            paging = payload.get("paging")
            if isinstance(paging, Mapping) and paging.get("is_end") is True:
                break
            if captured_count >= comment_limit:
                break
            next_offset = self._next_comment_offset(
                paging,
                endpoint=endpoint,
            )
            if next_offset is None:
                errors.append(
                    "Zhihu comment pagination did not provide a safe next cursor."
                )
                break
            if next_offset in seen_offsets:
                errors.append("Zhihu comment pagination repeated a cursor.")
                break
            seen_offsets.add(next_offset)
            offset = next_offset
        if reported_count > 0 and not comments and not errors:
            errors.append(
                f"Zhihu reports {reported_count} comments but returned no readable "
                "comments."
            )
        error = "; ".join(dict.fromkeys(errors)) if errors else None
        return _CommentsResult(tuple(comments), error=error)

    async def _read_child_comments(
        self,
        root_comment_id: int,
        child_limit: int,
        seen_ids: set[int],
        *,
        initial_offset: str = "",
    ) -> _CommentsResult:
        """Fetch replies omitted from a comment_v5 root-comment response."""
        endpoint = (
            f"/api/v4/comment_v5/comment/{root_comment_id}/child_comment"
        )
        children: list[_Comment] = []
        errors: list[str] = []
        offset = initial_offset
        seen_offsets = {offset}
        for _ in range(self._max_comment_pages):
            page_limit = min(
                20,
                max(1, child_limit - len(children) + len(seen_ids)),
            )
            try:
                payload = await self._fetch_comment_json(
                    endpoint,
                    params={"limit": page_limit, "offset": offset},
                )
            except ZhihuReaderError as exc:
                errors.append(str(exc))
                break
            data = payload.get("data")
            if not isinstance(data, list):
                errors.append("Zhihu returned an invalid child comment list.")
                break
            if not data:
                break

            for raw_child in data:
                if len(children) >= child_limit:
                    break
                if not isinstance(raw_child, Mapping):
                    continue
                child_id = self._positive_id(raw_child.get("id"))
                if child_id is not None and child_id in seen_ids:
                    continue
                child_text = html_to_text(raw_child.get("content"))
                if not child_text:
                    continue
                children.append(
                    self._comment_from_mapping(raw_child, child_text)
                )
                if child_id is not None:
                    seen_ids.add(child_id)

            paging = payload.get("paging")
            if isinstance(paging, Mapping) and paging.get("is_end") is True:
                break
            if len(children) >= child_limit:
                break
            next_offset = self._next_comment_offset(
                paging,
                endpoint=endpoint,
            )
            if next_offset is None:
                errors.append(
                    "Zhihu child-comment pagination did not provide a safe next cursor."
                )
                break
            if next_offset in seen_offsets:
                errors.append("Zhihu child-comment pagination repeated a cursor.")
                break
            seen_offsets.add(next_offset)
            offset = next_offset
        error = "; ".join(dict.fromkeys(errors)) if errors else None
        return _CommentsResult(tuple(children), error=error)

    @staticmethod
    def _next_comment_offset(
        paging: object,
        *,
        endpoint: str,
    ) -> str | None:
        """Extract the opaque comment cursor without following response URLs."""
        if not isinstance(paging, Mapping) or paging.get("is_end") is True:
            return None

        next_url = paging.get("next")
        if isinstance(next_url, str) and next_url:
            try:
                parsed = urlsplit(next_url)
                host = (parsed.hostname or "").lower().rstrip(".")
                is_relative = not parsed.scheme and not parsed.netloc
                if not is_relative and (
                    parsed.scheme.lower() not in {"http", "https"}
                    or host not in _SUPPORTED_HOSTS
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.port is not None
                ):
                    return None
                if parsed.path != endpoint:
                    return None
                offsets = parse_qs(
                    parsed.query,
                    keep_blank_values=True,
                ).get("offset")
                if not offsets:
                    return None
                return ZhihuReader._normalize_comment_offset(offsets[0])
            except ValueError:
                return None

        return None

    @staticmethod
    def _normalize_comment_offset(value: object) -> str | None:
        offset = str(value).strip() if value is not None else ""
        if (
            not offset
            or len(offset) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in offset)
        ):
            return None
        return offset

    def _comment_from_mapping(self, value: Mapping[str, object], text: str) -> _Comment:
        return _Comment(
            author=self._comment_author_name(value),
            text=text,
            votes=self._integer(
                value.get("vote_count")
                if value.get("vote_count") is not None
                else value.get("like_count")
            ),
            created_at=self._timestamp(value.get("created_time")),
        )

    @classmethod
    def _comment_author_name(cls, value: Mapping[str, object]) -> str:
        """Read author data from both observed comment_v5 response shapes."""
        author = value.get("author")
        if author is not None:
            name = cls._author_name(author)
            if name != "Anonymous":
                return name
        return cls._author_name(value.get("member"))

    @staticmethod
    def _positive_id(value: object) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if 0 < parsed < 10**30 else None

    @classmethod
    def _comment_total_count(cls, payload: Mapping[str, object]) -> int:
        counts = payload.get("counts")
        count_value = (
            counts.get("total_counts") if isinstance(counts, Mapping) else None
        )
        paging = payload.get("paging")
        paging_value = paging.get("totals") if isinstance(paging, Mapping) else None
        return max(cls._integer(count_value), cls._integer(paging_value))

    @staticmethod
    def _author_name(value: object) -> str:
        if isinstance(value, Mapping):
            member = value.get("member")
            if isinstance(member, Mapping):
                name = html_to_text(member.get("name"))
                if name:
                    return name
            name = html_to_text(value.get("name"))
            if name:
                return name
        return "Anonymous"

    @staticmethod
    def _integer(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _timestamp(value: object) -> str:
        try:
            timestamp = float(value)
            if timestamp <= 0:
                return "Unknown"
            return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        except (TypeError, ValueError, OverflowError, OSError):
            return "Unknown"

    @staticmethod
    def _append_comments(
        lines: list[str],
        comments: Sequence[_Comment],
        *,
        heading: str = "Comments captured",
    ) -> None:
        if not comments:
            return
        captured = sum(1 + len(comment.children) for comment in comments)
        lines.extend(["", f"{heading}: {captured}"])
        for index, comment in enumerate(comments, start=1):
            text = comment.text.replace("\n", " ")
            lines.append(
                f"{index}. {comment.author} | {comment.votes} likes | "
                f"{comment.created_at}: {text}"
            )
            for child in comment.children:
                child_text = child.text.replace("\n", " ")
                lines.append(
                    f"   Reply - {child.author} | {child.votes} likes | "
                    f"{child.created_at}: {child_text}"
                )

    def _compose_with_comments(
        self,
        primary_lines: list[str],
        comments: _CommentsResult,
    ) -> str:
        """Keep representative comments visible when the primary text is long."""
        comment_lines: list[str] = []
        self._append_comments(comment_lines, comments.comments)
        if comments.error:
            availability = (
                "partially unavailable" if comments.comments else "unavailable"
            )
            comment_lines.extend(
                ["", f"Comments {availability}: {comments.error}"]
            )
        return self._compose_with_comment_lines(primary_lines, comment_lines)

    def _compose_with_comment_lines(
        self,
        primary_lines: list[str],
        comment_lines: Sequence[str],
    ) -> str:
        """Reserve output space for rendered comments and their diagnostics."""
        primary = "\n".join(primary_lines).strip()
        if not comment_lines:
            return primary

        full_comments = "\n".join(comment_lines).strip()
        separator = "\n\n"
        if (
            len(primary) + len(separator) + len(full_comments)
            <= self._max_output_chars
        ):
            return primary + separator + full_comments

        comment_budget = max(1, self._max_output_chars * 35 // 100)
        rendered_comments = self._truncate_section(
            full_comments,
            comment_budget,
            "\n[Comments truncated.]",
        )
        primary_budget = max(
            0,
            self._max_output_chars
            - len(separator)
            - len(rendered_comments),
        )
        rendered_primary = self._truncate_section(
            primary,
            primary_budget,
            "\n[Content truncated.]",
        )

        unused = (
            self._max_output_chars
            - len(rendered_primary)
            - len(separator)
            - len(rendered_comments)
        )
        if unused > 0 and len(rendered_comments) < len(full_comments):
            rendered_comments = self._truncate_section(
                full_comments,
                len(rendered_comments) + unused,
                "\n[Comments truncated.]",
            )

        return separator.join(
            part for part in (rendered_primary, rendered_comments) if part
        )

    @staticmethod
    def _truncate_section(text: str, limit: int, marker: str) -> str:
        if limit <= 0:
            return ""
        if len(text) <= limit:
            return text
        if limit <= len(marker):
            return text[:limit]
        return text[: limit - len(marker)].rstrip() + marker

    def _truncate(self, text: str) -> str:
        text = text.strip()
        if len(text) <= self._max_output_chars:
            return text
        marker = "\n\n[Output truncated at the configured character limit.]"
        if self._max_output_chars <= len(marker):
            return marker[: self._max_output_chars]
        return text[: self._max_output_chars - len(marker)].rstrip() + marker


__all__ = [
    "InvalidZhihuUrlError",
    "ZhihuReader",
    "ZhihuReaderError",
    "ZhihuRequestError",
    "ZhihuDocument",
    "ZhihuTarget",
    "extract_zhihu_urls",
    "html_to_text",
    "parse_url",
    "parse_zhihu_url",
]
