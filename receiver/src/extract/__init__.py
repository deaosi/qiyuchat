"""问答抽取与聚类。这一层完全不调大模型。

输入：raw_message（逐句对话）+ raw_session（元数据）
输出：qa_pair（谁/什么时间/问了什么/原话术）+ q_cluster（高频问题簇）

三步走：
    抽取  qa.py       客户问 → 客服答，配对成记录
    归一  normalize.py 去寒暄、去语气词、数字占位、全角转半角
    聚类  cluster.py   字符 2-gram Jaccard + 并查集，纯标准库
"""

from .qa import extract_pairs
from .normalize import normalize
from .cluster import cluster_questions
from .aggregate import by_customer, by_group, overall

__all__ = [
    "extract_pairs",
    "normalize",
    "cluster_questions",
    "by_customer",
    "by_group",
    "overall",
]
