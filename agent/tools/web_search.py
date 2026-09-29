"""Host-side public web search through Tavily's fixed Search endpoint."""
from __future__ import annotations

from typing import Annotated, Literal

import httpx
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import Field


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT_SECONDS = 20


def build_web_search_tool(api_key: str) -> BaseTool:
    """Bind a Tavily credential without exposing it in the model's tool schema."""
    if not api_key.strip():
        raise ValueError("Tavily API key must not be empty")

    def web_search(
        query: str,
        max_results: Annotated[int, Field(ge=1, le=20)] = 5,
        topic: Literal["general", "news"] = "general",
        time_range: Literal["day", "week", "month", "year"] | None = None,
        include_domains: list[str] | None = None,
    ) -> tuple[str, dict[str, object]]:
        """Search the public web for current or external information.

        Use web_search when:
        - the user explicitly asks to search, browse, look up, or verify something;
        - information may have changed recently, such as documentation, releases,
          versions, APIs, products, prices, or news;
        - you are uncertain about a factual claim or need authoritative sources;
        - the answer depends on information outside the local workspace.

        Do not use web_search for local files or code. Use read_file, grep, or glob instead.

        Prefer specific queries. Use include_domains when an official or particular
        source is required. Use topic="news" and a time range for recent events.

        Search results are ranked snippets, not exhaustive results or full page content.
        Do not assume absence from the returned results means the information does not exist.
        """
        query = query.strip()
        if not query:
            raise ToolException("Web search query must not be empty. Try a specific search phrase.")
        if include_domains is not None and any(
            not isinstance(domain, str) or not domain.strip() for domain in include_domains
        ):
            raise ToolException("include_domains must contain non-empty domain names.")
        payload: dict[str, object] = {
            "query": query,
            "max_results": max_results,
            "topic": topic,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_usage": True,
        }
        if time_range is not None:
            payload["time_range"] = time_range
        if include_domains:
            payload["include_domains"] = [domain.strip() for domain in include_domains]
        try:
            with httpx.Client(timeout=TAVILY_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
                response = client.post(
                    TAVILY_SEARCH_URL,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise ToolException("Tavily search timed out after 20s. Try a narrower query.") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = {
                400: "invalid search request",
                401: "invalid API key",
                403: "access denied",
                422: "invalid search parameters",
                429: "rate limit exceeded",
                432: "search credits exhausted",
                433: "search credits exhausted",
            }.get(status, f"HTTP {status}")
            raise ToolException(f"Tavily search failed: {reason} (HTTP {status}).") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise ToolException("Tavily search failed: network or response error. Try again later.") from exc

        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ToolException("Tavily search failed: invalid response format.")
        results: list[dict[str, object]] = []
        for item in data["results"][:max_results]:
            if not isinstance(item, dict):
                continue
            results.append({
                "title": str(item.get("title") or "Untitled result"),
                "url": str(item.get("url") or ""),
                "content": str(item.get("content") or ""),
                "published_date": item.get("published_date"),
                "score": item.get("score"),
            })
        artifact: dict[str, object] = {
            "provider": "tavily",
            "query": query,
            "results": results,
            "response_time": data.get("response_time"),
            "request_id": data.get("request_id"),
            "usage": data.get("usage"),
        }
        if not results:
            return f"No web results found for: {query}", artifact
        lines = [f"Web search results for: {query}", f"{len(results)} results", ""]
        for index, result in enumerate(results, 1):
            lines.append(f"{index}. {result['title']}")
            lines.append(f"   URL: {result['url']}")
            if result["published_date"]:
                lines.append(f"   Published: {result['published_date']}")
            if result["content"]:
                lines.append(f"   {result['content']}")
            lines.append("")
        return "\n".join(lines).rstrip(), artifact

    return StructuredTool.from_function(
        func=web_search,
        name="web_search",
        response_format="content_and_artifact",
        handle_tool_error=True,
        handle_validation_error=True,
    )
