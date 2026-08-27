"""只读评测基线：数据契约、历史任务收集与原子输出。

不导入数据库模块，公开类型全部为纯数据契约；收集器和 IO 按需单独导入。
"""

from coderus.evaluation.models import (
    EVALUATION_CONTRACT_VERSION,
    BaselineReport,
    BaselineSelection,
    BaselineSummary,
    TaskAnnotation,
    TaskBaseline,
    TaskOutcome,
)

__all__ = [
    "EVALUATION_CONTRACT_VERSION",
    "BaselineReport",
    "BaselineSelection",
    "BaselineSummary",
    "TaskAnnotation",
    "TaskBaseline",
    "TaskOutcome",
]
