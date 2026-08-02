import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Make the three source trees importable without packaging any of them. The
# skill ships as files copied into someone else's repository, so it must stay
# importable as plain scripts -- a package layout here would be a layout the
# contribution target does not have.
for path in (ROOT / "scripts",
             ROOT / "eval",
             ROOT / "skills" / "opensearch-skills" / "observability" / "unclosed" / "scripts"):
    sys.path.insert(0, str(path))
