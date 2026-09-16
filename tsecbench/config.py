"""Runtime settings and task configuration loading."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import ConfigurationError, TaskDefinition, parse_task_config


@dataclass(frozen=True)
class Settings:
    database_path: str = "./data/tsecbench.sqlite3"
    config_path: str | None = None
    inline_config: str | None = None
    benchmark_base_url: str | None = None
    benchmark_token: str | None = None
    max_active_challenges: int = 3
    provisioner: str = "static"
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> Settings:
        dotenv_path = Path(env_file) if env_file is not None else Path(__file__).resolve().parent.parent / ".env"
        if dotenv_path.is_file():
            load_dotenv(dotenv_path=dotenv_path, override=False)

        database_path = str(Path(os.getenv("TSECBENCH_DB_PATH", os.getenv("TSECBENCH_DATABASE", cls.database_path))).expanduser())
        config_path = os.getenv("TSECBENCH_CONFIG")
        inline_config = os.getenv("TSECBENCH_TASKS_JSON")
        base_url_value = os.getenv("BENCHMARK_BASE_URL", "").strip()
        base_url = base_url_value or None
        token_value = os.getenv("BENCHMARK_TOKEN", "").strip()
        token = token_value or None
        try:
            max_active = int(os.getenv("TSECBENCH_MAX_ACTIVE_CHALLENGES", "3"))
            port = int(os.getenv("PORT", "8000"))
        except ValueError as exc:
            raise ConfigurationError("numeric environment settings are invalid") from exc
        if max_active < 1:
            raise ConfigurationError("TSECBENCH_MAX_ACTIVE_CHALLENGES must be at least 1")
        if port < 1 or port > 65535:
            raise ConfigurationError("PORT must be between 1 and 65535")
        return cls(
            database_path=database_path,
            config_path=config_path,
            inline_config=inline_config,
            benchmark_base_url=base_url,
            benchmark_token=token,
            max_active_challenges=max_active,
            provisioner=os.getenv("TSECBENCH_PROVISIONER", "static").lower(),
            host=os.getenv("HOST", "0.0.0.0"),
            port=port,
        )

    def load_tasks(self) -> tuple[TaskDefinition, ...]:
        raw: Any = None
        if self.config_path:
            raw = json.loads(Path(self.config_path).read_text(encoding="utf-8"))
        elif self.inline_config:
            raw = json.loads(self.inline_config)
        if self.benchmark_token and raw is not None:
            if isinstance(raw, dict) and "tasks" not in raw and "token" not in raw and "benchmark_token" not in raw and "challenges" in raw:
                raw = {**raw, "token": self.benchmark_token}
            elif isinstance(raw, list) and all(isinstance(item, dict) and "token" not in item and "benchmark_token" not in item for item in raw):
                raw = {"token": self.benchmark_token, "challenges": raw}
        tasks = parse_task_config(raw)
        # A token without an external catalog still creates a valid task. This is
        # useful for operators that provision challenge rows through Store.create_task.
        if not tasks and self.benchmark_token:
            tasks = (TaskDefinition(token=self.benchmark_token, challenges=()),)
        return tasks
