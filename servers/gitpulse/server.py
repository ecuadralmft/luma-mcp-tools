"""GitPulse — MCP server for workspace-aware git repo scanning, diagnosis, and safe sync."""

import fcntl
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gitpulse")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_IGNORE = {"node_modules", ".git", ".cache", "__pycache__", ".venv", "venv", "vendor", ".worktrees", ".gitpulse"}
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024  # 10 MB
CACHE_TTL_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _ws_root(path: str | None = None) -> Path:
    """Resolve workspace root: walk up from *path* (or cwd) to find nearest .git, else use path/cwd."""
    start = Path(path).resolve() if path else Path.cwd().resolve()
    cur = start
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return start


def _gpdir(ws: Path) -> Path:
    d = ws / ".gitpulse"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cache").mkdir(exist_ok=True)
    (d / "audit").mkdir(exist_ok=True)
    return d


def _ensure_gitignore(ws: Path) -> None:
    gi = ws / ".gitignore"
    entry = ".gitpulse/"
    if gi.exists():
        text = gi.read_text()
        if entry not in text.splitlines():
            with gi.open("a") as f:
                if not text.endswith("\n"):
                    f.write("\n")
                f.write(entry + "\n")
    else:
        gi.write_text(entry + "\n")


def _run(cmd: list[str], cwd: str | Path | None = None, timeout: int = 30) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"


def _git(args: list[str], cwd: str | Path | None = None, timeout: int = 30) -> tuple[int, str, str]:
    return _run(["git"] + args, cwd=cwd, timeout=timeout)


def _gh(args: list[str], cwd: str | Path | None = None, timeout: int = 30) -> tuple[int, str, str]:
    return _run(["gh"] + args, cwd=cwd, timeout=timeout)


def _audit(ws: Path, entry: dict) -> None:
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    log = _gpdir(ws) / "audit" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    with log.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(entry) + "\n")
        f.flush()
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _write_cache(ws: Path, name: str, data: Any) -> None:
    p = _gpdir(ws) / "cache" / f"{name}.json"
    p.write_text(json.dumps(data, indent=2, default=str))


def _read_cache(ws: Path, name: str, ttl: int = CACHE_TTL_SECONDS) -> Any | None:
    p = _gpdir(ws) / "cache" / f"{name}.json"
    if p.exists():
        try:
            if ttl and (time.time() - p.stat().st_mtime) > ttl:
                return None  # Stale
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _gh_auth_ok() -> tuple[bool, str]:
    rc, out, err = _gh(["auth", "status"])
    return rc == 0, (out or err)


def _fork_info(cwd: Path) -> dict:
    """Use gh to detect fork and upstream info."""
    rc, out, _ = _gh(["repo", "view", "--json", "isFork,parent,url,name,owner"], cwd=cwd)
    if rc != 0:
        return {"is_fork": False, "parent": None}
    try:
        data = json.loads(out)
        return {
            "is_fork": data.get("isFork", False),
            "parent": data.get("parent"),
        }
    except json.JSONDecodeError:
        return {"is_fork": False, "parent": None}


def _branches(cwd: Path) -> list[dict]:
    rc, out, _ = _git(["for-each-ref", "--format=%(refname:short) %(upstream:short) %(upstream:track)", "refs/heads/"], cwd=cwd)
    if rc != 0 or not out:
        return []
    branches = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        name = parts[0] if parts else ""
        tracking = parts[1] if len(parts) > 1 else ""
        track_info = parts[2] if len(parts) > 2 else ""
        ahead = behind = 0
        if "ahead" in track_info:
            try:
                ahead = int(track_info.split("ahead ")[1].split("]")[0].split(",")[0])
            except (IndexError, ValueError):
                pass
        if "behind" in track_info:
            try:
                behind = int(track_info.split("behind ")[1].split("]")[0])
            except (IndexError, ValueError):
                pass
        branches.append({"name": name, "tracking": tracking, "ahead": ahead, "behind": behind})
    return branches


