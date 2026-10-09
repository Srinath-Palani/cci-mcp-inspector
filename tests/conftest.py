import sys
from pathlib import Path

# Ensure the project root (parent of tests/) is importable so `src` and
# `api_bridge` resolve regardless of how the test is launched.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
