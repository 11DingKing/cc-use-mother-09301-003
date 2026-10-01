"""启动高校资源分类配置服务端（自动恢复未完成批次）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from allocation.__main__ import main

if __name__ == "__main__":
    main()
