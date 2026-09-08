"""Domain models and configuration normalization for TSecBench."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import floor, isfinite
from typing import Any, Iterable, Mapping


class ConfigurationError(ValueError):
    """Raised when a task/challenge configuration is invalid."""


def _as_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field} must be an integer") from exc
    if result < minimum:
        raise ConfigurationError(f"{field} must be at least {minimum}")
    return result


def _as_ratio(value: Any, field: str = "hint_cost_radio") -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field} must be a number") from exc
    if not isfinite(result) or result < 0 or result > 1:
        raise ConfigurationError(f"{field} must be between 0 and 1")
    return result


def _parse_expiry(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if not isinstance(value, str):
        raise ConfigurationError("expires_at must be an ISO-8601 string or timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigurationError("expires_at must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _address_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        values = value
    else:
        raise ConfigurationError("container_addr must be a string or array of strings")
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ConfigurationError("container_addr entries must be strings")
        address = item.strip()
        if address:
            result.append(address)
    return tuple(result)


@dataclass(frozen=True)
class FlagDefinition:
    """A flag's public index, secret digest, and base score."""

    index: int
    value_hash: str
    score: int

    @classmethod
    def from_value(cls, index: int, value: str, score: int) -> "FlagDefinition":
        if not isinstance(value, str) or not value:
            raise ConfigurationError("flag values must be non-empty strings")
        return cls(index=index, value_hash=hashlib.sha256(value.encode("utf-8")).hexdigest(), score=score)

    def as_json(self) -> dict[str, Any]:
        return {"index": self.index, "value_hash": self.value_hash, "score": self.score}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "FlagDefinition":
        try:
            index = _as_int(value["index"], "flag.index")
            digest = str(value["value_hash"])
            score = _as_int(value["score"], "flag.score")
        except KeyError as exc:
            raise ConfigurationError("stored flag definition is missing a field") from exc
        if len(digest) != 64:
            raise ConfigurationError("flag.value_hash must be a SHA-256 digest")
        return cls(index=index, value_hash=digest, score=score)


@dataclass(frozen=True)
class ChallengeDefinition:
    unique_code: str
    description: str | None
    difficulty: str
    level: int
    total_score: int
    flags: tuple[FlagDefinition, ...]
    hint: str | None
    hint_cost_radio: float
    container_addr: tuple[str, ...]
    image: str | None = None
    container_port: int | None = None
    docker_network: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ChallengeDefinition":
        unique_code = str(raw.get("unique_code", "")).strip()
        if not unique_code:
            raise ConfigurationError("challenge.unique_code is required")
        difficulty = str(raw.get("difficulty", "unknown"))
        level = _as_int(raw.get("level", 0), "challenge.level")
        hint = raw.get("hint")
        if hint is not None:
            hint = str(hint)
        description = raw.get("description")
        if description is not None:
            description = str(description)

        raw_flags = raw.get("flags", ())
        if not isinstance(raw_flags, Iterable) or isinstance(raw_flags, (str, bytes, Mapping)):
            raise ConfigurationError("challenge.flags must be an array")
        raw_flags = list(raw_flags)
        if not raw_flags:
            raise ConfigurationError("challenge.flags must contain at least one flag")

        requested_total = raw.get("total_score")
        total_score = _as_int(requested_total, "challenge.total_score") if requested_total is not None else None
        explicit_scores: list[int | None] = []
        values: list[str] = []
        indices: list[int] = []
        for position, item in enumerate(raw_flags):
            if isinstance(item, str):
                value = item
                explicit_score = None
                index = position
            elif isinstance(item, Mapping):
                value = item.get("value", item.get("flag"))
                explicit_score = item.get("score")
                index = _as_int(item.get("index", position), "flag.index")
            else:
                raise ConfigurationError("each challenge flag must be a string or object")
            if not isinstance(value, str) or not value:
                raise ConfigurationError("flag values must be non-empty strings")
            values.append(value)
            indices.append(index)
            explicit_scores.append(None if explicit_score is None else _as_int(explicit_score, "flag.score"))
        if len(set(indices)) != len(indices):
            raise ConfigurationError("flag indices must be unique")

        if total_score is None:
            total_score = sum(score for score in explicit_scores if score is not None)
            if not any(score is not None for score in explicit_scores):
                total_score = len(values)
        if any(score is None for score in explicit_scores):
            explicit_total = sum(score for score in explicit_scores if score is not None)
            remaining = total_score - explicit_total
            missing = sum(score is None for score in explicit_scores)
            if remaining < 0:
                raise ConfigurationError("explicit flag scores exceed total_score")
            base, remainder = divmod(remaining, missing)
            scores = []
            for score in explicit_scores:
                if score is not None:
                    scores.append(score)
                else:
                    scores.append(base + (1 if remainder else 0))
                    remainder = max(0, remainder - 1)
        else:
            scores = [score for score in explicit_scores if score is not None]
            if sum(scores) != total_score:
                raise ConfigurationError("flag scores must add up to total_score")
        flags = tuple(FlagDefinition.from_value(index, value, score) for index, value, score in zip(indices, values, scores))

        ratio = _as_ratio(raw.get("hint_cost_radio", raw.get("hint_cost_ratio", 0)))
        addresses = _address_list(raw.get("container_addr", raw.get("container_addresses", raw.get("addresses"))))
        image = raw.get("image")
        image = str(image).strip() if image else None
        container_port = raw.get("container_port")
        if container_port is not None:
            container_port = _as_int(container_port, "challenge.container_port", minimum=1)
        docker_network = raw.get("docker_network")
        docker_network = str(docker_network).strip() if docker_network else None
        return cls(
            unique_code=unique_code,
            description=description,
            difficulty=difficulty,
            level=level,
            total_score=total_score,
            flags=flags,
            hint=hint,
            hint_cost_radio=ratio,
            container_addr=addresses,
            image=image,
            container_port=container_port,
            docker_network=docker_network,
        )


