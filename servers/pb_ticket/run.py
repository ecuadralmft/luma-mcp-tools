#!/usr/bin/env python3
"""Launch wrapper — importable without cwd tricks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pb_ticket.server import mcp  # noqa: E402

mcp.run(transport="stdio")
