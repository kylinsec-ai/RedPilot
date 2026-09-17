"""worker 身份的一致性：`_worker_id()` / `_status_path()` / 热重载标记必须是同一个人。

这一条是**回归守卫**。修之前，同一个进程里"我是谁"有两个答案：

    单体形态（compose 的设定：ADAPTER_WORKER_ID=1，ADAPTER_WORKER_COUNT 没设）
      _worker_id()   → 0    （1 % 1）
      _status_path() → /work/status/worker-1.json

后果全是静默的：状态文件写在 worker-1.json，而 mutex 认领时间戳、观测上报的
worker_id、`touch .reload.wid{N}` 全用 0；重启后还会读回自己写的 `claim_ts` 按
`wid=0` 判胜负。装配层的 `_warn_if_worker_id_drift` 也看不见 —— 它比的是
`WORKER_ID` 尾号与 `ADAPTER_WORKER_ID`，漂移发生在装配层之后的取模里。

根因是**取模的条件写错了**：`% count` 只在真的在做分片（count > 1）时才有意义，
count <= 1 时取模等于把 ID 抹成 0。
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager

from redpilot.worker import orchestrator as o


@contextmanager
def _env(**kw):
    """设置/清除一组环境变量（值为 None 表示删除）。"""
    saved = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class WorkerIdentityTests(unittest.TestCase):
    # 三种部署形态下 (ADAPTER_WORKER_ID, ADAPTER_WORKER_COUNT) → 期望 wid
    FLEET = [("worker-1 monitor", "0", "3", 0),
             ("worker-2", "1", "3", 1),
             ("worker-3", "2", "3", 2)]
    MONOLITH = [("worker (单容器)", "1", None, 1)]   # ← 修之前是 0
    BARE = [("裸跑（两个都不设）", None, None, 0)]

    def test_every_topology_agrees_on_who_i_am(self):
        for label, wid, count, want in self.FLEET + self.MONOLITH + self.BARE:
            with self.subTest(label):
                with _env(ADAPTER_WORKER_ID=wid, ADAPTER_WORKER_COUNT=count,
                          HOSTNAME="redpilot-worker"):
                    got = o._worker_id()
                    self.assertEqual(got, want,
                                     f"{label}: ADAPTER_WORKER_ID={wid!r} "
                                     f"COUNT={count!r} → 期望 wid={want}")
                    # 两条路径必须指向同一个 worker —— 这是本文件存在的理由
                    self.assertEqual(
                        os.path.basename(o._status_path()), f"worker-{got}.json",
                        f"{label}: status 文件的序数与 _worker_id() 不一致")

    def test_monolith_keeps_its_configured_index(self):
        """单体形态：ID=1 / COUNT 未设 → 必须是 1，不能被 `% 1` 抹成 0。

        抹成 0 的连锁后果：状态文件仍是 worker-1.json（`_status_path` 原来自己
        推一份），但 `_unknown_bucket` 会把它当"协调者不解题"而**丢弃所有无法
        分类的题** —— 单体形态下那些题永远不会被任何人接走。
        """
        with _env(ADAPTER_WORKER_ID="1", ADAPTER_WORKER_COUNT=None,
                  HOSTNAME="redpilot-worker"):
            self.assertEqual(o._worker_id(), 1)
            self.assertEqual(os.path.basename(o._status_path()), "worker-1.json")
            # 单容器没有 monitor 可让位，unknown 题必须归它
            self.assertTrue(o._unknown_bucket("some-unknown-code", 1, 1),
                            "单体形态下 unknown 题被判成'不归我' —— 那题就没人接了")

    def test_fleet_unknown_challenges_still_skip_the_monitor(self):
        """舰队形态不变：wid 0 是 monitor（不解题），unknown 只在 1..count-1 之间分流。

        ID=0 与 COUNT=3 的组合下取模本来就是恒等的（0 % 3 == 0），
        所以这一条在修改前后都必须成立 —— 它锁的是"没改坏"。
        """
        with _env(ADAPTER_WORKER_ID="0", ADAPTER_WORKER_COUNT="3"):
            self.assertEqual(o._worker_id(), 0)
            for code in ("a", "b", "c", "d"):
                self.assertFalse(o._unknown_bucket(code, 0, 3),
                                 "monitor 不该接 unknown 题")

    def test_reload_marker_uses_the_same_index(self):
        """`touch .reload.wid{N}` 的 N 必须与 status/mutex 用的一致。

        修之前单体形态是 `.reload.wid0`（monitor 的序号，而它并不存在）——
        运维照 README 敲 `.reload.wid1` 会没人消费。
        """
        with _env(ADAPTER_WORKER_ID="1", ADAPTER_WORKER_COUNT=None,
                  HOSTNAME="redpilot-worker"):
            path = os.path.join(os.getenv("ADAPTER_WORKDIR", "/work"),
                                f".reload.wid{o._worker_id()}")
            self.assertTrue(path.endswith(".reload.wid1"), path)

    def test_hostname_fallback_still_works(self):
        """两个环境变量都缺时退回 HOSTNAME 尾号减一（`--scale` 场景）。"""
        with _env(ADAPTER_WORKER_ID=None, ADAPTER_WORKER_COUNT="3",
                  HOSTNAME="tsecbench-adapter-adapter-2"):
            self.assertEqual(o._worker_id(), 1)

    def test_shard_gate_is_only_active_when_count_exceeds_one(self):
        """`count <= 1` 不取模；`count > 1` 才取模 —— 这是修复的关键条件。

        取模本身没错，错的是无条件取模。用 ID=5 把两种条件区分开：
          count=1 → 5（保持原值，status 写 worker-5.json）
          count=3 → 2（5 % 3，桶号）
        """
        with _env(ADAPTER_WORKER_ID="5", HOSTNAME="redpilot-worker"):
            with _env(ADAPTER_WORKER_COUNT="1"):
                self.assertEqual(o._worker_id(), 5)
                self.assertEqual(os.path.basename(o._status_path()), "worker-5.json")
            with _env(ADAPTER_WORKER_COUNT="3"):
                self.assertEqual(o._worker_id(), 2)


class ShardPartitionTests(unittest.TestCase):
    """分片语义未变：舰队三容器下切片互不重叠且并集完整。"""

    class _C:
        def __init__(self, code):
            self.unique_code = code

    def test_shards_are_disjoint_and_cover_everything(self):
        challenges = [self._C(f"c{i}") for i in range(9)]
        seen = []
        for wid in ("0", "1", "2"):
            with _env(ADAPTER_WORKER_ID=wid, ADAPTER_WORKER_COUNT="3"):
                seen.append({c.unique_code for c in o._worker_shard(list(challenges))})
        self.assertEqual(set().union(*seen), {c.unique_code for c in challenges},
                         "有题没有归属 —— 分片漏了")
        self.assertEqual(sum(len(s) for s in seen), 9, "切片之间有重叠")

    def test_no_sharding_when_count_is_one(self):
        challenges = [self._C(f"c{i}") for i in range(9)]
        with _env(ADAPTER_WORKER_ID="1", ADAPTER_WORKER_COUNT="1"):
            self.assertEqual(len(o._worker_shard(list(challenges))), 9)


if __name__ == "__main__":
    unittest.main()
