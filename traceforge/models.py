from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    root: str = Field(min_length=1, max_length=2000)
    test_command: list[str] = Field(default_factory=list, max_length=30)
    runner: Literal["local", "docker"] = "docker"
    trusted: bool = False
    github_repo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

    @field_validator("test_command")
    @classmethod
    def command_parts(cls, values: list[str]) -> list[str]:
        if any(not x or len(x) > 2000 or "\x00" in x for x in values):
            raise ValueError("test_command 参数不能为空或包含 NUL")
        return values


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=30000)
    mode: Literal["replay", "live", "investigate"] = "investigate"


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    patch_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)
    before: str = Field(min_length=1, max_length=30000)
    after: str = Field(max_length=30000)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=4000)
    root_cause: str = Field(min_length=1, max_length=8000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    confidence: Literal["low", "medium", "high"]
    limitations: list[str] = Field(max_length=20)
    patches: list[Patch] = Field(default_factory=list, max_length=10)
