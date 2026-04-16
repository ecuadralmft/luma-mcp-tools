"""Obsidian Memory — MCP server for vault-based persistent knowledge graph."""

import json
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .note import Note, parse_note, render_note, generate_note_id, extract_wikilinks
from .vault import (
    TIERS, FREEFORM_TIERS, APPEND_ONLY_TIERS,
    resolve_memory_root, resolve_vault, vault_path, tier_path,
    list_vaults as _list_vaults, list_notes,
    read_config, write_config, VaultConfig,
)
from .index import get_index, rebuild_index
from .compat import (
    compat_read, compat_write, compat_search, compat_compact, compat_status,
    _write_locked,
)
from .migrate import migrate_v1 as _migrate_v1

mcp = FastMCP("obsidian")


# ---------------------------------------------------------------------------
# Backward-compatible tools (v1 API)
# ---------------------------------------------------------------------------

@mcp.tool()
def memory_read(
    tier: str,
    search: str | None = None,
    last_n: int | None = None,
    brief: bool = False,
) -> dict:
    """Read from workspace memory. Tiers: project, decisions, learnings, active, glossary, blockers, all. Set brief=True for compact summaries."""
    return compat_read(tier, search, last_n, brief)


@mcp.tool()
def memory_write(
    tier: str,
    content: str,
    mode: str = "append",
    tags: list[str] | None = None,
    source: str | None = None,
) -> dict:
    """Write to workspace memory. Mode: append (default) or overwrite. Overwrite only for project/active tiers."""
    result = compat_write(tier, content, mode, tags, source)
    if result.get("success"):
        get_index().invalidate()
    return result


@mcp.tool()
def memory_search(
    query: str,
    tiers: list[str] | None = None,
) -> dict:
    """Search across memory tiers for a text pattern."""
    return compat_search(query, tiers)


@mcp.tool()
def memory_compact(
    tier: str,
    strategy: str = "dedup",
    days: int = 30,
) -> dict:
    """Compact a memory tier. Strategies: dedup, prune_older_than."""
    result = compat_compact(tier, strategy, days)
    if result.get("entries_before", 0) != result.get("entries_after", 0):
        get_index().invalidate()
    return result


@mcp.tool()
def memory_status() -> dict:
    """Get status of all memory tiers: existence, entry counts, sizes."""
    return compat_status()


# ---------------------------------------------------------------------------
# New note-level tools
# ---------------------------------------------------------------------------

@mcp.tool()
def memory_note_read(note_id: str, vault: str | None = None) -> dict:
    """Read a single note by ID. Returns frontmatter, body, and resolved backlinks."""
    vp = vault_path(vault)
    note_path = vp / f"{note_id}.md"
    if not note_path.is_file():
        return {"error": f"Note not found: {note_id}"}

    note = parse_note(note_path)
    idx = get_index()
    entry = idx.get(note.id)

    return {
        "id": note.id,
        "vault": resolve_vault(vault),
        "frontmatter": {
            "title": note.title,
            "created": note.created.isoformat(),
            "modified": note.modified.isoformat(),
            "tags": note.tags,
            "source": note.source,
            "aliases": note.aliases,
        },
        "body": note.body,
        "links_to": entry.links_to if entry else note.links,
        "linked_from": entry.linked_from if entry else [],
    }


@mcp.tool()
def memory_note_write(
    tier: str,
    title: str,
    body: str,
    tags: list[str] | None = None,
    links: list[str] | None = None,
    vault: str | None = None,
) -> dict:
    """Create a new note in a tier. Auto-generates ID from sequence + title."""
    if tier not in APPEND_ONLY_TIERS:
        return {"error": f"Note creation only for append-only tiers: {', '.join(APPEND_ONLY_TIERS)}"}

    vp = vault_path(vault)
    now = datetime.now(timezone.utc)
    note_id = generate_note_id(tier, title, vp)

    if links:
        body += "\n\n" + "\n".join(f"[[{link}]]" for link in links)

    note = Note(
        id=note_id, title=title, created=now, modified=now,
        tags=tags or [], body=body, vault=resolve_vault(vault),
        links=extract_wikilinks(body),
    )

    note_path = vp / f"{note_id}.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    _write_locked(note_path, render_note(note))

    idx = get_index()
    idx.update_note(note.id, note_path)
    idx.save(resolve_memory_root() / ".index.json")

    return {"success": True, "id": note_id, "vault": resolve_vault(vault), "path": str(note_path)}


