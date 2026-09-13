import sys
from pathlib import Path

# Make `import atlas_edge` work without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
