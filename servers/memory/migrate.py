"""V1 to V2 memory migration: flat tier files to individual vault notes."""

import fcntl
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .note import Note, generate_note_id, render_note
from .vault import APPEND_ONLY_TIERS, FREEFORM_TIERS, TIERS, vault_path

ENTRY_SEPARATOR = "\n\n---\n\n"
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?", re.DOTALL)


def _extract_title(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.lstrip("#").strip()[:80]
    return "untitled"


def parse_v1_entries(content: str) -> list[dict]:
    blocks = content.split(ENTRY_SEPARATOR)
    entries = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        meta: dict = {}
        body = block
        m = FRONTMATTER_RE.match(block)
        if m:
            meta = yaml.safe_load(m.group(1)) or {}
            body = block[m.end():].strip()
        date = meta.get("date")
        if isinstance(date, str):
            try:
                date = datetime.fromisoformat(date)
            except ValueError:
                date = None
        if date and not date.tzinfo:
            date = date.replace(tzinfo=timezone.utc)
        tags = meta.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        entries.append({
            "date": date,
            "tags": tags,
            "source": meta.get("source"),
            "body": body,
        })
    return entries


def _write_locked(path: Path, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(content)


def migrate_v1(source_dir: str, target_vault: str | None = None) -> dict:
    src = Path(source_dir)
    vault_name = target_vault or "global"
    vp = vault_path(vault_name)
    now = datetime.now(timezone.utc)

    report: dict = {
        "vault": vault_name,
        "notes_created": 0,
        "files_copied": 0,
        "errors": [],
        "details_per_tier": {},
    }

    for tier in TIERS:
        src_file = src / f"{tier}.md"
        if not src_file.is_file():
            continue

        tier_report: dict = {"action": None, "count": 0, "errors": []}

        if tier in FREEFORM_TIERS:
            dest = vp / f"{tier}.md"
            try:
                shutil.copy2(src_file, dest)
                tier_report["action"] = "copied"
                tier_report["count"] = 1
                report["files_copied"] += 1
            except OSError as e:
                tier_report["errors"].append(str(e))
                report["errors"].append(f"{tier}: {e}")
        elif tier in APPEND_ONLY_TIERS:
            content = src_file.read_text(encoding="utf-8")
            entries = parse_v1_entries(content)
            tier_report["action"] = "migrated"

            for entry in entries:
                title = _extract_title(entry["body"])
                created = entry["date"] or now
                note_id = generate_note_id(tier, title, vp)
                note = Note(
                    id=note_id,
                    title=title,
                    created=created,
                    modified=created,
                    tags=entry["tags"],
                    source=entry["source"],
                    body=entry["body"],
                )
                dest = vp / f"{note_id}.md"
                try:
                    _write_locked(dest, render_note(note))
                    tier_report["count"] += 1
                    report["notes_created"] += 1
                except OSError as e:
                    tier_report["errors"].append(str(e))
                    report["errors"].append(f"{tier}/{title}: {e}")

        report["details_per_tier"][tier] = tier_report

    return report
