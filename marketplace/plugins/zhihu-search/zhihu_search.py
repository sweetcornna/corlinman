#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://developer.zhihu.com"
DEFAULT_TIMEOUT_SECONDS = 5


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def success(request_id: Any, result: Any) -> None:
    emit({"jsonrpc": "2.0", "id": request_id, "result": result})


def failure(request_id: Any, message: str, code: int = -32000) -> None:
    emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def parse_query(params: dict[str, Any]) -> str:
    raw_query = (
        params.get("query")
        or params.get("q")
        or params.get("text")
        or params.get("Query")
        or ""
    )
    if not isinstance(raw_query, str) or not raw_query.strip():
        raise ValueError("Missing required argument: query.")
    return raw_query.strip()


def parse_count(params: dict[str, Any], *, max_count: int) -> int:
    raw_count = params.get("count", params.get("max_results", 10))
    try:
      count = int(raw_count)
    except (TypeError, ValueError):
      count = 10
    return max(1, min(max_count, count))


def get_timeout_seconds() -> int:
    raw_timeout = os.getenv("ZHIHU_SEARCH_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = int(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS
    return max(1, min(60, timeout))


def get_endpoint(search_type: str) -> str:
    env_name = (
        "ZHIHU_GLOBAL_SEARCH_URL"
        if search_type == "global_search"
        else "ZHIHU_ZHIHU_SEARCH_URL"
    )
    path = (
        "/api/v1/content/global_search"
        if search_type == "global_search"
        else "/api/v1/content/zhihu_search"
    )

    explicit = os.getenv(env_name, "").strip()
    if explicit:
        return explicit

    base_url = os.getenv("ZHIHU_OPENAPI_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    return f"{base_url.rstrip('/')}{path}"


def request_zhihu(query: str, count: int, search_type: str) -> dict[str, Any]:
    secret = os.getenv("ZHIHU_ACCESS_SECRET", "").strip()
    if not secret:
        raise ValueError("Set ZHIHU_ACCESS_SECRET first.")

    params = urlencode({"Query": query, "Count": str(count)})
    url = f"{get_endpoint(search_type)}?{params}"
    req = Request(
        url=url,
        method="GET",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Request-Timestamp": str(int(time.time())),
        },
    )

    try:
        with urlopen(req, timeout=get_timeout_seconds()) as response:
            body_text = response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        body_text = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body_text[:1000]}") from error
    except (TimeoutError, URLError) as error:
        raise RuntimeError(f"HTTP request failed (timeout or network error): {error}") from error

    try:
        api_response = json.loads(body_text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Non-JSON response from API: {body_text[:1000]}") from error

    if not isinstance(api_response, dict):
        raise RuntimeError("Invalid JSON response from API.")
    return api_response


def build_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rank": index,
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("summary", ""),
            "author_name": item.get("author_name", ""),
        }
        for index, item in enumerate(items, start=1)
    ]


def build_markdown_result(result: dict[str, Any]) -> str:
    search_type = result.get("search_type", "zhihu_search")
    items = result.get("items", [])
    lines = [
        "## Zhihu Site Search Results" if search_type == "zhihu_search" else "## Zhihu Global Search Results",
        "",
        f"Status: {result.get('api_message', '')}",
        f"Result count: {result.get('item_count', 0)}",
        "",
    ]

    if not items:
        lines.append("No results found.")
        return "\n".join(lines)

    for index, item in enumerate(items, start=1):
        title = item.get("title", "") or "Untitled"
        url = item.get("url", "")
        author = item.get("author_name", "")
        summary = item.get("summary", "")
        lines.append(f"### {index}. {title}")
        if url:
            lines.append(f"- URL: {url}")
        if author:
            lines.append(f"- Author: {author}")
        if "vote_up_count" in item:
            lines.append(
                f"- Votes / Comments: {item.get('vote_up_count', 0)} / {item.get('comment_count', 0)}"
            )
        if item.get("edit_time"):
            lines.append(f"- Edit time: {item.get('edit_time')}")
        if summary:
            lines.append(f"- Summary: {summary}")
        lines.append("")
    return "\n".join(lines).strip()


def normalize_result(api_response: dict[str, Any], search_type: str) -> dict[str, Any]:
    data = api_response.get("Data") if isinstance(api_response.get("Data"), dict) else {}
    items = data.get("Items") if isinstance(data.get("Items"), list) else []

    normalized_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = {
            "title": item.get("Title", ""),
            "url": item.get("Url", ""),
            "author_name": item.get("AuthorName", ""),
            "summary": item.get("ContentText", ""),
            "edit_time": item.get("EditTime", 0),
        }
        if search_type == "zhihu_search":
            normalized["vote_up_count"] = item.get("VoteUpCount", 0)
            normalized["comment_count"] = item.get("CommentCount", 0)
        normalized_items.append(normalized)

    result = {
        "search_type": search_type,
        "code": api_response.get("Code", -1),
        "api_message": api_response.get("Message", ""),
        "item_count": len(normalized_items),
        "items": normalized_items,
    }
    result["sources"] = build_sources(normalized_items)
    result["content"] = build_markdown_result(result)
    result["message"] = result["content"]
    return result


def route(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "zhihu_site_search":
        query = parse_query(params)
        count = parse_count(params, max_count=10)
        api_response = request_zhihu(query, count, "zhihu_search")
        return normalize_result(api_response, "zhihu_search")
    if method == "zhihu_global_search":
        query = parse_query(params)
        count = parse_count(params, max_count=20)
        api_response = request_zhihu(query, count, "global_search")
        return normalize_result(api_response, "global_search")
    raise ValueError(f"Unknown tool: {method}")


def main() -> None:
    raw = sys.stdin.read().lstrip("\ufeff")
    try:
        request = json.loads(raw)
    except json.JSONDecodeError:
        failure(None, "Invalid JSON-RPC request.")
        return

    request_id = request.get("id")
    try:
        method = request.get("method")
        if not isinstance(method, str) or not method.strip():
            raise ValueError("Missing JSON-RPC method.")
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object.")
        result = route(method, params)
        success(request_id, result)
    except Exception as error:
        failure(request_id, str(error))


if __name__ == "__main__":
    main()
