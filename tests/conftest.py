# Lets the tests import the scripts (inventory, get_routes, ...) by name,
# the same way the scripts import each other.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
