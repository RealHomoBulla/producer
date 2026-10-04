"""Make the toolchain importable for every test, whatever order the files run in."""
import sys
from pathlib import Path

TOOLS = str(Path(__file__).resolve().parents[1])
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
