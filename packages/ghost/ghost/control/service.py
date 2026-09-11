"""Business rules for the authenticated Challenges API."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from threading import RLock
from typing import Iterable


from ghost.control.errors import APIError, challenge_not_found, duplicate, invalid_state, resource_unavailable, task_not_found
from ghost.control.models import TaskDefinition, discounted_score
from ghost.control.provisioner import ContainerProvisioner, ProvisionerError, ProvisionedContainer, ResourceUnavailable
from ghost.control.store import ChallengeRow, DuplicateSubmission, Store

logger = logging.getLogger("ghost.control.service")



class ChallengeService:
    def __init__(self, store: Store, provisioner: ContainerProvisioner, max_active_challenges: int = 3) -> None:
        if max_active_challenges < 1:
            raise ValueError("max_active_challenges must be at least 1")
        self.store = store
        self.provisioner = provisioner
        self.max_active_challenges = max_active_challenges
        self._lock = RLock()

    def seed(self, tasks: Iterable[TaskDefinition], *, ignore_existing: bool = True) -> None:
        """启动期种子:已存在的任务**不重建**(保护容器状态与已看提示)。

        代价是任务配置的变更不会自动生效 —— 尤其 flag:库里存的是 SHA-256,改 JSON 里的
        明文后重启会静默沿用旧哈希,而 flag 正是评分密钥。这里主动比对并存差异响亮
        告警,把静默的正确性隐患变成看得见的事件(是否推进由运维决定:改 token 或清库)。
        """
        for task in tasks:
            created = self.store.insert_task(task, ignore_existing=ignore_existing)
            if not created:
                drift = self.store.task_config_drift(task)
                if drift:
                    logger.error(
                        "task %s already exists but its configuration differs (%s): "
                        "seed does NOT apply changes — flags/scores in the DB keep their "
                        "original values. Use a new task token or re-seed the DB.",
                        task.token, "; ".join(drift))

    def create_task(self, task: TaskDefinition) -> None:
        self.store.insert_task(task)

    def stop_task(self, token: str) -> bool:
        with self._lock:
            return self.store.stop_task(token)

    def authenticate(self, token: str | None) -> str:
        if not token or not self.store.has_task(token):
            raise task_not_found()
        if not self.store.task_is_active(token):
            raise invalid_state()
        return token

    def _get_challenge(self, token: str, unique_code: str) -> ChallengeRow:
        row = self.store.get_challenge(token, unique_code)
        if row is None:
            raise challenge_not_found()
        return row

    @staticmethod
    def _challenge_payload(row: ChallengeRow, submissions: tuple) -> dict:
        correct_count = len(submissions)
        definition = row.definition
        status = row.container_status
        return {
            "unique_code": definition.unique_code,
            "description": definition.description,
            "difficulty": definition.difficulty,
            "level": definition.level,
            "total_score": definition.total_score,
            "flag_count": len(definition.flags),
            "correct_flag_count": correct_count,
            "is_completed": correct_count == len(definition.flags),
            "container_status": status,
            "container_addr": list(row.container_addresses) if status == "available" else [],
        }

    def list_challenges(self, token: str) -> list[dict]:
        self.authenticate(token)
        return [
            self._challenge_payload(row, self.store.submissions(token, row.definition.unique_code))
            for row in self.store.list_challenges(token)
        ]

    def start(self, token: str, unique_code: str) -> dict:
        # 两段式:先在锁内做鉴权+预留(纯 DB 事务),再在锁外调 provisioner
        # side effect,最后回锁内落终态。provisioner(Docker CLI/网络)可能阻塞数秒,
        # 绝不在持有服务锁时执行,避免拖住同任务其他题目的 start/submit/close。
        # 预留已把行置 pending(计入 max_active),锁外窗口内并发 start 同题只会看到
        # transitioning,不会超限多起。
        with self._lock:
            self.authenticate(token)
            row = self._get_challenge(token, unique_code)
            reservation = self.store.reserve_container(token, unique_code, self.max_active_challenges)
            if reservation == "missing":
                raise challenge_not_found()
            if reservation == "available":
                current = self.store.get_challenge(token, unique_code)
                if current is None or not current.container_addresses:
                    raise resource_unavailable("Challenge instance has no address")
                return {"unique_code": unique_code, "container_addr": list(current.container_addresses)}
            if reservation == "transitioning":
                raise resource_unavailable("Challenge instance is still transitioning")
            if reservation == "limit":
                raise invalid_state("Maximum active challenge limit reached")

        def _revert_to_stopped() -> None:
            """启动失败回滚:pending → stopped。

            带 CAS:pending 期间并发 close 已把状态推到 stop_pending/stopped 时,
            这里**不再写** —— 否则会用 stopped 抹掉并发成功启动那条路径写下的
            addresses/container_id(容器泄漏且句柄丢失)。
            """
            if not self.store.set_container(token, unique_code, "stopped",
                                            expect=("pending",)):
                logger.warning(
                    "start rollback skipped for %s/%s: container state advanced concurrently",
                    token, unique_code)

        try:
            provisioned = self.provisioner.start(token, row.definition)
            if not isinstance(provisioned, ProvisionedContainer) or not provisioned.addresses:
                raise ResourceUnavailable("Challenge instance returned no address")
            addresses = tuple(address for address in provisioned.addresses if address)
            if not addresses:
                raise ResourceUnavailable("Challenge instance returned no address")
        except ResourceUnavailable as exc:
            _revert_to_stopped()
            raise resource_unavailable(str(exc)) from exc
        except ProvisionerError as exc:
            _revert_to_stopped()
            raise resource_unavailable(str(exc)) from exc
        except Exception as exc:
            _revert_to_stopped()
            raise APIError(500, "internal_error", "Internal server error") from exc
        if not self.store.set_container(token, unique_code, "available", addresses,
                                        provisioned.container_id, expect=("pending",)):
            # 容器已起但我们输掉了状态迁移(并发 close 抢先):必须回收刚起的容器,
            # 否则它既不在任何可关闭状态里、也没人持有句柄 —— 永久泄漏。
            logger.warning("start raced with close for %s/%s: reclaiming container %s",
                           token, unique_code, provisioned.container_id)
            with contextlib.suppress(Exception):
                self.provisioner.stop(token, row.definition, provisioned.container_id)
            raise resource_unavailable("Challenge instance state changed concurrently")
        return {"unique_code": unique_code, "container_addr": list(addresses)}

    def hint(self, token: str, unique_code: str) -> dict:
        with self._lock:
            self.authenticate(token)
            row = self._get_challenge(token, unique_code)
            submissions = self.store.submissions(token, unique_code)
            if len(submissions) == len(row.definition.flags):
                raise invalid_state("Challenge has already been completed")
            if not row.hint_viewed:
                self.store.mark_hint_viewed(token, unique_code)
            return {"unique_code": unique_code, "hint": row.definition.hint}

    def submit(self, token: str, unique_code: str, flag: str) -> dict:
        with self._lock:
            self.authenticate(token)
            row = self._get_challenge(token, unique_code)
            current = self.store.submissions(token, unique_code)
            submitted_indices = {submission.flag_index for submission in current}
            digest = hashlib.sha256(flag.encode("utf-8")).hexdigest()
            matches = tuple(item for item in row.definition.flags if item.value_hash == digest)
            matched = next((item for item in matches if item.index not in submitted_indices), None)
            if matched is None and matches:
                raise duplicate()
            if matched is None:
                return {
                    "correct": False,
                    "awarded": 0,
                    "cumulative_score": sum(item.awarded for item in current),
                    "correct_flag_count": len(current),
                    "total_flag_count": len(row.definition.flags),
                    "matched_flag_index": None,
                }
            awarded = discounted_score(matched.score, row.definition.hint_cost_radio, row.hint_viewed)
            try:
                self.store.record_submission(token, unique_code, matched.index, awarded)
            except DuplicateSubmission as exc:
                raise duplicate() from exc
            updated = self.store.submissions(token, unique_code)
            return {
                "correct": True,
                "awarded": awarded,
                "cumulative_score": sum(item.awarded for item in updated),
                "correct_flag_count": len(updated),
                "total_flag_count": len(row.definition.flags),
                "matched_flag_index": matched.index,
            }

    def close(self, token: str, unique_code: str) -> dict:
        # 两段式:锁内标记 stop_pending(纯 DB),锁外调 provisioner.stop side effect,
        # 再回锁内落 stopped/available。Docker rm -f 可能阻塞,绝不持锁执行。
        with self._lock:
            self.authenticate(token)
            row = self._get_challenge(token, unique_code)
            if row.container_status not in {"available", "pending", "stop_pending"}:
                # 已不在可关闭状态(stopped/空):幂等返回,且不做无条件盲写。
                return {"unique_code": unique_code, "closed": True}
            if not self.store.set_container(token, unique_code, "stop_pending",
                                            row.container_addresses, row.container_id,
                                            expect=("available", "pending")):
                # 并发者已推进(另一 close 抢到 / 已 stopped):幂等返回。
                # 关键是不再调 provisioner —— 避免重复 docker rm -f。
                return {"unique_code": unique_code, "closed": True}
            definition, container_id = row.definition, row.container_id
            saved_addresses = row.container_addresses

        def _rollback_to_available() -> None:
            """停止失败回滚:stop_pending → available。

            带 CAS:并发者已把状态推离 stop_pending 时不写,免得覆盖更晚的进度。
            """
            if not self.store.set_container(token, unique_code, "available",
                                            saved_addresses, container_id,
                                            expect=("stop_pending",)):
                logger.warning(
                    "close rollback skipped for %s/%s: container state advanced concurrently",
                    token, unique_code)

        try:
            self.provisioner.stop(token, definition, container_id)
        except ResourceUnavailable as exc:
            _rollback_to_available()
            raise resource_unavailable(str(exc)) from exc
        except ProvisionerError as exc:
            _rollback_to_available()
            raise APIError(500, "internal_error", "Internal server error") from exc
        except Exception as exc:
            _rollback_to_available()
            raise APIError(500, "internal_error", "Internal server error") from exc
        if not self.store.set_container(token, unique_code, "stopped",
                                        expect=("stop_pending", "stopped")):
            # 容器确实已停,但状态被并发推进(如另一 close 已落 stopped):
            # 记录而非覆盖 —— 结果等价(都是停)。
            logger.warning("close finalize raced for %s/%s; leaving state to the winner",
                           token, unique_code)
        return {"unique_code": unique_code, "closed": True}
