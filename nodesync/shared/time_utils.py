"""时间工具。"""

from __future__ import annotations

import time


def now_ts() -> float:
    """返回当前 Unix 时间戳。"""

    return time.time()
