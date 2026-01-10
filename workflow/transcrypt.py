"""Entry-point script for the transcrypt CLI."""
from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from transcrypt.src.cli import main as cli_main


def main() -> int:
    """Delegate to the transcrypt CLI entry point."""
    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
