"""PyInstaller entry point for 答题卡扫描识别.

multiprocessing.freeze_support() MUST run before any other import
so that spawn-mode worker processes start correctly in frozen builds.
"""

import multiprocessing

multiprocessing.freeze_support()

from scan_answer.ui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
