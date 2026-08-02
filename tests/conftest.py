import sys
from pathlib import Path

# Make scripts/ importable without packaging it. Keeps the seeder a plain
# script for operators while still letting tests exercise its pure functions.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