def _current_branch(cwd: Path) -> tuple[str, bool]:
    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if rc != 0:
        return "", False
    detached = out == "HEAD"
    if detached:
        rc2, sha, _ = _git(["rev-parse", "--short", "HEAD"], cwd=cwd)
        return sha if rc2 == 0 else "HEAD", True
    return out, False


def _dirty_files(cwd: Path) -> list[dict]:
    rc, out, _ = _git(["status", "--porcelain"], cwd=cwd)
    if rc != 0 or not out:
        return []
    files = []
    for line in out.splitlines():
        if len(line) >= 4:
            files.append({"status": line[:2].strip(), "path": line[3:]})
    return files


def _stashes(cwd: Path) -> list[str]:
    rc, out, _ = _git(["stash", "list"], cwd=cwd)
    if rc != 0 or not out:
        return []
    return out.splitlines()


def _remotes(cwd: Path) -> list[dict]:
    rc, out, _ = _git(["remote", "-v"], cwd=cwd)
    if rc != 0 or not out:
        return []
    seen = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name = parts[0]
            if name not in seen:
                seen[name] = parts[1]
    return [{"name": n, "url": u} for n, u in seen.items()]


def _submodules(cwd: Path) -> list[dict]:
    rc, out, _ = _git(["submodule", "status"], cwd=cwd)
    if rc != 0 or not out:
        return []
    subs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        synced = not line.startswith("+") and not line.startswith("-")
        parts = line.lstrip("+-U").split(None, 1)
        sha = parts[0] if parts else ""
        path = parts[1].split()[0] if len(parts) > 1 else ""
        subs.append({"path": path, "actual_commit": sha, "synced": synced})
    return subs


def _find_repos(root: Path, ignore: set[str], found: list[Path] | None = None, _depth: int = 0, max_depth: int | None = None) -> list[Path]:
    if found is None:
        found = []
        # Check if root itself is a repo
        if (root / ".git").exists():
            found.append(root)
    if max_depth is not None and _depth >= max_depth:
        return found
    try:
        entries = sorted(root.iterdir())
    except PermissionError:
        return found
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name in ignore:
            continue
        if entry.name == ".git":
            continue
        try:
            has_git = (entry / ".git").exists()
        except PermissionError:
            continue
        if has_git:
            found.append(entry)
        _find_repos(entry, ignore, found, _depth + 1, max_depth)
    return found


