"""容器状态迁移的并发正确性(start/close 交错)。

背景:start 与 close 都是两段式 —— 锁内改状态 → **锁外**调 provisioner(秒级
side effect)→ 回锁内落终态。锁外那段时间状态可被并发推进,若回写不带前置条件,
两个真实后果:
  1. start 输给 close 后仍写 available:客户端被告知"已关闭",行却是 available,
     刚起的容器无人持有句柄 → 永久泄漏;
  2. start 失败路径无条件写 stopped:抹掉并发成功 start 写下的
     addresses/container_id → 容器泄漏且句柄丢失。

本文件用可控阻塞的 provisioner 精确构造这两种交错。
"""

from __future__ import annotations

import threading

from ghost.control.models import parse_task_config
from ghost.control.provisioner import ProvisionedContainer
from ghost.control.service import ChallengeService
from ghost.control.store import Store

TASK_TOKEN = "task-cas"


class BlockingProvisioner:
    """start 可被闸门阻塞;记录 start/stop 调用以便断言补偿行为。"""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.block = False
        self.started: list[str] = []
        self.stopped: list[str | None] = []
        self.fail_start = False

    def start(self, task_token, challenge) -> ProvisionedContainer:
        self.started.append(challenge.unique_code)
        if self.block:
            self.gate.wait(10.0)
        if self.fail_start:
            raise RuntimeError("provisioner exploded")
        return ProvisionedContainer(addresses=("10.0.0.9:80",), container_id="cid-new")

    def stop(self, task_token, challenge, container_id=None) -> None:
        self.stopped.append(container_id)


def _service(tmp_path, provisioner) -> ChallengeService:
    store = Store(str(tmp_path / "control.sqlite3"))
    service = ChallengeService(store, provisioner, max_active_challenges=3)
    service.seed(parse_task_config({"token": TASK_TOKEN, "challenges": [
        {"unique_code": "web-01", "description": "cas", "flags": ["flag{cas}"],
         "container_addr": ["10.0.0.1:80"]},
    ]}))
    return service


def _status(service: ChallengeService) -> str:
    return service.store.get_challenge(TASK_TOKEN, "web-01").container_status


def test_start_losing_race_to_close_reclaims_container(tmp_path):
    """start 输掉 CAS 时必须回收刚起的容器,并报冲突(而非留下泄漏的 available)。"""
    prov = BlockingProvisioner()
    prov.block = True
    service = _service(tmp_path, prov)

    results: dict = {}

    def do_start() -> None:
        try:
            results["start"] = service.start(TASK_TOKEN, "web-01")
        except Exception as exc:  # noqa: BLE001 — 断言用
            results["start_error"] = exc

    t = threading.Thread(target=do_start, daemon=True)
    t.start()
    # 等 start 进入锁外 provisioner 调用(pending 已落库)
    for _ in range(200):
        if prov.started:
            break
        threading.Event().wait(0.01)
    assert prov.started, "start 未进入 provisioner"

    # 此刻 pending;并发 close 抢先(它只看得到 pending,会置 stop_pending/stopped)
    service.close(TASK_TOKEN, "web-01")
    assert _status(service) == "stopped"

    prov.gate.set()  # 放行 start
    t.join(timeout=10.0)

    assert "start_error" in results, "输掉 CAS 的 start 必须报错而非静默成功"
    assert "cid-new" in prov.stopped, "刚起的容器未被回收 —— 永久泄漏"
    assert _status(service) == "stopped", "start 不得把状态写回 available"


def test_start_failure_does_not_clobber_concurrent_success(tmp_path):
    """start 失败回滚带 CAS:状态已被并发推进时不得盲写 stopped。"""
    prov = BlockingProvisioner()
    service = _service(tmp_path, prov)

    # 直接把状态推到 available(模拟并发成功启动已落库)
    service.store.set_container(TASK_TOKEN, "web-01", "available",
                               ("10.0.0.7:80",), "cid-other", expect=("stopped",))

    # 失败路径的 CAS 以 pending 为前提 → 这里必然不匹配
    wrote = service.store.set_container(TASK_TOKEN, "web-01", "stopped",
                                        expect=("pending",))
    assert wrote is False
    row = service.store.get_challenge(TASK_TOKEN, "web-01")
    assert row.container_status == "available"
    assert row.container_addresses == ("10.0.0.7:80",)
    assert row.container_id == "cid-other", "并发成功的句柄被抹掉了"


def test_double_close_is_idempotent_and_calls_provisioner_once(tmp_path):
    """并发 close:只有一个走到 provisioner.stop,另一个幂等返回(不重复 rm -f)。"""
    prov = BlockingProvisioner()
    service = _service(tmp_path, prov)
    service.start(TASK_TOKEN, "web-01")
    assert _status(service) == "available"
    before = len(prov.stopped)

    first = service.close(TASK_TOKEN, "web-01")
    second = service.close(TASK_TOKEN, "web-01")  # 已 stopped:早退分支

    assert first == {"unique_code": "web-01", "closed": True}
    assert second == {"unique_code": "web-01", "closed": True}
    assert len(prov.stopped) - before == 1, "重复调用了 provisioner.stop"


def test_set_container_cas_reports_conflict(tmp_path):
    """CAS 契约本身:expect 不匹配 → False 且不写。"""
    prov = BlockingProvisioner()
    service = _service(tmp_path, prov)
    store = service.store
    assert store.set_container(TASK_TOKEN, "web-01", "available",
                               ("10.0.0.8:80",), "cid-1", expect=("pending",)) is False
    assert _status(service) == "stopped"
    assert store.set_container(TASK_TOKEN, "web-01", "pending") is True  # 无条件写
    assert store.set_container(TASK_TOKEN, "web-01", "available",
                               ("10.0.0.8:80",), "cid-1", expect=("pending",)) is True
    assert _status(service) == "available"
