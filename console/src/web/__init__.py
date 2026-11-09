"""本地看板：纯标准库 HTTP 服务 + 单页前端，零依赖、零外网。"""

from .api import build_summary, customers, groups, pairs, questions, trend
from .server import serve

__all__ = ["serve", "build_summary", "questions", "groups", "customers",
           "pairs", "trend"]
