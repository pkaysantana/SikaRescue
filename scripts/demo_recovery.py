"""Run the seeded SK-10421 failed-payment recovery in the terminal (synthetic data, no LLM).

uv run python scripts/demo_recovery.py            # asks for approval
uv run python scripts/demo_recovery.py --approve  # approves non-interactively
"""

import sys

try:
    from sikarescue.cli.demo import main
except ImportError:
    sys.exit("Run inside the project environment:  uv run python scripts/demo_recovery.py")

if __name__ == "__main__":
    sys.exit(main())
