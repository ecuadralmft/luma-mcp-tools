"""PB-Ticket MCP — smart Jira reader for the PB board."""

import json
import os
import re
import urllib.request
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pb-ticket")

PROJECT = "PB"
LUMA_MCP_URL = os.environ.get("LUMA_MCP_URL", "https://ai-mcp.lumafintech.com/mcp")

SECTION_PATTERNS = [
    ("problem", re.compile(r"#{0,4}\s*1\.\s*Problem", re.I)),
    ("user_module", re.compile(r"#{0,4}\s*2\.\s*User\s*/?\s*Client\s*/?\s*Module", re.I)),
    ("why_now", re.compile(r"#{0,4}\s*3\.\s*Why\s+[Nn]ow", re.I)),
    ("desired_outcome", re.compile(r"#{0,4}\s*4\.\s*Desired\s+[Oo]utcome", re.I)),
    ("scope", re.compile(r"#{0,4}\s*5\.\s*Scope", re.I)),
    ("acceptance_criteria", re.compile(r"#{0,4}\s*6\.\s*Acceptance\s+[Cc]riteria", re.I)),
    ("dependencies", re.compile(r"#{0,4}\s*7\.\s*Dependenc", re.I)),
    ("notes", re.compile(r"#{0,4}\s*8\.\s*Notes", re.I)),
]


# ---------------------------------------------------------------------------
# Luma MCP proxy
# ---------------------------------------------------------------------------

_session_id = None


