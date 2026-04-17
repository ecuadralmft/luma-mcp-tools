"""Web Search MCP — read-only web search and page reader."""

import re
import time

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("web")

_last_request = 0.0


def _rate_limit():
    global _last_request
    now = time.monotonic()
    wait = 1.0 - (now - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _search_ddg(query: str, max_results: int) -> list[dict]:
    _rate_limit()
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            })
    return results


def _extract_text(html: str, max_chars: int) -> tuple[str, str]:
    """Extract clean text from HTML. Returns (title, content)."""
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()

    main = soup.find("article") or soup.find("main") or soup.find("body")
    if not main:
        return title, ""

    text = main.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = text.strip()

    return title, text[:max_chars]


@mcp.tool()
def web_search(query: str, max_results: int = 3) -> dict:
    """Search the web via DuckDuckGo. Returns titles, URLs, and snippets.
    Use only when local knowledge (memory, codebase, Luma tools) is insufficient.
    Check snippets before calling web_read — often the snippet has the answer."""
    try:
        results = _search_ddg(query, max_results)
        return {"query": query, "results": results, "result_count": len(results)}
    except Exception as e:
        return {"error": str(e), "query": query}


@mcp.tool()
def web_read(url: str, max_chars: int = 4000) -> dict:
    """Fetch a URL and return clean text content. Strips HTML, prefers article/main content.
    Use only after web_search confirms the page is relevant. Default 4000 chars."""
    try:
        _rate_limit()
        with httpx.Client(timeout=10, follow_redirects=True, verify=False,
                          headers={"User-Agent": "luma-web-mcp/1.0 (research assistant)"}) as client:
            resp = client.get(url)
            resp.raise_for_status()

        title, content = _extract_text(resp.text, max_chars)
        return {
            "url": url,
            "title": title,
            "content": content,
            "content_length": len(content),
            "truncated": len(content) >= max_chars,
        }
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "url": url}
    except httpx.TimeoutException:
        return {"error": "Timeout (10s)", "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}
