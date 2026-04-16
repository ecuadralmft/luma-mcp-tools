"""Note model with frontmatter and wikilink parsing for the memory MCP server."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

WIKILINK_RE = re.compile(r"\[\[(?:vault:)?([^\]|]+)(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?", re.DOTALL)
SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class Note:
    id: str
    title: str
    created: datetime
    modified: datetime
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    vault: str | None = None
    aliases: list[str] = field(default_factory=list)
    body: str = ""
    links: list[str] = field(default_factory=list)


def slugify(text: str) -> str:
    return SLUG_RE.sub("-", text.lower()).strip("-")[:50]


def extract_wikilinks(text: str) -> list[str]:
    return WIKILINK_RE.findall(text)


def parse_note(path: Path) -> Note:
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    body = raw

    m = FRONTMATTER_RE.match(raw)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        body = raw[m.end():]

    stat = path.stat()

    def _dt(val: str | datetime | None, fallback_ts: float) -> datetime:
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return datetime.fromtimestamp(fallback_ts, tz=timezone.utc)

    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    return Note(
        id=meta.get("id", path.stem),
        title=meta.get("title", path.stem),
        created=_dt(meta.get("created"), stat.st_ctime),
        modified=_dt(meta.get("modified"), stat.st_mtime),
        tags=tags,
        source=meta.get("source"),
        vault=meta.get("vault"),
        aliases=meta.get("aliases", []),
        body=body,
        links=extract_wikilinks(body),
    )


def render_note(note: Note) -> str:
    fm = {
        "id": note.id,
        "title": note.title,
        "created": note.created.isoformat(),
        "modified": note.modified.isoformat(),
        "tags": note.tags,
    }
    if note.source:
        fm["source"] = note.source
    if note.vault:
        fm["vault"] = note.vault
    if note.aliases:
        fm["aliases"] = note.aliases

    header = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return f"---\n{header}---\n{note.body}"


def generate_note_id(tier: str, title: str, vault_path: Path) -> str:
    tier_dir = vault_path / tier
    existing = list(tier_dir.glob("*.md")) if tier_dir.is_dir() else []
    seq = len(existing) + 1
    return f"{tier}/{seq:03d}-{slugify(title)}"
