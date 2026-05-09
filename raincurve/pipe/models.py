from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InterceptedRequest(BaseModel):
    api: str
    method: str
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


class MockResponse(BaseModel):
    status: int = 200
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(default_factory=dict)
    state_writes: dict[str, dict[str, Any]] = Field(default_factory=dict)
