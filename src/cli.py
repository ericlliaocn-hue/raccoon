"""Raccoon CLI 入口点（pip install 后的 `raccoon` 命令）"""

import sys
from pathlib import Path


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from raccoon import main as _main
    _main()


if __name__ == "__main__":
    main()
