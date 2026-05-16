"""日志工具。"""

from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    """返回带稳定命名空间的 NodeSync logger。"""

    return logging.getLogger(f"nodesync.{name}")
