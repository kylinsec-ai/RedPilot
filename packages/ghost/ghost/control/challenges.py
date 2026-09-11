"""Challenges/评分业务外观:participant/agent 业务规则的唯一入口。

只委托 ChallengeService,不触碰 evaluations/jobs/attempts 调度表;
调度侧入口见 scheduling.py。api.py challenges 路由组只经本外观调用。
"""

from __future__ import annotations

from typing import Iterable

from ghost.control.models import TaskDefinition
from ghost.control.service import ChallengeService


class ChallengeFacade:
    """题目业务外观(评分规则 + 靶场预留语义,不含调度状态)。"""

    def __init__(self, service: ChallengeService) -> None:
        self._service = service

    @property
    def service(self) -> ChallengeService:
        return self._service

    def seed(self, tasks: Iterable[TaskDefinition], *, ignore_existing: bool = True) -> None:
        self._service.seed(tasks, ignore_existing=ignore_existing)

    def authenticate(self, token: str | None) -> str:
        return self._service.authenticate(token)

    def list_challenges(self, token: str) -> list[dict]:
        return self._service.list_challenges(token)

    def start(self, token: str, unique_code: str) -> dict:
        return self._service.start(token, unique_code)

    def hint(self, token: str, unique_code: str) -> dict:
        return self._service.hint(token, unique_code)

    def submit(self, token: str, unique_code: str, flag: str) -> dict:
        return self._service.submit(token, unique_code, flag)

    def close(self, token: str, unique_code: str) -> dict:
        return self._service.close(token, unique_code)

    def stop_task(self, token: str) -> bool:
        return self._service.stop_task(token)
