"""Entry point: python3 -m memory (from parent dir) or via install.sh config."""
from .server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
