"""Vault resolution and multi-vault overlay for the memory MCP server."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TIERS = ("project", "decisions", "learnings", "active", "glossary", "blockers")
FREEFORM_TIERS = ("project", "active")
APPEND_ONLY_TIERS = ("decisions", "learnings", "glossary", "blockers")

CONFIG_FILE = ".config.json"


def _run_git(args: list[str], cwd: Path | None = None) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=cwd, timeout=10
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _walk_up(marker: str, start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    while True:
        candidate = cur / marker
        if candidate.exists():
            return candidate
        if cur == cur.parent:
            break
        cur = cur.parent
    return None


def _extract_repo_name(git_dir: Path) -> str | None:
    """Extract repo name from origin remote URL."""
    config_path = git_dir / "config"
    if config_path.is_file():
        text = config_path.read_text()
        m = re.search(
            r'\[remote\s+"origin"\].*?url\s*=\s*(.+)',
            text,
            re.DOTALL,
        )
        if m:
            url = m.group(1).strip().split("\n")[0]
            name = url.rstrip("/").rsplit("/", 1)[-1]
            return name.removesuffix(".git") or None

    out = _run_git(["remote", "get-url", "origin"], cwd=git_dir.parent)
    if out:
        name = out.rstrip("/").rsplit("/", 1)[-1]
        return name.removesuffix(".git") or None
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class VaultConfig:
    active_vault: str = "global"
    vault_registry: dict[str, str] = field(default_factory=dict)


def read_config() -> VaultConfig:
    path = resolve_memory_root() / CONFIG_FILE
    if not path.is_file():
        return VaultConfig()
    try:
        data = json.loads(path.read_text())
        return VaultConfig(
            active_vault=data.get("active_vault", "global"),
            vault_registry=data.get("vault_registry", {}),
        )
    except (json.JSONDecodeError, OSError):
        return VaultConfig()


def write_config(config: VaultConfig) -> None:
    path = resolve_memory_root() / CONFIG_FILE
    path.write_text(json.dumps({
        "active_vault": config.active_vault,
        "vault_registry": config.vault_registry,
    }, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_memory_root() -> Path:
    env = os.environ.get("MEMORY_ROOT")
    if env:
        root = Path(env).resolve()
    else:
        found = _walk_up(".luma-memory")
        root = found if found else Path.home() / ".luma-memory"

    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_vault(explicit_vault: str | None = None) -> str:
    if explicit_vault:
        return explicit_vault

    env = os.environ.get("MEMORY_VAULT")
    if env:
        return env

    vault_file = _walk_up(".vault")
    if vault_file:
        name = vault_file.read_text().strip()
        if name:
            return name

    # Check config (set by vault_switch)
    config_path = resolve_memory_root() / CONFIG_FILE
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text())
            active = data.get("active_vault")
            if active and active != "global":
                return active
        except (json.JSONDecodeError, OSError):
            pass

    git_dir = _walk_up(".git")
    if git_dir and git_dir.is_dir():
        repo_name = _extract_repo_name(git_dir)
        if repo_name:
            return repo_name

    return "global"


def _sanitize_vault_name(name: str) -> str:
    """Strip path traversal and invalid characters from vault names."""
    name = name.replace("..", "").replace("/", "").replace("\\", "").strip(". ")
    return name or "global"


def vault_path(vault_name: str | None = None) -> Path:
    name = _sanitize_vault_name(resolve_vault(vault_name))
    vp = resolve_memory_root() / name
    vp.mkdir(parents=True, exist_ok=True)
    for tier in TIERS:
        if tier in APPEND_ONLY_TIERS:
            (vp / tier).mkdir(exist_ok=True)
    return vp


def tier_path(tier: str, vault_name: str | None = None) -> Path:
    vp = vault_path(vault_name)
    if tier in FREEFORM_TIERS:
        return vp / f"{tier}.md"
    return vp / tier


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def list_vaults() -> list[dict]:
    root = resolve_memory_root()
    vaults = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        note_count = sum(1 for f in entry.rglob("*.md") if f.is_file())
        mtime = max(
            (f.stat().st_mtime for f in entry.rglob("*") if f.is_file()),
            default=0,
        )
        vaults.append({
            "name": entry.name,
            "path": str(entry),
            "note_count": note_count,
            "last_modified": (
                datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                if mtime
                else None
            ),
        })
    return vaults


def list_notes(tier: str, vault_name: str | None = None) -> list[Path]:
    tp = tier_path(tier, vault_name)
    if tier in FREEFORM_TIERS:
        return [tp] if tp.is_file() else []
    if not tp.is_dir():
        return []
    return sorted(f for f in tp.iterdir() if f.suffix == ".md" and f.is_file())


def overlay_read(tier: str, vault_name: str | None = None) -> list[Path]:
    """Return notes from the project vault then the global vault (overlay)."""
    name = resolve_vault(vault_name)
    project_notes = list_notes(tier, name)

    if name == "global":
        return project_notes

    global_notes = list_notes(tier, "global")

    if tier in FREEFORM_TIERS:
        return project_notes + [g for g in global_notes if g not in project_notes]

    seen = {f.name for f in project_notes}
    return project_notes + [g for g in global_notes if g.name not in seen]
