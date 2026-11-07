from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEMO_ROOT = PACKAGE_ROOT / "examples" / "checkout-service"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: PACKAGE_ROOT / ".traceforge")
    allowed_roots: list[Path] = field(default_factory=lambda: [DEMO_ROOT])
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_style: str = "chat"
    request_timeout: float = 60
    max_steps: int = 10
    max_context_chars: int = 32000
    max_total_tokens: int = 60000
    max_output_tokens: int = 6000
    max_run_seconds: float = 240
    test_timeout: float = 45
    max_files: int = 1200
    max_file_bytes: int = 250000
    docker_image: str = "python:3.12-slim"
    api_token: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        roots = json.loads(env.get("TRACEFORGE_ALLOWED_ROOTS", "[]"))
        if not isinstance(roots, list) or any(not isinstance(x, str) for x in roots):
            raise ValueError("TRACEFORGE_ALLOWED_ROOTS must be a JSON array of paths")
        style = env.get("TRACEFORGE_API_STYLE", "chat")
        if style not in {"chat", "responses"}:
            raise ValueError("TRACEFORGE_API_STYLE must be chat or responses")
        return cls(
            data_dir=Path(env.get("TRACEFORGE_DATA_DIR", str(PACKAGE_ROOT / ".traceforge"))).resolve(),
            allowed_roots=[DEMO_ROOT.resolve(), *(Path(x).resolve() for x in roots)],
            api_key=env.get("TRACEFORGE_API_KEY", ""),
            base_url=env.get("TRACEFORGE_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            model=env.get("TRACEFORGE_MODEL", ""),
            api_style=style,
            request_timeout=float(env.get("TRACEFORGE_REQUEST_TIMEOUT", "60")),
            max_steps=int(env.get("TRACEFORGE_MAX_STEPS", "10")),
            max_context_chars=int(env.get("TRACEFORGE_MAX_CONTEXT_CHARS", "32000")),
            max_total_tokens=int(env.get("TRACEFORGE_MAX_TOTAL_TOKENS", "60000")),
            max_output_tokens=int(env.get("TRACEFORGE_MAX_OUTPUT_TOKENS", "6000")),
            max_run_seconds=float(env.get("TRACEFORGE_MAX_RUN_SECONDS", "240")),
            test_timeout=float(env.get("TRACEFORGE_TEST_TIMEOUT", "45")),
            docker_image=env.get("TRACEFORGE_DOCKER_IMAGE", "python:3.12-slim"),
            api_token=env.get("TRACEFORGE_API_TOKEN", ""),
        )

    @property
    def provider_configured(self) -> bool:
        return bool(self.api_key and self.model)

    def validate_root(self, raw: str) -> Path:
        root = Path(raw).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("项目路径必须是目录")
        if not any(root.is_relative_to(base.resolve()) for base in self.allowed_roots):
            raise ValueError("路径未在 TRACEFORGE_ALLOWED_ROOTS 白名单内")
        return root
