from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.release_update import pack_dist


def main() -> None:
    pack_dist(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