def _luma_call(method: str, arguments: dict, _retry: bool = True) -> dict:
    global _session_id
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    try:
        if not _session_id:
            init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                           "clientInfo": {"name": "pb-ticket", "version": "1.0"}}})
            req = urllib.request.Request(LUMA_MCP_URL, data=init.encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                _session_id = resp.headers.get("Mcp-Session-Id", "")
            notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            headers["Mcp-Session-Id"] = _session_id
            urllib.request.urlopen(urllib.request.Request(LUMA_MCP_URL, data=notif.encode(), headers=headers, method="POST"), timeout=5)

        headers["Mcp-Session-Id"] = _session_id
        call = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": method, "arguments": arguments}})
        req = urllib.request.Request(LUMA_MCP_URL, data=call.encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()

        for line in body.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                content = data.get("result", {}).get("content", [])
                for c in content:
                    text = c.get("text", "")
                    if text:
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            return {"raw": text}
    except Exception:
        if _retry:
            _session_id = None
            return _luma_call(method, arguments, _retry=False)
        return {"error": f"Luma MCP call failed: {method}"}

    # Empty response — session may be stale
    if _retry:
        _session_id = None
        return _luma_call(method, arguments, _retry=False)
    return {}


def _jira_get(key: str) -> dict:
    return _luma_call("get_jira_issue", {"issue_key": key, "expand": "renderedFields,names"})


def _jira_search(jql: str, fields: str = "summary,status,issuetype,parent", max_results: int = 200) -> list:
    result = _luma_call("search_jira_issues", {"jql": jql, "fields": fields, "max_results": max_results})
    return result.get("issues", [])


# ---------------------------------------------------------------------------
# ADF parser
# ---------------------------------------------------------------------------

def _adf_to_text(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    if isinstance(node, dict):
        ntype = node.get("type", "")
        text = node.get("text", "")
        children = node.get("content", [])
        child_text = "".join(_adf_to_text(c) for c in children)
        if ntype == "hardBreak":
            return "\n"
        if ntype == "heading":
            level = node.get("attrs", {}).get("level", 2)
            return f"\n{'#' * level} {child_text}\n"
        if ntype == "codeBlock":
            lang = node.get("attrs", {}).get("language", "")
            return f"\n```{lang}\n{child_text}\n```\n"
        if ntype == "bulletList":
            return child_text
        if ntype == "orderedList":
            return child_text
        if ntype == "listItem":
            return f"- {child_text}\n"
        if ntype == "table":
            return child_text + "\n"
        if ntype in ("tableRow", "tableHeader", "tableCell"):
            return child_text + " | "
        if ntype == "mention":
            return node.get("attrs", {}).get("text", text)
        return text + child_text
    return ""


def _parse_sections(adf) -> dict:
    if not adf or not isinstance(adf, dict):
        return {}
    text = _adf_to_text(adf).strip()
    if not text:
        return {}

    positions = []
    for name, pattern in SECTION_PATTERNS:
        m = pattern.search(text)
        if m:
            positions.append((m.start(), name))

    if not positions:
        return {"description": text}

    positions.sort()
    sections = {}
    for i, (pos, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        chunk = text[pos:end]
        lines = chunk.split("\n", 1)
        sections[name] = lines[1].strip() if len(lines) > 1 else ""

    return sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compact_issue(issue: dict) -> dict:
    f = issue.get("fields", {})
    return {
        "key": issue.get("key", ""),
        "type": f.get("issuetype", {}).get("name", ""),
        "summary": f.get("summary", ""),
        "status": f.get("status", {}).get("name", ""),
    }


def _walk_parent_chain(issue: dict) -> list:
    chain = []
    current = issue
    seen = set()
    while True:
        parent = current.get("fields", {}).get("parent")
        if not parent:
            break
        parent_key = parent.get("key", "")
        if not parent_key or parent_key in seen:
            break
        seen.add(parent_key)
        parent_issue = _jira_get(parent_key)
        if not parent_issue or "fields" not in parent_issue:
            chain.append({"key": parent_key, "type": "?",
                          "summary": parent.get("fields", {}).get("summary", "?")})
            break
        chain.append(_compact_issue(parent_issue))
        current = parent_issue
    chain.reverse()
    return chain


def _build_tree(issues: list) -> list:
    by_key = {}
    for iss in issues:
        c = _compact_issue(iss)
        c["children"] = []
        c["child_count"] = 0
        parent = iss.get("fields", {}).get("parent", {})
        c["_parent_key"] = parent.get("key") if parent else None
        by_key[c["key"]] = c

    roots = []
    for key, node in by_key.items():
        pk = node.pop("_parent_key", None)
        if pk and pk in by_key:
            by_key[pk]["children"].append(node)
            by_key[pk]["child_count"] = len(by_key[pk]["children"])
        else:
            roots.append(node)
    return roots


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def pb_context(scope: str | None = None) -> dict:
    """Pull the PB board hierarchy from Jira. Returns Initiative → Epic → Feature tree.
    Optional scope filter (e.g., 'MSWM', 'Blotter', 'PB-215') to narrow results."""
    jql = f"project = {PROJECT} AND issuetype in (Initiative, Epic, Feature) ORDER BY key ASC"
    issues = _jira_search(jql, fields="summary,status,issuetype,parent")

    if scope:
        scope_lower = scope.lower()
        matching_keys = set()
        for iss in issues:
            f = iss.get("fields", {})
            key = iss.get("key", "")
            summary = f.get("summary", "")
            if scope_lower in key.lower() or scope_lower in summary.lower():
                matching_keys.add(key)

        # Build parent lookup
        parent_of = {}
        children_of = {}
        for iss in issues:
            key = iss.get("key", "")
            parent = iss.get("fields", {}).get("parent", {})
            pk = parent.get("key") if parent else None
            parent_of[key] = pk
            if pk:
                children_of.setdefault(pk, set()).add(key)

        # Expand ancestors (loop until stable)
        expanded = set(matching_keys)
        changed = True
        while changed:
            changed = False
            for key in list(expanded):
                pk = parent_of.get(key)
                if pk and pk not in expanded:
                    expanded.add(pk)
                    changed = True

        # Expand children of matching keys
        for key in list(matching_keys):
            for child in children_of.get(key, set()):
                expanded.add(child)

        issues = [i for i in issues if i.get("key", "") in expanded]

    tree = _build_tree(issues)
    return {"hierarchy": tree, "total_issues": len(issues)}


@mcp.tool()
def pb_ticket(key: str) -> dict:
    """Pull a PB ticket with parent chain, siblings, and parsed description sections."""
    issue = _jira_get(key)
    if not issue or "fields" not in issue:
        return {"error": f"Ticket not found: {key}"}

    f = issue.get("fields", {})
    result = _compact_issue(issue)
    result["story_points"] = f.get("customfield_10005")
    result["severity"] = (f.get("customfield_10812") or {}).get("value")
    result["parent_chain"] = _walk_parent_chain(issue)

    result["sections"] = _parse_sections(f.get("description"))

    vs = f.get("customfield_10820")
    if vs:
        result["value_statement"] = _adf_to_text(vs).strip()
    dod = f.get("customfield_10819")
    if dod:
        result["definition_of_done"] = _adf_to_text(dod).strip()

    parent = f.get("parent", {})
    if parent and parent.get("key"):
        sibling_issues = _jira_search(
            f"project = {PROJECT} AND parent = {parent['key']} AND key != {key}",
            fields="summary,status,issuetype"
        )
        result["siblings"] = [_compact_issue(s) for s in sibling_issues]
    else:
        result["siblings"] = []

    return result


@mcp.tool()
def pb_place(summary: str, description: str | None = None) -> dict:
    """Return all PB Features with parent Epics for placement reasoning.
    The agent decides which Feature fits based on conversation context."""
    features = _jira_search(
        f"project = {PROJECT} AND issuetype = Feature ORDER BY key ASC",
        fields="summary,status,parent"
    )

    result = []
    for iss in features:
        f = iss.get("fields", {})
        parent = f.get("parent", {})
        parent_summary = parent.get("fields", {}).get("summary", "") if parent else ""
        parent_key = parent.get("key", "") if parent else ""
        result.append({
            "key": iss.get("key", ""),
            "summary": f.get("summary", ""),
            "status": f.get("status", {}).get("name", ""),
            "parent_epic": f"{parent_key} — {parent_summary}" if parent_key else "None",
        })

    return {"query": summary, "features": result, "total_features": len(result)}