@dataclass(frozen=True)
class TaskDefinition:
    token: str
    challenges: tuple[ChallengeDefinition, ...]
    expires_at: str | None = None
    state: str = "active"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TaskDefinition":
        token = str(raw.get("token", raw.get("benchmark_token", ""))).strip()
        if not token:
            raise ConfigurationError("task.token is required")
        raw_challenges = raw.get("challenges", ())
        if not isinstance(raw_challenges, Iterable) or isinstance(raw_challenges, (str, bytes, Mapping)):
            raise ConfigurationError("task.challenges must be an array")
        challenges = tuple(ChallengeDefinition.from_mapping(item) for item in raw_challenges)
        if len({challenge.unique_code for challenge in challenges}) != len(challenges):
            raise ConfigurationError("challenge unique_code values must be unique within a task")
        state = str(raw.get("state", "active"))
        if state not in {"active", "stopped", "expired"}:
            raise ConfigurationError("task.state must be active, stopped, or expired")
        return cls(token=token, challenges=challenges, expires_at=_parse_expiry(raw.get("expires_at")), state=state)


def parse_task_config(raw: Any) -> tuple[TaskDefinition, ...]:
    """Parse either {\"tasks\": [...]} or a task array/object."""
    if raw is None:
        return ()
    if isinstance(raw, Mapping) and "tasks" in raw:
        raw = raw["tasks"]
    elif isinstance(raw, Mapping):
        raw = (raw,)
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, Mapping)):
        raise ConfigurationError("task configuration must be an object or array")
    parsed: list[TaskDefinition] = []
    for item in raw:
        if isinstance(item, TaskDefinition):
            parsed.append(item)
        elif isinstance(item, Mapping):
            parsed.append(TaskDefinition.from_mapping(item))
        else:
            raise ConfigurationError("each task must be an object")
    return tuple(parsed)


def discounted_score(score: int, hint_cost_radio: float, hint_viewed: bool) -> int:
    if not hint_viewed:
        return score
    # Scores are integer points; dropping fractional points avoids over-awarding.
    return floor(score * (1 - hint_cost_radio) + 1e-12)


def flags_json(flags: tuple[FlagDefinition, ...]) -> str:
    return json.dumps([flag.as_json() for flag in flags], separators=(",", ":"), sort_keys=True)