@mcp.tool()
def memory_note_update(
    note_id: str,
    body: str | None = None,
    tags: list[str] | None = None,
    append: str | None = None,
    vault: str | None = None,
) -> dict:
    """Update an existing note. Use append to add content to the end."""
    vp = vault_path(vault)
    note_path = vp / f"{note_id}.md"
    if not note_path.is_file():
        return {"error": f"Note not found: {note_id}"}

    note = parse_note(note_path)
    note.modified = datetime.now(timezone.utc)

    if body is not None:
        note.body = body
    if append:
        note.body = note.body.rstrip() + "\n\n" + append
    if tags is not None:
        note.tags = tags

    note.links = extract_wikilinks(note.body)
    _write_locked(note_path, render_note(note))

    idx = get_index()
    idx.update_note(note.id, note_path)
    idx.save(resolve_memory_root() / ".index.json")

    return {"success": True, "id": note.id, "modified": note.modified.isoformat()}


# ---------------------------------------------------------------------------
# Link tools
# ---------------------------------------------------------------------------

@mcp.tool()
def memory_link(
    from_note: str,
    to_note: str,
    relation: str | None = None,
    vault: str | None = None,
) -> dict:
    """Create a wikilink from one note to another."""
    vp = vault_path(vault)
    from_path = vp / f"{from_note}.md"
    if not from_path.is_file():
        return {"error": f"Source note not found: {from_note}"}

    note = parse_note(from_path)
    link_text = f"[[{to_note}]]"
    if relation:
        link_text = f"{relation}: [[{to_note}]]"

    note.body = note.body.rstrip() + "\n" + link_text + "\n"
    note.modified = datetime.now(timezone.utc)
    note.links = extract_wikilinks(note.body)
    _write_locked(from_path, render_note(note))

    idx = get_index()
    idx.update_note(note.id, from_path)
    idx.save(resolve_memory_root() / ".index.json")

    return {"success": True, "from": from_note, "to": to_note, "relation": relation}


@mcp.tool()
def memory_backlinks(note_id: str, vault: str | None = None) -> dict:
    """Return all notes that link TO this note, with context."""
    idx = get_index()
    return {"note_id": note_id, "backlinks": idx.backlinks(note_id)}


@mcp.tool()
def memory_graph(note_id: str, depth: int = 1, vault: str | None = None) -> dict:
    """Return the neighborhood graph around a note. Depth 1 = direct links."""
    idx = get_index()
    return idx.graph(note_id, depth)


# ---------------------------------------------------------------------------
# Vault tools
# ---------------------------------------------------------------------------

@mcp.tool()
def vault_list() -> dict:
    """List all vaults with note counts and last activity."""
    vaults = _list_vaults()
    return {"active_vault": resolve_vault(), "vaults": vaults}


@mcp.tool()
def vault_switch(vault: str) -> dict:
    """Switch the active vault for this session. Creates vault if needed."""
    vp = vault_path(vault)
    config = read_config()
    config.active_vault = vault
    config.vault_registry[vault] = str(vp)
    write_config(config)
    return {"success": True, "active_vault": vault, "path": str(vp)}


# ---------------------------------------------------------------------------
# Migration tool
# ---------------------------------------------------------------------------

@mcp.tool()
def memory_migrate_v1(source_dir: str, target_vault: str | None = None) -> dict:
    """Migrate v1 flat-file memory to v2 vault format. Non-destructive."""
    result = _migrate_v1(source_dir, target_vault)
    if result.get("notes_created", 0) > 0:
        rebuild_index()
    return result


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

