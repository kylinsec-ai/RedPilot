"""attempt 终态写入的单源性与 canonical 契约。

契约:ghost_contracts.CANONICAL_TERMINAL_STATUSES 含 `interrupted`,即**所有**终态
(含 lease 过期回收与 evaluation 取消)都必须写出 `attempt.completed` 事件。

修复前:这两条路径各自用裸 UPDATE 写 attempts.status,绕过事件写入 —— 后果是
core 的 platform_events 序列缺一条终态,而 obs 侧 run 永远停在 running
(权威终态缺失,且 relay 无权关它)。本文件锁定"终态只有一个写者"这一不变量。
"""

from __future__ import annotations

import time

from ghost.control.models import parse_task_config
from ghost.control.provisioner import ProvisionedContainer
from ghost.control.service import ChallengeService
from ghost.control.store import Store

TASK_TOKEN = "task-terminal"


class StubProvisioner:
    def start(self, task_token, challenge) -> ProvisionedContainer:
        return ProvisionedContainer(addresses=("10.0.0.1:80",), container_id="cid")

    def stop(self, task_token, challenge, container_id=None) -> None:
        return None


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "control.sqlite3"))
    service = ChallengeService(store, StubProvisioner(), max_active_challenges=3)
    service.seed(parse_task_config({"token": TASK_TOKEN, "challenges": [
        {"unique_code": "web-01", "description": "t", "flags": ["flag{t}"],
         "container_addr": ["10.0.0.1:80"]},
    ]}))
    return store


def _claimed(store: Store, *, lease_seconds: int = 300):
    store.register_worker("worker-1")
    store.create_evaluation(TASK_TOKEN, project_id="proj", idempotency_key="idem-1")
    assignment = store.claim_job("worker-1", lease_seconds=lease_seconds)
    assert assignment is not None
    return assignment


def _terminal_payloads(store: Store, attempt_id: str) -> list[dict]:
    """attempt_events() 返回完整事件信封;终态载荷在 e["payload"] 内。"""
    return [e["payload"] for e in store.attempt_events(attempt_id)
            if e.get("event_type") == "attempt.completed"]


def test_lease_expiry_emits_canonical_completed(tmp_path):
    """lease 过期回收必须发 canonical 终态事件(修复前:裸 UPDATE,无事件)。"""
    store = _store(tmp_path)
    assignment = _claimed(store, lease_seconds=1)
    attempt_id = assignment.attempt_id

    # 把租约推到过去,再由 sweeper 回收
    store._connection.execute(
        "UPDATE jobs SET lease_expires_at = ?", (time.time() - 10.0,))
    reaped = store.reap_expired_leases()
    assert reaped == [attempt_id]

    terminals = [p for p in _terminal_payloads(store, attempt_id)
                 if p["status"] == "interrupted"]
    assert terminals, "lease 过期未发 canonical 终态事件"
    assert terminals[-1]["reason"] == "lease_expired"
    # job 回到 pending,可被重新领取
    assert store.claim_job("worker-1") is not None


def test_cancel_evaluation_emits_canonical_completed(tmp_path):
    """取消 evaluation 也必须为在飞 attempt 发 canonical 终态事件。"""
    store = _store(tmp_path)
    assignment = _claimed(store)
    attempt_id = assignment.attempt_id

    store.cancel_evaluation(assignment.evaluation_id)

    terminals = [p for p in _terminal_payloads(store, attempt_id)
                 if p["status"] == "interrupted"]
    assert terminals, "取消 evaluation 未发 canonical 终态事件"
    assert terminals[-1]["reason"] == "evaluation_canceled"


def test_complete_attempt_still_emits_exactly_once(tmp_path):
    """统一写者未破坏既有语义:complete 仍是恰好一条终态事件,重放幂等。"""
    store = _store(tmp_path)
    assignment = _claimed(store)
    attempt_id = assignment.attempt_id

    first = store.complete_attempt(attempt_id, "worker-1", assignment.lease_id,
                                   status="solved", solved=True, flags_found=1)
    assert first["idempotent"] is False
    second = store.complete_attempt(attempt_id, "worker-1", assignment.lease_id,
                                    status="solved", solved=True, flags_found=1)
    assert second["idempotent"] is True

    terminals = _terminal_payloads(store, attempt_id)
    assert len(terminals) == 1, f"终态事件应恰好一条,实得 {len(terminals)}"
    assert terminals[0]["status"] == "solved"


