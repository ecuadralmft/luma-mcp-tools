"""Link graph with backlinks and traversal for the memory MCP server."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .note import parse_note, Note, WIKILINK_RE
from .vault import resolve_memory_root


@dataclass
class NoteEntry:
    id: str
    title: str
    tags: list[str]
    links_to: list[str] = field(default_factory=list)
    linked_from: list[str] = field(default_factory=list)


class LinkIndex:
    def __init__(self) -> None:
        self.entries: dict[str, NoteEntry] = {}
        self._paths: dict[str, Path] = {}
        self._root: Path | None = None
        self._dirty: bool = True

    def build(self, root: Path) -> None:
        self._root = root
        self.entries.clear()
        self._paths.clear()

        for md in root.rglob("*.md"):
            if "backups" in md.parts:
                continue
            note = parse_note(md)
            self.entries[note.id] = NoteEntry(
                id=note.id,
                title=note.title,
                tags=list(note.tags),
                links_to=list(note.links),
            )
            self._paths[note.id] = md

        for nid, entry in self.entries.items():
            for target in entry.links_to:
                if target in self.entries:
                    self.entries[target].linked_from.append(nid)

        self._dirty = False

    def save(self, path: Path) -> None:
        notes = {}
        for nid, e in self.entries.items():
            notes[nid] = {
                "title": e.title,
                "tags": e.tags,
                "links_to": e.links_to,
                "linked_from": e.linked_from,
            }
        data = {
            "rebuilt_at": datetime.now(timezone.utc).isoformat(),
            "notes": notes,
        }
        path.write_text(json.dumps(data, indent=2) + "\n")

    def load(self, path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return False

        self.entries.clear()
        for nid, info in data.get("notes", {}).items():
            self.entries[nid] = NoteEntry(
                id=nid,
                title=info.get("title", nid),
                tags=info.get("tags", []),
                links_to=info.get("links_to", []),
                linked_from=info.get("linked_from", []),
            )
        self._dirty = False
        return True

    def invalidate(self) -> None:
        self._dirty = True

    def get(self, note_id: str) -> NoteEntry | None:
        return self.entries.get(note_id)

    def backlinks(self, note_id: str) -> list[dict]:
        entry = self.entries.get(note_id)
        if not entry:
            return []

        results = []
        for src_id in entry.linked_from:
            src_path = self._paths.get(src_id)
            if not src_path or not src_path.is_file():
                results.append({"from": src_id, "context": ""})
                continue

            context = ""
            text = src_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                for match in WIKILINK_RE.finditer(line):
                    if match.group(1) == note_id:
                        context = line.strip()
                        break
                if context:
                    break
            results.append({"from": src_id, "context": context})
        return results

    def graph(self, note_id: str, depth: int = 1) -> dict:
        center = self.entries.get(note_id)
        if not center:
            return {"center": note_id, "nodes": [], "edges": []}

        visited: set[str] = set()
        nodes: list[dict] = []
        edges: list[dict] = []
        queue: deque[tuple[str, int]] = deque([(note_id, 0)])

        while queue:
            nid, d = queue.popleft()
            if nid in visited:
                continue
            visited.add(nid)

            entry = self.entries.get(nid)
            if not entry:
                continue

            nodes.append({"id": entry.id, "title": entry.title, "tags": entry.tags})

            if d < depth:
                for target in entry.links_to:
                    edges.append({"from": nid, "to": target, "relation": "links_to"})
                    if target not in visited:
                        queue.append((target, d + 1))
                for src in entry.linked_from:
                    edges.append({"from": src, "to": nid, "relation": "linked_from"})
                    if src not in visited:
                        queue.append((src, d + 1))

        return {"center": note_id, "nodes": nodes, "edges": edges}

    def update_note(self, note_id: str, note_path: Path) -> None:
        old = self.entries.get(note_id)
        if old:
            for target in old.links_to:
                if target in self.entries:
                    lf = self.entries[target].linked_from
                    if note_id in lf:
                        lf.remove(note_id)

        note = parse_note(note_path)
        self.entries[note.id] = NoteEntry(
            id=note.id,
            title=note.title,
            tags=list(note.tags),
            links_to=list(note.links),
        )
        self._paths[note.id] = note_path

        for target in note.links:
            if target in self.entries and note.id not in self.entries[target].linked_from:
                self.entries[target].linked_from.append(note.id)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_index: LinkIndex | None = None


def get_index(root: Path | None = None) -> LinkIndex:
    global _index
    if _index is not None and not _index._dirty:
        return _index

    _index = LinkIndex()
    r = root or resolve_memory_root()
    cache = r / ".index.json"

    if not _index.load(cache):
        _index.build(r)
        _index.save(cache)

    return _index


def rebuild_index(root: Path | None = None) -> LinkIndex:
    global _index
    _index = LinkIndex()
    r = root or resolve_memory_root()
    _index.build(r)
    _index.save(r / ".index.json")
    return _index
