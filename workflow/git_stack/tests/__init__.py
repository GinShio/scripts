import os
import sys

# Ensure project root (scripts/) is in sys.path so 'workflow.git_stack.src...' imports work
# Expected path: .../scripts/workflow/git_stack/tests/__init__.py
# Root path: .../scripts/

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
