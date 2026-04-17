# luma-mcp-tools

MCP tool servers for AI agents. 4 servers, 68 tools.

## Servers

| Server | Tools | Transport | Description |
|--------|-------|-----------|-------------|
| **GitPulse** | 6 | stdio | Workspace-aware git repo scanning, diagnosis, safe pulls, fork sync |
| **Obsidian Memory** | 15 | stdio | Vault-based persistent knowledge graph — notes, wikilinks, backlinks, multi-vault |
| **Web Search** | 2 | stdio | DuckDuckGo search + page reader with context-window-conscious defaults |
| **Luma** | 45 | HTTP+SSE | Finance, data, PDW tools (hosted remotely, not in this repo) |

## Prerequisites

| Dependency | Version | Check |
|------------|---------|-------|
| Python 3 | 3.10+ | `python3 --version` |
| Git | any | `git --version` |
| pip | any | `python3 -m pip --version` |

## Install

```bash
git clone https://github.com/ecuadralmft/luma-mcp-tools.git ~/luma-mcp-tools
cd ~/luma-mcp-tools
./install.sh
```

The installer:
1. Creates `~/.luma-memory/global/` vault directory (Obsidian Memory data root)
2. Creates isolated Python venvs in each server directory
3. Installs dependencies per server from `requirements.txt`
4. Generates `~/.kiro/settings/mcp.json` with absolute paths for your machine
5. Preserves any existing servers in `mcp.json` (e.g., `luma-mcp-server`)

After install, restart `kiro-cli` to pick up the new servers.

### Custom vault location

By default, the Obsidian Memory vault lives at `~/.luma-memory/`. To use a different path (e.g., a Windows-native path for Obsidian app compatibility):

```bash
# Edit the MEMORY_ROOT in mcp.json after install
# Example for WSL + Windows Obsidian:
"env": {"MEMORY_ROOT": "/mnt/c/Users/YourName/luma-memory"}
```

### Manual setup (without install.sh)

```bash
cd ~/luma-mcp-tools

# GitPulse
cd servers/gitpulse && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Obsidian Memory
cd ../obsidian && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Web Search
cd ../web && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Then manually add to `~/.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "gitpulse": {
      "command": "/absolute/path/to/luma-mcp-tools/servers/gitpulse/.venv/bin/python3",
      "args": ["/absolute/path/to/luma-mcp-tools/servers/gitpulse/server.py"],
      "env": {}
    },
    "obsidian": {
      "command": "/absolute/path/to/luma-mcp-tools/servers/obsidian/.venv/bin/python3",
      "args": ["/absolute/path/to/luma-mcp-tools/servers/obsidian/run.py"],
      "env": {"MEMORY_ROOT": "/home/you/.luma-memory"}
    },
    "web": {
      "command": "/absolute/path/to/luma-mcp-tools/servers/web/.venv/bin/python3",
      "args": ["/absolute/path/to/luma-mcp-tools/servers/web/run.py"],
      "env": {"WEB_SEARCH_BACKEND": "ddg"}
    }
  }
}
```

## Server Details

### GitPulse (6 tools)

| Tool | Description |
|------|-------------|
| `scan_workspace` | Discover all git repos in a directory tree |
| `diagnose_workspace` | Full health check — uncommitted changes, ahead/behind, stale branches |
| `repo_status` | Deep status of a single repo |
| `pull_repo` | Safe pull with strategy selection (ff-only, merge, rebase) |
| `sync_fork` | Sync a forked repo with upstream |
| `sync_report` | Dry-run comparison of local vs remote |

Dependencies: `mcp`, `typer`, `rich`

### Obsidian Memory (15 tools)

Vault-based persistent knowledge graph. Compatible with [Obsidian](https://obsidian.md/) — open the vault directory in Obsidian for a visual graph UI.

| Tool | Description |
|------|-------------|
| `memory_read` | Read a tier (project, decisions, learnings, active, glossary, blockers, all) |
| `memory_write` | Write to a tier (append or overwrite) |
| `memory_search` | Search across tiers |
| `memory_compact` | Deduplicate or prune old entries |
| `memory_status` | Tier counts and sizes |
| `memory_note_read` | Read a single note by ID with backlinks |
| `memory_note_write` | Create a new note with auto-generated ID |
| `memory_note_update` | Update an existing note |
| `memory_link` | Create a wikilink between notes |
| `memory_backlinks` | Find all notes linking to a given note |
| `memory_graph` | Neighborhood graph traversal (configurable depth) |
| `vault_list` | List all vaults with note counts |
| `vault_switch` | Switch active vault |
| `memory_migrate_v1` | Migrate flat-file memory to vault format |
| `discover_tools` | Discover all MCP tools across servers |

Dependencies: `mcp`, `pyyaml`

### Web Search (2 tools)

| Tool | Description |
|------|-------------|
| `web_search` | Search via DuckDuckGo (3 results default, ~600 chars) |
| `web_read` | Fetch URL + extract clean text (4000 chars default) |

Dependencies: `mcp`, `httpx`, `beautifulsoup4`, `ddgs`

Knowledge hierarchy (check in this order before using web search):
1. Memory → 2. Codebase → 3. Luma tools → 4. Web search

## Directory Structure

```
luma-mcp-tools/
├── servers/
│   ├── gitpulse/
│   │   ├── server.py
│   │   ├── requirements.txt
│   │   └── .venv/              ← created by install.sh
│   ├── obsidian/
│   │   ├── run.py              ← entry point
│   │   ├── server.py           ← MCP tool definitions
│   │   ├── note.py             ← Note model + wikilink parsing
│   │   ├── vault.py            ← Vault resolution + multi-vault
│   │   ├── index.py            ← Link graph + backlinks
│   │   ├── compat.py           ← v1 backward-compatible API
│   │   ├── migrate.py          ← v1 → v2 migration
│   │   ├── requirements.txt
│   │   └── .venv/
│   └── web/
│       ├── run.py              ← entry point
│       ├── server.py           ← search + reader
│       ├── requirements.txt
│       └── .venv/
├── install.sh
├── README.md
└── LICENSE
```

## Using with Pickle Rick

This repo provides the MCP tools. The orchestrator lives in a separate repo:

```bash
# Install tools first
git clone https://github.com/ecuadralmft/luma-mcp-tools.git ~/luma-mcp-tools
cd ~/luma-mcp-tools && ./install.sh

# Then install the orchestrator
git clone https://github.com/ecuadralmft/luma-pickle-rick.git ~/luma-pickle-rick
cd ~/luma-pickle-rick && ./install.sh

# Launch
kiro-cli chat --agent pickle-rick --trust-all-tools
```

See [luma-pickle-rick](https://github.com/ecuadralmft/luma-pickle-rick) for full orchestrator documentation.

## License

MIT
