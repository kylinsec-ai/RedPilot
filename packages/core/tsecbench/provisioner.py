"""Container lifecycle adapters used by the challenge service."""

from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
import subprocess
from typing import Protocol

from .models import ChallengeDefinition


class ProvisionerError(Exception):
    """Base class for expected provisioning failures."""


class ResourceUnavailable(ProvisionerError):
    """The challenge cannot currently be provisioned."""


@dataclass(frozen=True)
class ProvisionedContainer:
    addresses: tuple[str, ...]
    container_id: str | None = None


class ContainerProvisioner(Protocol):
    def start(self, task_token: str, challenge: ChallengeDefinition) -> ProvisionedContainer:
        ...

    def stop(self, task_token: str, challenge: ChallengeDefinition, container_id: str | None) -> None:
        ...


class StaticProvisioner:
    """Use addresses supplied by the task catalog.

    This is the safe default for an API-only deployment where another system
    owns challenge containers. A missing address is treated as unavailable,
    never as a fabricated localhost endpoint.
    """

    def start(self, task_token: str, challenge: ChallengeDefinition) -> ProvisionedContainer:
        if not challenge.container_addr:
            raise ResourceUnavailable("No challenge instance is available")
        return ProvisionedContainer(addresses=challenge.container_addr)

    def stop(self, task_token: str, challenge: ChallengeDefinition, container_id: str | None) -> None:
        return None


class DockerProvisioner:
    """Launch configured challenge images with the Docker CLI.

    An image must be explicitly present in the challenge catalog. When a
    catalog already contains addresses they are retained after startup; for
    dynamically allocated containers the configured container port is used to
    construct an address from Docker's network inspection data.
    """

    def __init__(self, docker_binary: str = "docker", command_timeout: float = 30.0) -> None:
        self.docker_binary = docker_binary
        self.command_timeout = command_timeout

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if shutil.which(self.docker_binary) is None:
            raise ResourceUnavailable("Docker is not available")
        try:
            return subprocess.run(
                [self.docker_binary, *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ResourceUnavailable("Docker could not provision the challenge") from exc

    def start(self, task_token: str, challenge: ChallengeDefinition) -> ProvisionedContainer:
        if not challenge.image:
            if challenge.container_addr:
                return ProvisionedContainer(addresses=challenge.container_addr)
            raise ResourceUnavailable("No challenge image or address is configured")
        args = [
            "run",
            "-d",
            "--rm",
            "--label",
            f"tsecbench.task={task_token}",
            "--label",
            f"tsecbench.challenge={challenge.unique_code}",
        ]
        if challenge.docker_network:
            args.extend(["--network", challenge.docker_network])
        if challenge.container_port is not None:
            args.extend(["-P", "--expose", str(challenge.container_port)])
        args.append(challenge.image)
        result = self._run(args)
        container_id = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
        if not container_id:
            raise ResourceUnavailable("Docker returned no container id")

        if challenge.container_addr:
            return ProvisionedContainer(addresses=challenge.container_addr, container_id=container_id)

        inspect = self._run(["inspect", container_id])
        try:
            payload = json.loads(inspect.stdout)[0]
            networks = payload.get("NetworkSettings", {}).get("Networks", {})
            ip_address = next(
                (network.get("IPAddress") for network in networks.values() if network.get("IPAddress")),
                None,
            )
            if not ip_address or challenge.container_port is None:
                raise ValueError
        except (ValueError, IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.stop(task_token, challenge, container_id)
            raise ResourceUnavailable("Docker did not expose a challenge address") from exc
        return ProvisionedContainer(addresses=(f"{ip_address}:{challenge.container_port}",), container_id=container_id)

    def stop(self, task_token: str, challenge: ChallengeDefinition, container_id: str | None) -> None:
        if not container_id:
            return None
        self._run(["rm", "-f", container_id])


def provisioner_for(mode: str) -> ContainerProvisioner:
    if mode == "static":
        return StaticProvisioner()
    if mode == "docker":
        return DockerProvisioner()
    raise ValueError(f"unsupported provisioner mode: {mode}")
