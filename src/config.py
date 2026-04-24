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
    approval_timeout_seconds: int = 300  # 审批超时（秒），默认 5 分钟

    # ─── Scheduler ───
    schedules_dir: Path = Field(default=PROJECT_ROOT / "schedules")

    # ─── Notifier ───
    notify_channels: list[dict] = Field(default_factory=list)  # [{"type": "system"}, {"type": "bark", "url": "...", "key": "..."}, ...]
    notify_silent_hours: dict = Field(default_factory=dict)     # {"start": "23:00", "end": "07:00"}
    notify_on_success: bool = True
    notify_on_failure: bool = True
    notify_dedup_window_seconds: int = 300  # 去重时间窗口（秒），默认 5 分钟

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

    # ─── 多模型管理 ───
    llm_models: list[dict] = Field(default_factory=list)  # [{"id","name","vendor","url","apiKey","maxInputTokens","maxOutputTokens","supportsToolCall","supportsImages","supportsReasoning"}, ...]
    llm_active_model_id: str = ""  # 当前激活的模型 ID（对应 llm_models 中的 id）

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
            config = cls(**data)
            # 迁移：如果 llm_models 为空但有旧版 LLM 配置，自动创建默认模型
            config._migrate_legacy_llm_config()
            return config
        return cls()

    def _migrate_legacy_llm_config(self) -> None:
        """将旧版单模型配置迁移到 llm_models 列表"""
        if self.llm_models:
            return  # 已有模型列表，无需迁移
        if not self.llm_api_key:
            return  # 没有配置 API Key，无法创建模型

        # 根据 provider 推断 vendor
        vendor_map = {
            "spark": "讯飞",
            "openai": "OpenAI",
            "mock": "Mock",
        }
        vendor = vendor_map.get(self.llm_provider, self.llm_provider)

        # 创建默认模型
        default_model = {
            "id": self.llm_model or "default",
            "name": self.llm_model or "Default Model",
            "vendor": vendor,
            "url": self.llm_base_url,
            "apiKey": self.llm_api_key,
            "maxOutputTokens": self.llm_max_tokens,
        }
        self.llm_models = [default_model]
        self.llm_active_model_id = default_model["id"]

        # 持久化迁移后的配置
        self._persist_models()
        print(f"[Config] 已自动迁移旧版 LLM 配置到模型列表: {default_model['name']}")

    def update_llm(
        self,
        provider: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        """更新 LLM 配置并持久化到 config.json"""
        if provider is not None:
            self.llm_provider = provider
        if api_key is not None:
            self.llm_api_key = api_key
        if model is not None:
            self.llm_model = model
        if base_url is not None:
            self.llm_base_url = base_url
        if temperature is not None:
            self.llm_temperature = temperature
        if max_tokens is not None:
            self.llm_max_tokens = max_tokens

        # 持久化到 config.json
        config_path = PROJECT_ROOT / "config.json"
        existing = {}
        if config_path.exists():
            with open(config_path) as f:
                existing = json.load(f)

        llm_fields = {
            "llm_provider", "llm_api_key", "llm_model",
            "llm_base_url", "llm_temperature", "llm_max_tokens",
        }
        for field in llm_fields:
            val = getattr(self, field)
            if field == "llm_temperature":
                existing[field] = float(val)
            elif field == "llm_max_tokens":
                existing[field] = int(val)
            else:
                existing[field] = str(val)

        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

    def mask_api_key(self) -> str:
        """返回脱敏的 API Key"""
        key = self.llm_api_key
        if not key or len(key) <= 8:
            return "***" if key else ""
        return key[:4] + "*" * (len(key) - 8) + key[-4:]

    # ─── 多模型管理 ──────────────────────────────────────────

    def list_models(self) -> list[dict]:
        """列出所有已配置的模型（apiKey 脱敏）"""
        result = []
        for m in self.llm_models:
            entry = dict(m)
            if entry.get("apiKey"):
                ak = entry["apiKey"]
                entry["apiKey"] = ak[:4] + "*" * (len(ak) - 8) + ak[-4:] if len(ak) > 8 else "***"
            entry["active"] = (m.get("id") == self.llm_active_model_id)
            result.append(entry)
        return result

    def get_model(self, model_id: str) -> dict | None:
        """获取指定模型配置（apiKey 不脱敏，仅内部使用）"""
        for m in self.llm_models:
            if m.get("id") == model_id:
                return dict(m)
        return None

    def add_model(self, model: dict) -> dict:
        """新增模型配置"""
        # 确保 id 唯一
        model_id = model.get("id", "")
        if not model_id:
            import uuid
            model_id = model.get("name", str(uuid.uuid4())[:8])
            model["id"] = model_id
        # 检查 id 冲突
        existing_ids = {m.get("id") for m in self.llm_models}
        if model_id in existing_ids:
            # 追加后缀
            base = model_id
            i = 1
            while model_id in existing_ids:
                model_id = f"{base}_{i}"
                i += 1
            model["id"] = model_id
        self.llm_models.append(model)
        self._persist_models()
        return {"status": "added", "id": model_id}

    def update_model(self, model_id: str, updates: dict) -> dict:
        """更新模型配置"""
        for i, m in enumerate(self.llm_models):
            if m.get("id") == model_id:
                # 不允许修改 id
                updates.pop("id", None)
                # apiKey 含 *** 则跳过
                if updates.get("apiKey") and "***" in updates.get("apiKey", ""):
                    updates.pop("apiKey")
                self.llm_models[i].update(updates)
                self._persist_models()
                # 如果更新的是当前激活模型，同步到顶层 LLM 配置
                if model_id == self.llm_active_model_id:
                    self._sync_active_model_to_llm()
                return {"status": "updated", "id": model_id}
        return {"status": "not_found"}

    def delete_model(self, model_id: str) -> dict:
        """删除模型配置"""
        before = len(self.llm_models)
        self.llm_models = [m for m in self.llm_models if m.get("id") != model_id]
        if len(self.llm_models) < before:
            # 如果删除的是当前激活模型，清除激活状态
            if model_id == self.llm_active_model_id:
                self.llm_active_model_id = ""
            self._persist_models()
            return {"status": "deleted", "id": model_id}
        return {"status": "not_found"}

    def activate_model(self, model_id: str) -> dict:
        """切换当前激活的模型"""
        model = self.get_model(model_id)
        if not model:
            return {"status": "not_found"}
        self.llm_active_model_id = model_id
        self._sync_active_model_to_llm()
        self._persist_models()
        return {"status": "activated", "id": model_id, "provider": self.llm_provider, "model": self.llm_model}

    def _sync_active_model_to_llm(self) -> None:
        """将激活的模型配置同步到顶层 LLM 字段"""
        model = self.get_model(self.llm_active_model_id)
        if not model:
            return
        # 根据 vendor 推断 provider
        vendor = model.get("vendor", "").lower()
        if vendor == "spark" or "星火" in model.get("vendor", ""):
            self.llm_provider = "spark"
        elif vendor == "mock":
            self.llm_provider = "mock"
        else:
            self.llm_provider = "openai"
        self.llm_api_key = model.get("apiKey", self.llm_api_key)
        self.llm_model = model.get("name", self.llm_model)
        self.llm_base_url = model.get("url", self.llm_base_url)
        if model.get("maxOutputTokens"):
            self.llm_max_tokens = model["maxOutputTokens"]

    def _persist_models(self) -> None:
        """持久化模型列表到 config.json"""
        config_path = PROJECT_ROOT / "config.json"
        existing = {}
        if config_path.exists():
            with open(config_path) as f:
                existing = json.load(f)
        existing["llm_models"] = self.llm_models
        existing["llm_active_model_id"] = self.llm_active_model_id
        # 同步顶层 LLM 字段
        llm_fields = {
            "llm_provider", "llm_api_key", "llm_model",
            "llm_base_url", "llm_temperature", "llm_max_tokens",
        }
        for field in llm_fields:
            val = getattr(self, field)
            if field == "llm_temperature":
                existing[field] = float(val)
            elif field == "llm_max_tokens":
                existing[field] = int(val)
            else:
                existing[field] = str(val)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)


