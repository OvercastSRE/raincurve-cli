from __future__ import annotations

import docker


def ensure_network(client: docker.DockerClient, name: str) -> str:
    try:
        net = client.networks.get(name)
        return net.id  # type: ignore[return-value]
    except docker.errors.NotFound:
        net = client.networks.create(name, driver="bridge")
        return net.id  # type: ignore[return-value]


def remove_network(client: docker.DockerClient, name: str) -> None:
    try:
        net = client.networks.get(name)
        net.remove()
    except docker.errors.NotFound:
        pass
