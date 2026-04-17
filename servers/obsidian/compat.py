"""Backward-compatible tier API — translates v1 tool signatures to v2 vault/note internals."""

from __future__ import annotations

import fcntl
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .note import Note, generate_note_id, parse_note, render_note
from .vault import (
    APPEND_ONLY_TIERS,
    FREEFORM_TIERS,
    TIERS,
    list_notes,
    overlay_read,
    tier_path,
    vault_path,
)

ENTRY_SEPARATOR = "\n\n---\n\n"
MAX_ENTRIES_BEFORE_COMPACT_WARNING = 500


def _write_locked(path: Path, content: str, mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(content)
        f.flush()
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read_tier_content(tier: str) -> tuple[str, list[Note]]:
    """Read a tier and return (concatenated_content, notes).
    For freeform tiers, notes list is empty."""
    if tier in FREEFORM_TIERS:
        paths = overlay_read(tier)
        content = ""
        for p in paths:
            if p.is_file():
                content = p.read_text()
                break
        return content, []

    paths = overlay_read(tier)
    notes = [parse_note(p) for p in paths]
    if not notes:
        return "", notes
    content = ENTRY_SEPARATOR.join(n.body for n in notes)
    return content, notes


def _tier_last_updated(tier: str) -> str | None:
    paths = overlay_read(tier)
    if not paths:
        return None
    mtime = max(p.stat().st_mtime for p in paths if p.is_file())
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat() if mtime else None


def _tier_size(tier: str) -> int:
    return sum(p.stat().st_size for p in overlay_read(tier) if p.is_file())


def _count_entries(tier: str) -> int:
    paths = overlay_read(tier)
    if tier in FREEFORM_TIERS:
        return 1 if paths and any(p.is_file() and p.read_text().strip() for p in paths) else 0
    return len(paths)


# ---------------------------------------------------------------------------
# compat_read
# ---------------------------------------------------------------------------

def compat_read(
    tier: str,
    search: str | None = None,
    last_n: int | None = None,
    brief: bool = False,
) -> dict:
    if tier == "all":
        combined: dict = {}
        for t in TIERS:
            count = _count_entries(t)
            if brief:
                combined[t] = {"entries": count, "bytes": _tier_size(t)}
            else:
                content, _ = _read_tier_content(t)
                if not content.strip():
                    continue
                combined[t] = {
                    "content": content[:2000] + ("..." if len(content) > 2000 else ""),
                    "entries_count": count,
                }
        return {"tier": "all", "tiers": combined}

    if tier not in TIERS:
        return {"error": f"Unknown tier: {tier}. Valid: {', '.join(TIERS)}, all"}

    content, notes = _read_tier_content(tier)

    if search and content:
        pattern = re.compile(search, re.IGNORECASE)
        if notes:
            notes = [n for n in notes if pattern.search(n.body)]
            content = ENTRY_SEPARATOR.join(n.body for n in notes)
        else:
            lines = content.splitlines()
            content = "\n".join(l for l in lines if pattern.search(l))

    if last_n and notes:
        notes = notes[-last_n:]
        content = ENTRY_SEPARATOR.join(n.body for n in notes)

    count = len(notes) if notes else (1 if content.strip() else 0)

    if brief:
        preview = ""
        if notes:
            preview = notes[-1].body[:100]
        elif content.strip():
            preview = content.strip()[:100]
        return {"tier": tier, "entries": count, "preview": preview}

    return {
        "tier": tier,
        "content": content,
        "entries_count": count,
        "last_updated": _tier_last_updated(tier),
    }


# ---------------------------------------------------------------------------
# compat_write
# ---------------------------------------------------------------------------

def compat_write(
    tier: str,
    content: str,
    mode: str = "append",
    tags: list[str] | None = None,
    source: str | None = None,
) -> dict:
    if tier not in TIERS:
        return {"error": f"Unknown tier: {tier}. Valid: {', '.join(TIERS)}"}

    if mode == "overwrite" and tier in APPEND_ONLY_TIERS:
        return {"error": f"Tier '{tier}' is append-only. Use mode='append'."}

    now = datetime.now(timezone.utc)

    if tier in FREEFORM_TIERS:
        tp = tier_path(tier)
        if mode == "overwrite":
            _write_locked(tp, content + "\n")
        else:
            _write_locked(tp, content + "\n", mode="a")
    else:
        vp = vault_path()
        note_id = generate_note_id(tier, content[:40], vp)
        note = Note(
            id=note_id,
            title=content[:40],
            created=now,
            modified=now,
            tags=tags or [],
            source=source,
            body=content,
        )
        note_path = vp / f"{note_id}.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        _write_locked(note_path, render_note(note))

    return {
        "success": True,
        "tier": tier,
        "entries_count": _count_entries(tier),
        "timestamp": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# compat_search
# ---------------------------------------------------------------------------

def compat_search(
    query: str,
    tiers: list[str] | None = None,
) -> dict:
    search_tiers = tiers or list(TIERS)
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []

    for tier in search_tiers:
        if tier not in TIERS:
            continue
        content, _ = _read_tier_content(tier)
        if not content:
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if pattern.search(line):
                start = max(0, i - 3)
                end = min(len(lines), i + 2)
                results.append({
                    "tier": tier,
                    "line_number": i,
                    "content": line.strip(),
                    "context": "\n".join(lines[start:end]),
                })

    return {"results": results, "total_matches": len(results)}


# ---------------------------------------------------------------------------
# compat_compact
# ---------------------------------------------------------------------------

def compat_compact(
    tier: str,
    strategy: str = "dedup",
    days: int = 30,
) -> dict:
    if tier not in TIERS:
        return {"error": f"Unknown tier: {tier}. Valid: {', '.join(TIERS)}"}
    if tier not in APPEND_ONLY_TIERS:
        return {"error": f"Tier '{tier}' is freeform, not entry-based. Edit directly."}

    note_paths = list_notes(tier)
    before = len(note_paths)

    if before == 0:
        return {
            "tier": tier,
            "entries_before": 0,
            "entries_after": 0,
            "compacted_at": datetime.now(timezone.utc).isoformat(),
        }

    vp = vault_path()
    backup_dir = vp / "backups" / f"{tier}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for p in note_paths:
        shutil.copy2(p, backup_dir / p.name)

    to_remove: list[Path] = []

    if strategy == "dedup":
        seen: dict[str, Path] = {}
        for p in note_paths:
            note = parse_note(p)
            key = note.body.strip().lower()
            if key in seen:
                to_remove.append(p)
            else:
                seen[key] = p

    elif strategy == "prune_older_than":
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for p in note_paths:
            note = parse_note(p)
            if note.created < cutoff:
                to_remove.append(p)

    else:
        return {"error": f"Unknown strategy: {strategy}. Valid: dedup, prune_older_than"}

    for p in to_remove:
        p.unlink()

    return {
        "tier": tier,
        "entries_before": before,
        "entries_after": before - len(to_remove),
        "compacted_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# compat_status
# ---------------------------------------------------------------------------

def compat_status() -> dict:
    tier_info = []
    for tier in TIERS:
        count = _count_entries(tier)
        size = _tier_size(tier)
        last_updated = _tier_last_updated(tier)
        exists = count > 0 or size > 0

        tier_info.append({
            "name": tier,
            "exists": exists,
            "entries_count": count,
            "last_updated": last_updated,
            "size_bytes": size,
            "needs_compaction": tier in APPEND_ONLY_TIERS and count > MAX_ENTRIES_BEFORE_COMPACT_WARNING,
        })

    total = sum(t["size_bytes"] for t in tier_info)
    return {
        "tiers": tier_info,
        "total_size_bytes": total,
        "memory_dir": str(vault_path()),
    }
