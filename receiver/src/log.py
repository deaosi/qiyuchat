"""日志：同时输出到控制台和按天滚动的文件。零依赖。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def setup(log_dir: str, level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    root.addHandler(sh)

    fh = TimedRotatingFileHandler(
        Path(log_dir) / "app.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    root.addHandler(fh)

    # 第三方噪音降级
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _configured = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
