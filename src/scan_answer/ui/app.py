"""图形界面入口：scan-answer-gui"""
from __future__ import annotations

import sys

from scan_answer.ui.main_window import run_app


def main() -> int:
    return run_app()


if __name__ == "__main__":
    sys.exit(main())
