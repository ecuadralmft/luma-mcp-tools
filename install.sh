#!/bin/bash
# luma-mcp-tools installer — sets up MCP servers for AI agents
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KIRO_SETTINGS="$HOME/.kiro/settings"
MEMORY_ROOT="$HOME/.luma-memory"

echo "🔧 Installing luma-mcp-tools..."
echo ""

# Create memory vault root
mkdir -p "$MEMORY_ROOT/global"
echo "  ✓ Vault root: $MEMORY_ROOT"

# Set up gitpulse venv
echo "  Setting up GitPulse..."
cd "$SCRIPT_DIR/servers/gitpulse"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "  ✓ GitPulse ready"

# Set up obsidian venv
echo "  Setting up Obsidian Memory..."
cd "$SCRIPT_DIR/servers/obsidian"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "  ✓ Obsidian Memory ready"

# Generate mcp.json
mkdir -p "$KIRO_SETTINGS"
MCP_JSON="$KIRO_SETTINGS/mcp.json"

# Read existing mcp.json to preserve other servers (e.g., luma-mcp-server)
if [ -f "$MCP_JSON" ]; then
    EXISTING=$(python3 -c "
import json
with open('$MCP_JSON') as f:
    cfg = json.load(f)
servers = cfg.get('mcpServers', {})
servers.pop('gitpulse', None)
servers.pop('obsidian', None)
servers.pop('memory', None)
print(json.dumps(servers))
")
else
    EXISTING="{}"
fi

python3 -c "
import json
existing = json.loads('$EXISTING')
existing['gitpulse'] = {
    'command': '$SCRIPT_DIR/servers/gitpulse/.venv/bin/python3',
    'args': ['$SCRIPT_DIR/servers/gitpulse/server.py'],
    'env': {}
}
existing['obsidian'] = {
    'command': '$SCRIPT_DIR/servers/obsidian/.venv/bin/python3',
    'args': ['-m', 'obsidian'],
    'cwd': '$SCRIPT_DIR/servers',
    'env': {'MEMORY_ROOT': '$MEMORY_ROOT'}
}
cfg = {'mcpServers': existing}
with open('$MCP_JSON', 'w') as f:
    json.dump(cfg, f, indent=2)
print('  ✓ Generated ' + '$MCP_JSON')
"

echo ""
echo "✅ Installation complete!"
echo ""
echo "Servers configured:"
echo "  • GitPulse        → $SCRIPT_DIR/servers/gitpulse/"
echo "  • Obsidian Memory → $SCRIPT_DIR/servers/obsidian/"
echo "  • Vault root      → $MEMORY_ROOT"
echo ""
echo "Restart kiro-cli to pick up the new MCP servers."