@mcp.tool()
def discover_tools(force: bool = False) -> dict:
    """Discover all MCP tools. Uses cached result if mcp.json hasn't changed."""
    mcp_json = Path.home() / ".kiro" / "settings" / "mcp.json"
    if not mcp_json.exists():
        return {"error": "No mcp.json found"}

    mcp_content = mcp_json.read_text()
    config_hash = hashlib.md5(mcp_content.encode()).hexdigest()

    root = resolve_memory_root()
    cache_path = root / "global" / "tool-inventory.json"
    inventory_path = root / "global" / "tool-inventory.md"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("config_hash") == config_hash:
                return {
                    "cached": True,
                    "servers": cached["servers"],
                    "total_tools": cached["total_tools"],
                    "discovered_at": cached["discovered_at"],
                }
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    cfg = json.loads(mcp_content)
    servers = cfg.get("mcpServers", {})
    all_tools: dict[str, list[str]] = {}
    errors: dict[str, str] = {}

    init_msg = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "discover", "version": "1.0"}}
    })
    notif_msg = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    list_msg = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

    def _extract_names(data: dict) -> list[str]:
        if "result" in data and "tools" in data["result"]:
            return [t["name"] for t in data["result"]["tools"]]
        return []

    def _probe_server(name, conf):
        if name == "memory":
            return name, [
                "memory_read", "memory_write", "memory_search", "memory_compact",
                "memory_status", "memory_note_read", "memory_note_write",
                "memory_note_update", "memory_link", "memory_backlinks",
                "memory_graph", "vault_list", "vault_switch",
                "memory_migrate_v1", "discover_tools",
            ], None

        if "url" in conf:
            try:
                import urllib.request
                url = conf["url"]
                hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
                req = urllib.request.Request(url, data=init_msg.encode(), headers=hdrs, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    sid = resp.headers.get("Mcp-Session-Id", "")
                if sid:
                    hdrs["Mcp-Session-Id"] = sid
                urllib.request.urlopen(urllib.request.Request(url, data=notif_msg.encode(), headers=hdrs, method="POST"), timeout=5)
                with urllib.request.urlopen(urllib.request.Request(url, data=list_msg.encode(), headers=hdrs, method="POST"), timeout=15) as r3:
                    for line in r3.read().decode().splitlines():
                        if line.startswith("data: "):
                            names = _extract_names(json.loads(line[6:]))
                            if names:
                                return name, names, None
            except Exception as e:
                return name, None, str(e)
        elif "command" in conf:
            try:
                cmd = [conf["command"]] + conf.get("args", [])
                proc = subprocess.run(cmd, input=f"{init_msg}\n{notif_msg}\n{list_msg}\n",
                                      capture_output=True, text=True, timeout=15)
                for line in proc.stdout.strip().splitlines():
                    try:
                        names = _extract_names(json.loads(line))
                        if names:
                            return name, names, None
                    except json.JSONDecodeError:
                        continue
            except Exception as e:
                return name, None, str(e)
        return name, None, "Unknown server type"

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(servers)) as pool:
        futures = [pool.submit(_probe_server, n, c) for n, c in servers.items()]
        for future in futures:
            sname, tools, err = future.result()
            if tools:
                all_tools[sname] = tools
            if err:
                errors[sname] = err

    total = sum(len(t) for t in all_tools.values())
    result = {
        "config_hash": config_hash,
        "servers": {n: len(t) for n, t in all_tools.items()},
        "total_tools": total,
        "tools_by_server": all_tools,
        "errors": errors,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }

    _write_locked(cache_path, json.dumps(result, indent=2))

    lines = [f"## MCP Tool Inventory ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n"]
    lines.append(f"Total: {total} tools across {len(all_tools)} servers\n")
    for sname, tools in all_tools.items():
        lines.append(f"\n### {sname} ({len(tools)})")
        lines.append(", ".join(tools))
    _write_locked(inventory_path, "\n".join(lines) + "\n")

    return {
        "cached": False,
        "servers": result["servers"],
        "total_tools": total,
        "errors": errors if errors else None,
        "discovered_at": result["discovered_at"],
    }


# Entry point: use __main__.py (python3 -m memory)
