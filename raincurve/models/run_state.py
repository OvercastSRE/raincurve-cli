from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ContainerStatus(BaseModel):
    service_name: str
    container_id: str
    host_port: int | None = None
    status: str = "pending"
    error: str | None = None


class RunState(BaseModel):
    project_dir: str
    network_name: str
    containers: dict[str, ContainerStatus] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    snapshot_path: str | None = None