# ─── LLM 预设模板 ─────────────────────────────────────────────

LLM_PRESETS: list[dict] = [
    {
        "id": "spark_codeplan",
        "name": "讯飞 codeplan",
        "provider": "spark",
        "base_url": "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2",
        "model": "astron-code-latest",
        "description": "讯飞星火 codeplan 编程助手",
    },
    {
        "id": "spark_ultra",
        "name": "讯飞星火 4.0 Ultra",
        "provider": "spark",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "model": "generalv3.5",
        "description": "讯飞星火大模型 4.0 Ultra",
    },
    {
        "id": "openai_gpt4",
        "name": "OpenAI GPT-4o",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "description": "OpenAI GPT-4o",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek Chat",
        "provider": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "description": "DeepSeek Chat（OpenAI 兼容）",
    },
    {
        "id": "qwen",
        "name": "通义千问",
        "provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "description": "通义千问（OpenAI 兼容）",
    },
    {
        "id": "custom",
        "name": "自定义",
        "provider": "openai",
        "base_url": "",
        "model": "",
        "description": "自定义 OpenAI 兼容接口",
    },
]


def load_config() -> RaccoonConfig:
    """加载配置：config.json → 环境变量覆盖"""
    config_path = PROJECT_ROOT / "config.json"
    return RaccoonConfig.from_json(config_path)