def test_reap_is_idempotent(tmp_path):
    """重复回收不产生第二条终态事件。"""
    store = _store(tmp_path)
    assignment = _claimed(store, lease_seconds=1)
    store._connection.execute(
        "UPDATE jobs SET lease_expires_at = ?", (time.time() - 10.0,))
    assert len(store.reap_expired_leases()) == 1
    assert store.reap_expired_leases() == []

    terminals = [p for p in _terminal_payloads(store, assignment.attempt_id)
                 if p["status"] == "interrupted"]
    assert len(terminals) == 1


def test_expired_lease_heartbeat_rejected(tmp_path):
    """过期租约的 heartbeat 必须被拒 —— 否则租约可"复活",job 永久钉在 running。"""
    store = _store(tmp_path)
    assignment = _claimed(store, lease_seconds=1)
    store._connection.execute(
        "UPDATE jobs SET lease_expires_at = ?", (time.time() - 10.0,))
    assert store.heartbeat_assignment(
        assignment.attempt_id, "worker-1", assignment.lease_id, 300) is False
    # 未过期时正常续租
    store._connection.execute(
        "UPDATE jobs SET lease_expires_at = ?", (time.time() + 300.0,))
    assert store.heartbeat_assignment(
        assignment.attempt_id, "worker-1", assignment.lease_id, 300) is True


# ── seed 幂等与配置漂移告警 ──

def test_seed_does_not_apply_flag_changes_but_reports_drift(tmp_path, caplog):
    """已存在任务的 flag 变更不会生效,但必须**响亮告警**而非静默沿用旧哈希。

    flag 是评分密钥:改 JSON 明文后重启若静默沿用旧 SHA-256,平台会拒绝正确答案,
    而运维看不到任何信号 —— 这是本项要消除的隐患。
    """
    import logging

    store = _store(tmp_path)
    service = ChallengeService(store, StubProvisioner(), max_active_challenges=3)
    assert service.store.task_config_drift(
        parse_task_config({"token": TASK_TOKEN, "challenges": []})[0]) != []

    changed = parse_task_config({"token": TASK_TOKEN, "challenges": [
        {"unique_code": "web-01", "description": "t", "flags": ["flag{CHANGED}"],
         "container_addr": ["10.0.0.1:80"]},
    ]})
    with caplog.at_level(logging.ERROR, logger="ghost.control.service"):
        service.seed(changed)
    assert "flags changed" in caplog.text, "flag 变更未告警"

    # 旧哈希仍在(刻意不自动改):原 flag 仍可提交成功
    assert store.get_challenge(TASK_TOKEN, "web-01") is not None
    result = service.submit(TASK_TOKEN, "web-01", "flag{t}")
    assert result["correct"] is True
    # 新 flag 不生效
    assert service.submit(TASK_TOKEN, "web-01", "flag{CHANGED}")["correct"] is False


def test_seed_of_unchanged_config_reports_no_drift(tmp_path):
    """配置未变时不得刷告警。"""
    store = _store(tmp_path)
    service = ChallengeService(store, StubProvisioner(), max_active_challenges=3)
    same = parse_task_config({"token": TASK_TOKEN, "challenges": [
        {"unique_code": "web-01", "description": "t", "flags": ["flag{t}"],
         "container_addr": ["10.0.0.1:80"]},
    ]})
    assert store.task_config_drift(same[0]) == []


def test_reregister_does_not_downgrade_busy_or_draining(tmp_path):
    """重复注册不得把在飞 worker 降级为 idle。

    driver 在收到 worker_not_registered 后会重新注册;若无条件写 idle,
    正在解题的 worker 在控制面上就显示为空闲,误导调度与运维判断。
    """
    store = _store(tmp_path)
    assignment = _claimed(store)
    assert store.list_workers()[0]["status"] == "busy"

    store.register_worker("worker-1")
    assert store.list_workers()[0]["status"] == "busy", "在飞 worker 被降级为 idle"

    # 排空意图同样不能被重注册取消
    store.worker_heartbeat("worker-1", "draining")
    assert store.list_workers()[0]["status"] == "draining"
    store.register_worker("worker-1")
    assert store.list_workers()[0]["status"] == "draining"

    # 租约结束后重注册 → 正常回到 idle
    store.complete_attempt(assignment.attempt_id, "worker-1", assignment.lease_id,
                           status="done")
    store.register_worker("worker-1")
    assert store.list_workers()[0]["status"] == "idle"
