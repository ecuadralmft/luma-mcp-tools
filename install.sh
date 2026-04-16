#!/bin/bash
# luma-mcp-tools installer — sets up MCP servers for AI agents
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KIRO_SETTINGS="$HOME/.kiro/settings"
MEMORY_ROOT="$HOME/.luma-memory"

echo "🔧 Installing luma-mcp-tools..."
echo ""

# Create memory root
mkdir -p "$MEMORY_ROOT/global"
echo "  ✓ Memory root: $MEMORY_ROOT"

# Set up gitpulse venv
echo "  Setting up GitPulse..."
cd "$SCRIPT_DIR/servers/gitpulse"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "  ✓ GitPulse ready"

# Set up memory venv
echo "  Setting up Obsidian Memory..."
cd "$SCRIPT_DIR/servers/memory"
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
# Remove old gitpulse/memory entries, keep everything else
servers.pop('gitpulse', None)
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
    'args': ['-m', 'server'],
    'cwd': '$SCRIPT_DIR/servers/gitpulse',
    'env': {}
}
existing['memory'] = {
    'command': '$SCRIPT_DIR/servers/memory/.venv/bin/python3',
    'args': ['-m', 'server'],
    'cwd': '$SCRIPT_DIR/servers/memory',
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
echo "  • GitPulse  → $SCRIPT_DIR/servers/gitpulse/"
echo "  • Memory    → $SCRIPT_DIR/servers/memory/"
echo "  • Vault root: $MEMORY_ROOT"
echo ""
echo "Restart kiro-cli to pick up the new MCP servers."
echo ""
echo "To migrate existing v1 memory, run in a kiro-cli session:"
echo "  memory_migrate_v1(source_dir=\"path/to/conductor/memory/\")"
