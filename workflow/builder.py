"""Entry-point script for the builder CLI."""
from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "builder" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from builder.cli import main as cli_main


def main() -> int:
    """Delegate to the builder CLI entry point."""
    return cli_main(sys.argv[1:])


if __name__ == "__main__":  # pragma: no cover - exercised via integration tests
    raise SystemExit(main())
