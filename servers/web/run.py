#!/usr/bin/env python3
"""Launch wrapper — importable without cwd tricks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.server import mcp

mcp.run(transport="stdio")
