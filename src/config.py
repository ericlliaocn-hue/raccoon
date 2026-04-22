"""Pydantic Settings 配置管理"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


# 项目根目录（raccoon/）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class RaccoonConfig(BaseSettings):
    """全局配置"""

    # ─── EventBus ───
    event_queue_size: int = 1000

    # ─── Executor ───
    max_concurrent_tasks: int = 5
    task_timeout_seconds: int = 300

    # ─── SkillVault ───
    skills_dir: Path = Field(default=PROJECT_ROOT / "skills")
    sandbox_enabled: bool = True
    sandbox_memory_limit_mb: int = 256
    sandbox_cpu_timeout: int = 60

    # ─── MemCore ───
    db_path: Path = Field(default=PROJECT_ROOT / "data" / "memcore.db")
    write_lock_timeout_ms: int = 500
    write_lock_retries: int = 3

    # ─── Supervisor ───
    auto_approve: bool = True  # MVP: 自动通过

    # ─── Scheduler ───
    schedules_dir: Path = Field(default=PROJECT_ROOT / "schedules")

    # ─── Notifier ───
    notify_channels: list[dict] = Field(default_factory=list)  # [{"type": "system"}, {"type": "bark", "url": "...", "key": "..."}, ...]
    notify_silent_hours: dict = Field(default_factory=dict)     # {"start": "23:00", "end": "07:00"}
    notify_on_success: bool = True
    notify_on_failure: bool = True

    # ─── Gateway ───
    gateway_token: str = ""  # 入站网关认证 Token，为空则不启用

    # ─── Workflow ───
    workflows_dir: Path = Field(default=PROJECT_ROOT / "workflows")

    # ─── Adapters ───
    http_host: str = "0.0.0.0"
    http_port: int = 8900

    # ─── LLM（讯飞 codeplan） ───
    llm_provider: str = "spark"  # mock / spark / openai
    llm_api_key: str = ""  # 通过 config.json 或环境变量 RACCOON_LLM_API_KEY 设置
    llm_model: str = "astron-code-latest"  # 讯飞 codeplan
    llm_base_url: str = "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"  # codeplan 端点
    llm_temperature: float = 0.7
    llm_max_tokens: int = 8192

    model_config = {
        "env_prefix": "RACCOON_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }

    @classmethod
    def from_json(cls, path: Path) -> "RaccoonConfig":
        """从 config.json 加载配置（环境变量优先级更高）"""
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            return cls(**data)
        return cls()


def load_config() -> RaccoonConfig:
    """加载配置：config.json → 环境变量覆盖"""
    config_path = PROJECT_ROOT / "config.json"
    return RaccoonConfig.from_json(config_path)
