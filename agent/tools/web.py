"""Host-side tools for the DeepSeek Search and Fetch JSON APIs."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import Field


DEEPSEEK_TOOLS_URL = "https://api.deepseek.com/v1/tools"
WEB_TIMEOUT_SECONDS = 20

_HTTP_ERRORS = {
    400: "invalid request",
    401: "invalid API key",
    402: "insufficient DeepSeek balance",
    403: "request blocked by DeepSeek safety policy",
    404: "no usable page content available",
    408: "request timed out",
    413: "request body too large",
    429: "rate limit exceeded",
    504: "upstream request timed out",
}


def _post(
    operation: str, payload: dict[str, object], *, api_key: str, trust_env: bool,
) -> dict[str, Any]:
    label = f"DeepSeek {operation}"
    try:
        with httpx.Client(timeout=WEB_TIMEOUT_SECONDS, follow_redirects=False, trust_env=trust_env) as client:
            response = client.post(
                f"{DEEPSEEK_TOOLS_URL}/{operation}",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException:
        raise ToolException(f"{label} timed out after {WEB_TIMEOUT_SECONDS}s.") from None
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        reason = _HTTP_ERRORS.get(status, "unexpected response")
        raise ToolException(f"{label} failed: {reason} (HTTP {status}).") from None
    except httpx.RequestError:
        raise ToolException(f"{label} failed: network error.") from None
    except ValueError:
        raise ToolException(f"{label} failed: invalid response format.") from None
    if not isinstance(data, dict):
        raise ToolException(f"{label} failed: invalid response format.")
    return data


def build_web_search_tool(api_key: str, *, trust_env: bool = False) -> BaseTool:
    def web_search(
        query: Annotated[str, Field(min_length=1, max_length=128, description="Search query. Operators such as site:, lang:, and after: are allowed.")],
        count: Annotated[int, Field(strict=True, ge=1, le=50, description="Maximum number of results.")] = 10,
        ori_question: Annotated[str | None, Field(min_length=1, max_length=128, description="The user question that triggered the search. Optional.")] = None,
        localization: Annotated[str, Field(description="IP address used to prefer localized results. Optional.")] = "",
        need_content: Annotated[bool, Field(strict=True, description="When true, also return usable page text with the search results.")] = False,
    ) -> tuple[str, dict[str, object]]:
        """Use web_search to discover relevant information and sources.

        Verify publication dates when answering time-sensitive questions.
        Use web_fetch when the webpage address is already known.
        """
        if not query.strip():
            raise ToolException("Web search query must not be blank.")
        payload: dict[str, object] = {
            "query": query, "count": count, "localization": localization, "need_content": need_content,
        }
        if ori_question is not None:
            payload["ori_question"] = ori_question
        data = _post("search", payload, api_key=api_key, trust_env=trust_env)
        results = data.get("results")
        if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
            raise ToolException("DeepSeek search failed: invalid response format.")
        artifact = {"provider": "deepseek", "query": query, "results": results}
        if not results:
            return f"No web results found for: {query}", artifact

        lines = [f"Web search results for: {query}", f"{len(results)} results"]
        for index, result in enumerate(results, 1):
            lines.extend((
                "",
                f"{index}. {result.get('title') or 'Untitled result'}",
                f"   URL: {result.get('url', '')}",
            ))
            if result.get("date"):
                lines.append(f"   Published: {result['date']}")
            summary = result.get("summary") or result.get("snippet")
            if summary:
                lines.append(f"   {summary}")
            if need_content and result.get("content"):
                lines.extend(("", str(result["content"])))
        return "\n".join(lines), artifact

    return StructuredTool.from_function(
        func=web_search,
        response_format="content_and_artifact",
        handle_tool_error=True,
        handle_validation_error=True,
    )


def build_web_fetch_tool(api_key: str, *, trust_env: bool = False) -> BaseTool:
    def web_fetch(
        url: Annotated[str, Field(min_length=1, max_length=8192, description="HTTP or HTTPS webpage to retrieve.")],
    ) -> tuple[str, dict[str, object]]:
        """Use web_fetch to retrieve a known webpage as Markdown, including its title."""
        try:
            parsed = urlsplit(url)
            valid_url = (
                parsed.scheme in {"http", "https"} and parsed.hostname
                and parsed.username is None and parsed.password is None
                and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
            )
            # Accessing port validates the authority's optional port.
            _ = parsed.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ToolException("Web fetch requires an HTTP(S) URL with a host and without credentials.")

        data = _post("fetch", {"url": url}, api_key=api_key, trust_env=trust_env)
        title, content = data.get("title"), data.get("content")
        if not isinstance(title, str) or not isinstance(content, str):
            raise ToolException("DeepSeek fetch failed: invalid response format.")
        heading = f"{title}\n{url}" if title else url
        return f"{heading}\n\n{content}", {"provider": "deepseek", "url": url, "title": title}

    return StructuredTool.from_function(
        func=web_fetch,
        response_format="content_and_artifact",
        handle_tool_error=True,
        handle_validation_error=True,
    )
