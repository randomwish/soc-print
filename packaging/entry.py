"""Entry point of the standalone builds (PyInstaller).

`soc-print --askpass PROMPT` is the password helper ssh runs on Linux and macOS
(see socprint/ssh.py); anything else starts SoC Print as `python -m socprint` would.
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] == "--askpass":
    from socprint.askpass import main as askpass

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    sys.exit(askpass())

from socprint.__main__ import main

sys.exit(main())
