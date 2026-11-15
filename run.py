#!/usr/bin/env python3
"""LocalAgent 启动入口：python run.py [--port N] [--workspace DIR] [--no-browser] [--browser] [--load]

真正的逻辑在 lagent/launcher.py（打包后的 LocalAgent.exe 走同一个入口）。
"""
import sys

if sys.version_info < (3, 10):
    sys.exit("需要 Python 3.10 或更高版本")

from lagent.launcher import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
