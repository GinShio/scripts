"""Entry-point script for the git-stack CLI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# We need to make sure 'workflow.git_stack.src' can be imported.
# We assume this script is located at .../workflow/git_stack.py
# So the root for packages is the parent of this script (.../)
_ROOT = Path(__file__).resolve().parent.parent

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from workflow.git_stack.cli import main
except ImportError:
    # If the above fails, it might be because 'workflow' is not treated as a package
    # or we are in a different structure.
    # Try adding the workflow directory to path to allow 'import git_stack'
    _WORKFLOW_DIR = Path(__file__).resolve().parent
    if str(_WORKFLOW_DIR) not in sys.path:
        sys.path.insert(0, str(_WORKFLOW_DIR))

    try:
        from git_stack.cli import main
    except ImportError:
        # Last resort fallback (rare)
        raise

if __name__ == "__main__":
    main()
