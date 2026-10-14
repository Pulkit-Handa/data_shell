"""Make the in-repo provider package importable when tests run without installing."""

import sys
from pathlib import Path

_PROVIDER_ROOT = Path(__file__).resolve().parent
if str(_PROVIDER_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROVIDER_ROOT))
