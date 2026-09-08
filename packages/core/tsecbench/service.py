"""Business rules for the authenticated Challenges API."""

from __future__ import annotations

import hashlib
from threading import RLock
from typing import Iterable

from .errors import APIError, challenge_not_found, duplicate, invalid_state, resource_unavailable, task_not_found
from .models import TaskDefinition, discounted_score
from .provisioner import ContainerProvisioner, ProvisionerError, ProvisionedContainer, ResourceUnavailable
from .store import ChallengeRow, DuplicateSubmission, Store



class ChallengeService:
    def __init__(self, store: Store, provisioner: ContainerProvisioner, max_active_challenges: int = 3) -> None:
        if max_active_challenges < 1:
            raise ValueError("max_active_challenges must be at least 1")
        self.store = store
        self.provisioner = provisioner
        self.max_active_challenges = max_active_challenges
        self._lock = RLock()

    def seed(self, tasks: Iterable[TaskDefinition], *, ignore_existing: bool = True) -> None:
        for task in tasks:
            self.store.insert_task(task, ignore_existing=ignore_existing)

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

            try:
                provisioned = self.provisioner.start(token, row.definition)
                if not isinstance(provisioned, ProvisionedContainer) or not provisioned.addresses:
                    raise ResourceUnavailable("Challenge instance returned no address")
                addresses = tuple(address for address in provisioned.addresses if address)
                if not addresses:
                    raise ResourceUnavailable("Challenge instance returned no address")
            except ResourceUnavailable as exc:
                self.store.set_container(token, unique_code, "stopped")
                raise resource_unavailable(str(exc)) from exc
            except ProvisionerError as exc:
                self.store.set_container(token, unique_code, "stopped")
                raise resource_unavailable(str(exc)) from exc
            except Exception as exc:
                self.store.set_container(token, unique_code, "stopped")
                raise APIError(500, "internal_error", "Internal server error") from exc
            self.store.set_container(token, unique_code, "available", addresses, provisioned.container_id)
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
        with self._lock:
            self.authenticate(token)
            row = self._get_challenge(token, unique_code)
            if row.container_status in {"available", "pending", "stop_pending"}:
                self.store.set_container(token, unique_code, "stop_pending", row.container_addresses, row.container_id)
                try:
                    self.provisioner.stop(token, row.definition, row.container_id)
                except ResourceUnavailable as exc:
                    self.store.set_container(token, unique_code, "available", row.container_addresses, row.container_id)
                    raise resource_unavailable(str(exc)) from exc
                except ProvisionerError as exc:
                    self.store.set_container(token, unique_code, "available", row.container_addresses, row.container_id)
                    raise APIError(500, "internal_error", "Internal server error") from exc
                except Exception as exc:
                    self.store.set_container(token, unique_code, "available", row.container_addresses, row.container_id)
                    raise APIError(500, "internal_error", "Internal server error") from exc
            self.store.set_container(token, unique_code, "stopped")
            return {"unique_code": unique_code, "closed": True}