def _repo_info(repo: Path, parent: Path | None = None) -> dict:
    branch, detached = _current_branch(repo)
    rems = _remotes(repo)
    is_sub = parent is not None
    fi = _fork_info(repo) if rems else {"is_fork": False, "parent": None}
    return {
        "path": str(repo),
        "remotes": rems,
        "branches": _branches(repo),
        "current_branch": branch,
        "detached": detached,
        "is_submodule": is_sub,
        "parent_repo": str(parent) if parent else None,
        "is_fork": fi["is_fork"],
        "upstream_remote": fi["parent"],
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def scan_workspace(
    path: str | None = None,
    max_depth: int | None = None,
    ignore_patterns: list[str] | None = None,
    detail: str = "minimal",
) -> dict:
    """Deep recursive scan to discover all git repos in a workspace. detail='minimal' returns paths + current branch only (fast). detail='full' includes branches, fork status, submodules (slower, makes gh API calls)."""
    t0 = time.monotonic()
    ws = _ws_root(path)
    _ensure_gitignore(ws)

    ignore = set(ignore_patterns) if ignore_patterns else set(DEFAULT_IGNORE)
    repos = _find_repos(ws, ignore, max_depth=max_depth)

    results = []
    if detail == "minimal":
        for rp in repos:
            branch, detached = _current_branch(rp)
            dirty = bool(_dirty_files(rp))
            results.append({
                "path": str(rp),
                "current_branch": branch,
                "detached": detached,
                "dirty": dirty,
            })
    else:
        # Full detail: branches, forks, submodules
        sub_parents: dict[str, Path] = {}
        for rp in repos:
            for sm in _submodules(rp):
                sp = (rp / sm["path"]).resolve()
                sub_parents[str(sp)] = rp

        for rp in repos:
            parent = sub_parents.get(str(rp))
            results.append(_repo_info(rp, parent))

    data = {
        "workspace_root": str(ws),
        "repos": results,
        "repo_count": len(results),
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "scan_duration_ms": round((time.monotonic() - t0) * 1000),
    }
    _write_cache(ws, "scan", data)
    _audit(ws, {"tool": "scan_workspace", "workspace": str(ws), "repos_found": len(results)})
    return data


@mcp.tool()
def diagnose_workspace(
    path: str | None = None,
    use_cache: bool = True,
    check_remotes: bool = False,
) -> dict:
    """Full health check across all discovered repos: uncommitted changes, ahead/behind, detached HEAD, stale branches, broken remotes, submodule drift, fork drift, large untracked, auth issues. Set check_remotes=True to verify remote connectivity (slow, makes network calls per remote)."""
    ws = _ws_root(path)
    cached = _read_cache(ws, "scan") if use_cache else None
    if cached:
        repo_paths = [Path(r["path"]) for r in cached.get("repos", [])]
    else:
        scan = scan_workspace(path)
        repo_paths = [Path(r["path"]) for r in scan.get("repos", [])]

    auth_ok, auth_msg = _gh_auth_ok()
    repo_reports = []
    summary = {"total_repos": len(repo_paths), "healthy": 0, "warnings": 0, "errors": 0}

    for rp in repo_paths:
        issues: list[dict] = []

        # 1. Uncommitted changes
        dirty = _dirty_files(rp)
        if dirty:
            issues.append({"type": "uncommitted_changes", "severity": "warning", "detail": f"{len(dirty)} dirty file(s)", "files": dirty})

        # 2. Ahead/behind
        branch, detached = _current_branch(rp)
        if not detached:
            rc, out, _ = _git(["rev-list", "--left-right", "--count", f"{branch}...@{{u}}"], cwd=rp)
            if rc == 0 and out:
                parts = out.split()
                ahead = int(parts[0]) if parts else 0
                behind = int(parts[1]) if len(parts) > 1 else 0
                if behind > 0:
                    issues.append({"type": "behind_remote", "severity": "warning", "detail": f"{behind} commit(s) behind remote"})
                if ahead > 0:
                    issues.append({"type": "ahead_of_remote", "severity": "info", "detail": f"{ahead} unpushed commit(s)"})

        # 3. Detached HEAD
        if detached:
            issues.append({"type": "detached_head", "severity": "warning", "detail": f"HEAD detached at {branch}"})

        # 4. Stale branches
        rc, out, _ = _git(["branch", "--merged", "HEAD"], cwd=rp)
        if rc == 0 and out:
            merged = [b.strip().lstrip("* ") for b in out.splitlines()]
            stale = [b for b in merged if b and b not in ("main", "master", branch)]
            if stale:
                issues.append({"type": "stale_branches", "severity": "info", "detail": f"{len(stale)} merged branch(es) could be deleted", "branches": stale})

        # 5. Broken remotes (opt-in, network calls)
        rems = _remotes(rp)
        if check_remotes:
            for rem in rems:
                rc2, _, _ = _git(["ls-remote", "--exit-code", rem["name"]], cwd=rp, timeout=10)
                if rc2 != 0:
                    issues.append({"type": "broken_remote", "severity": "error", "detail": f"Remote '{rem['name']}' ({rem['url']}) unreachable"})

        # 6. Submodule drift
        for sm in _submodules(rp):
            if not sm["synced"]:
                issues.append({"type": "submodule_drift", "severity": "warning", "detail": f"Submodule {sm['path']} out of sync"})

        # 7. Fork upstream drift
        fi = _fork_info(rp) if rems else {"is_fork": False}
        if fi.get("is_fork") and fi.get("parent"):
            rc3, _, _ = _git(["fetch", "upstream", "--dry-run"], cwd=rp, timeout=15)
            if rc3 == 0:
                rc4, out4, _ = _git(["rev-list", "--count", f"HEAD..upstream/{branch}"], cwd=rp)
                if rc4 == 0 and out4 and int(out4) > 0:
                    issues.append({"type": "fork_upstream_drift", "severity": "warning", "detail": f"{out4} commit(s) behind upstream"})

        # 8. Large untracked files
        rc5, out5, _ = _git(["ls-files", "--others", "--exclude-standard"], cwd=rp)
        if rc5 == 0 and out5:
            for f in out5.splitlines():
                fp = rp / f
                try:
                    if fp.is_file() and fp.stat().st_size > LARGE_FILE_THRESHOLD:
                        size_mb = round(fp.stat().st_size / (1024 * 1024), 1)
                        issues.append({"type": "large_untracked", "severity": "warning", "detail": f"{f} ({size_mb} MB)"})
                except OSError:
                    pass

        # 9. Auth issues
        if not auth_ok and rems:
            issues.append({"type": "gh_auth_issue", "severity": "error", "detail": auth_msg})

        has_err = any(i["severity"] == "error" for i in issues)
        has_warn = any(i["severity"] == "warning" for i in issues)
        if has_err:
            summary["errors"] += 1
        elif has_warn:
            summary["warnings"] += 1
        else:
            summary["healthy"] += 1

        repo_reports.append({"path": str(rp), "issues": issues})

    data = {"repos": repo_reports, "summary": summary, "diagnosed_at": datetime.now(timezone.utc).isoformat()}
    _write_cache(ws, "diagnosis", data)
    _audit(ws, {"tool": "diagnose_workspace", "summary": summary})
    return data


@mcp.tool()
def repo_status(repo_path: str) -> dict:
    """Deep status of a single repo: dirty files, ahead/behind, stash, submodules, fork info."""
    rp = Path(repo_path).resolve()
    if not (rp / ".git").exists():
        return {"error": f"Not a git repo: {rp}"}

    branch, detached = _current_branch(rp)
    ahead = behind = 0
    if not detached:
        rc, out, _ = _git(["rev-list", "--left-right", "--count", f"{branch}...@{{u}}"], cwd=rp)
        if rc == 0 and out:
            parts = out.split()
            ahead = int(parts[0]) if parts else 0
            behind = int(parts[1]) if len(parts) > 1 else 0

    fi = _fork_info(rp)
    return {
        "path": str(rp),
        "current_branch": branch,
        "detached": detached,
        "dirty_files": _dirty_files(rp),
        "ahead": ahead,
        "behind": behind,
        "stashes": _stashes(rp),
        "submodules": _submodules(rp),
        "remotes": _remotes(rp),
        "is_fork": fi.get("is_fork", False),
        "upstream": fi.get("parent"),
    }


@mcp.tool()
def sync_report(repo_path: str, include_upstream: bool = True) -> dict:
    """Dry-run comparison of local vs remote (and upstream for forks). Does NOT modify the working tree."""
    rp = Path(repo_path).resolve()
    if not (rp / ".git").exists():
        return {"error": f"Not a git repo: {rp}"}

    ws = _ws_root(str(rp))
    branch, detached = _current_branch(rp)

    # Fetch without modifying working tree
    _git(["fetch", "--all", "--quiet"], cwd=rp, timeout=30)

    def _commits_between(a: str, b: str) -> list[dict]:
        rc, out, _ = _git(["log", f"{a}..{b}", "--format=%H||%s||%an||%aI"], cwd=rp)
        if rc != 0 or not out:
            return []
        commits = []
        for line in out.splitlines():
            parts = line.split("||", 3)
            if len(parts) == 4:
                commits.append({"sha": parts[0], "message": parts[1], "author": parts[2], "date": parts[3]})
        return commits

    tracking = ""
    if not detached:
        rc, t, _ = _git(["rev-parse", "--abbrev-ref", f"{branch}@{{u}}"], cwd=rp)
        if rc == 0:
            tracking = t

    behind_commits = _commits_between(f"HEAD", tracking) if tracking else []
    ahead_commits = _commits_between(tracking, "HEAD") if tracking else []

    # Fast-forward check
    ff_possible = False
    if tracking and behind_commits and not ahead_commits:
        ff_possible = True
    elif tracking and not behind_commits:
        ff_possible = True

    # Conflict likelihood
    conflicts_likely = bool(ahead_commits and behind_commits)

    # Upstream for forks
    upstream_behind: list[dict] = []
    if include_upstream:
        fi = _fork_info(rp)
        if fi.get("is_fork"):
            _git(["fetch", "upstream", "--quiet"], cwd=rp, timeout=30)
            upstream_behind = _commits_between("HEAD", f"upstream/{branch}")

    data = {
        "path": str(rp),
        "local_ref": branch,
        "remote_ref": tracking,
        "commits_behind": behind_commits,
        "commits_ahead": ahead_commits,
        "upstream_behind": upstream_behind,
        "fast_forward_possible": ff_possible,
        "conflicts_likely": conflicts_likely,
    }
    _audit(ws, {"tool": "sync_report", "repo": str(rp), "behind": len(behind_commits), "ahead": len(ahead_commits)})
    return data


@mcp.tool()
def pull_repo(
    repo_path: str | None = None,
    branch: str | None = None,
    strategy: str = "ff_only",
    stash_first: bool = False,
    batch: list[str] | None = None,
    confirmed: bool = False,
) -> dict:
    """Safe pull with strategy selection. Strategies: ff_only, merge, rebase, force. Set confirmed=True to approve destructive ops."""
    targets = [Path(p).resolve() for p in batch] if batch else [Path(repo_path).resolve()] if repo_path else []
    if not targets:
        return {"error": "Provide repo_path or batch list"}

    ws = _ws_root(str(targets[0]))

    # Force always requires confirmation
    if strategy == "force" and not confirmed:
        return {
            "confirmation_required": True,
            "message": "Force pull will overwrite local changes. Call again with confirmed=True to proceed.",
            "repos": [str(t) for t in targets],
            "strategy": strategy,
        }

    results = []
    for rp in targets:
        if not (rp / ".git").exists():
            results.append({"path": str(rp), "success": False, "result": "Not a git repo"})
            continue

        cur_branch, _ = _current_branch(rp)
        target_branch = branch or cur_branch

        # Stash if requested
        stashed = False
        if stash_first:
            dirty = _dirty_files(rp)
            if dirty:
                rc, _, _ = _git(["stash", "push", "-m", "gitpulse-auto-stash"], cwd=rp)
                stashed = rc == 0

        # Build pull command
        if strategy == "ff_only":
            cmd = ["pull", "--ff-only", "origin", target_branch]
        elif strategy == "merge":
            cmd = ["pull", "--no-rebase", "origin", target_branch]
        elif strategy == "rebase":
            cmd = ["pull", "--rebase", "origin", target_branch]
        elif strategy == "force":
            _git(["fetch", "origin", target_branch], cwd=rp)
            cmd = ["reset", "--hard", f"origin/{target_branch}"]
        else:
            results.append({"path": str(rp), "success": False, "result": f"Unknown strategy: {strategy}"})
            continue

        rc, out, err = _git(cmd, cwd=rp, timeout=60)

        # Count changes from git output
        files_changed = 0
        commits_pulled = 0
        combined = out + " " + err
        # Parse "X files changed" pattern
        fm = re.search(r'(\d+)\s+files?\s+changed', combined)
        if fm:
            files_changed = int(fm.group(1))
        # Count commits by checking shortstat or log
        if rc == 0 and strategy != "force":
            rc_c, out_c, _ = _git(["rev-list", "--count", f"{target_branch}@{{1}}..{target_branch}"], cwd=rp)
            if rc_c == 0 and out_c.strip().isdigit():
                commits_pulled = int(out_c.strip())

        # Pop stash if we stashed
        warnings = []
        if stashed:
            rc2, _, err2 = _git(["stash", "pop"], cwd=rp)
            if rc2 != 0:
                warnings.append(f"Stash pop failed: {err2}. Stash preserved.")

        success = rc == 0
        result_msg = out if success else err

        # If conflict detected, return options
        if not success and ("conflict" in err.lower() or "conflict" in out.lower()):
            _git(["merge", "--abort"], cwd=rp)
            if stashed:
                pass  # stash still exists
            results.append({
                "path": str(rp),
                "success": False,
                "result": "Conflicts detected",
                "conflicts": True,
                "options": [
                    "Retry with strategy='rebase'",
                    "Retry with strategy='force' (will overwrite local)",
                    "Retry with stash_first=True",
                    "Resolve manually",
                ],
            })
            continue

        results.append({
            "path": str(rp),
            "success": success,
            "result": result_msg,
            "commits_pulled": commits_pulled,
            "files_changed": files_changed,
            "strategy_used": strategy,
            "warnings": warnings,
        })

    _audit(ws, {"tool": "pull_repo", "repos": [str(t) for t in targets], "strategy": strategy, "results": [r.get("success") for r in results]})
    return {"success": all(r.get("success") for r in results), "repos": results}


@mcp.tool()
def sync_fork(
    repo_path: str,
    strategy: str = "merge",
    branch: str | None = None,
    confirmed: bool = False,
) -> dict:
    """Sync a forked repo with its upstream. Strategies: merge, rebase. Always requires confirmation."""
    rp = Path(repo_path).resolve()
    if not (rp / ".git").exists():
        return {"error": f"Not a git repo: {rp}"}

    ws = _ws_root(str(rp))
    fi = _fork_info(rp)
    if not fi.get("is_fork"):
        return {"error": "This repo is not a fork (per GitHub)"}

    cur_branch, _ = _current_branch(rp)
    target_branch = branch or cur_branch

    # Check if upstream remote exists, add if not
    rems = _remotes(rp)
    has_upstream = any(r["name"] == "upstream" for r in rems)
    if not has_upstream and fi.get("parent"):
        parent_url = fi["parent"].get("url", "")
        if parent_url:
            _git(["remote", "add", "upstream", parent_url], cwd=rp)

    # Fetch upstream
    rc, _, err = _git(["fetch", "upstream"], cwd=rp, timeout=30)
    if rc != 0:
        return {"error": f"Failed to fetch upstream: {err}"}

    # Count commits to sync
    rc2, out2, _ = _git(["rev-list", "--count", f"HEAD..upstream/{target_branch}"], cwd=rp)
    commits_to_sync = int(out2) if rc2 == 0 and out2 else 0

    if not confirmed:
        return {
            "confirmation_required": True,
            "message": f"Will {strategy} {commits_to_sync} commit(s) from upstream/{target_branch}. Call again with confirmed=True.",
            "repo": str(rp),
            "strategy": strategy,
            "commits_to_sync": commits_to_sync,
        }

    if strategy == "merge":
        rc3, out3, err3 = _git(["merge", f"upstream/{target_branch}"], cwd=rp)
    elif strategy == "rebase":
        rc3, out3, err3 = _git(["rebase", f"upstream/{target_branch}"], cwd=rp)
    else:
        return {"error": f"Unknown strategy: {strategy}"}

    success = rc3 == 0
    conflicts = not success and "conflict" in (out3 + err3).lower()
    if conflicts:
        _git(["merge", "--abort"] if strategy == "merge" else ["rebase", "--abort"], cwd=rp)

    data = {
        "success": success,
        "upstream_remote": "upstream",
        "commits_synced": commits_to_sync if success else 0,
        "conflicts": conflicts,
        "result": out3 if success else err3,
    }
    _audit(ws, {"tool": "sync_fork", "repo": str(rp), "strategy": strategy, "success": success})
    return data


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
