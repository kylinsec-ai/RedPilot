"""判据包 —— 评估面对外的门面,worker 桥接只认这里。

为什么判据要"注入"而不是 import:`packages/ghost/**` 不许 import `ghost_worker`
(红线,见 `eval/__init__.py` 与 `tests/test_ghost_purity.py`),而"这条命令算不算
联网"的判据长在 worker 侧(那儿才有靶场策略与白名单)。两条路:在评估面抄一份,
或者只声明接口、让外面注入。选后者 —— 抄一份就是判据分叉的起点:两份实现各自
演化,不报错、不告警,只是结论开始互相矛盾。代价是判据可能缺席,而这个代价
用 `skipped` 显式付掉(见 `deterministic.grade` 的 overall 取法)。

    from redpilot.eval.graders import CHECKS, grade   # 判据注册表与评分入口
    from redpilot.eval.graders import Predicates      # worker 侧的注入点(实现留给外面)
"""

from .deterministic import CHECKS, CheckResult, GradeReport, Predicates, grade

__all__ = ["CHECKS", "CheckResult", "GradeReport", "Predicates", "grade"]
