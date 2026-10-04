"""讲解员成长等级后端。

领域模块：
- ``rules``：成长规则版本（不可变、仅追加）。
- ``ledger``：积分事件的复式分录、冲回、更正与余额快照。
- ``engine``：等级评定、保护期、权益授予/收回。
- ``jobs``：可安全重跑的定时晋级任务。
- ``service`` / ``api``：应用门面与 HTTP 接口。
"""
from __future__ import annotations

from .service import GrowthService
from .store import Store

__all__ = ["GrowthService", "Store"]
