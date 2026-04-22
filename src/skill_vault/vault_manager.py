"""Skill 注册/安装/卸载，Git 版本管理

Skill 目录结构：
skills/<skill_name>/
  ├── metadata.json    # Skill 元数据
  ├── main.py          # Skill 入口
  └── ...              # 其他文件

安装流程：
1. clone Git 仓库到临时目录
2. 读取 metadata.json
3. pre_install_scan 冲突检测
4. 移动到 skills/ 目录
5. 注册到 Router
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

import structlog

from src.config import RaccoonConfig
from src.skill_vault.conflict_detector import ConflictDetector
from src.skill_vault.sandbox_runner import SkillRunner
from src.types import SkillMetadata

logger = structlog.get_logger(__name__)


class SkillRunnerProtocol(Protocol):
    async def run(self, task, params: dict) -> dict: ...


class VaultManager:
    """Skill 库管理器"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._skills_dir = self._config.skills_dir
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        self._skills: dict[str, SkillMetadata] = {}
        self._conflict_detector = ConflictDetector()
        self._load_existing_skills()

    def _load_existing_skills(self) -> None:
        """加载已安装的 Skill"""
        for skill_dir in self._skills_dir.iterdir():
            if skill_dir.is_dir():
                meta_path = skill_dir / "metadata.json"
                if meta_path.exists():
                    try:
                        import json
                        data = json.loads(meta_path.read_text(encoding="utf-8"))
                        # 解析 flow 定义（如果有）
                        meta = self._parse_metadata(data)
                        self._skills[meta.name] = meta
                        logger.info("skill_loaded", name=meta.name, version=meta.version, interactive=meta.interactive)
                    except Exception as e:
                        logger.warning("skill_load_failed", dir=str(skill_dir), error=str(e))

    def _parse_metadata(self, data: dict) -> SkillMetadata:
        """解析 Skill 元数据，处理 flow 定义"""
        from src.types import FlowDefinition, FlowStep, FlowStepType

        # 如果有 flow 定义，解析为 FlowDefinition
        if "flow" in data and data.get("interactive"):
            flow_data = data["flow"]
            steps = []
            for s in flow_data.get("steps", []):
                step = FlowStep(
                    name=s.get("name", ""),
                    type=FlowStepType(s.get("type", "skill_exec")),
                    prompt=s.get("prompt", ""),
                    options=s.get("options", []),
                    skill_action=s.get("skill_action", ""),
                    llm_system_prompt=s.get("llm_system_prompt", ""),
                    background=s.get("background", False),
                    next_step=s.get("next_step"),
                )
                steps.append(step)
            flow = FlowDefinition(
                steps=steps,
                on_complete=flow_data.get("on_complete", ""),
                on_cancel=flow_data.get("on_cancel", "流程已取消"),
            )
            data["flow"] = flow.model_dump()
            data["interactive"] = True

        return SkillMetadata.model_validate(data)

    def list_skills(self) -> list[SkillMetadata]:
        """列出已安装的 Skill"""
        return list(self._skills.values())

    def get_skill(self, name: str) -> SkillMetadata | None:
        """获取 Skill 元数据"""
        return self._skills.get(name)

    def get_skill_runner(self, name: str) -> SkillRunner | None:
        """获取 Skill 运行器"""
        meta = self._skills.get(name)
        if not meta:
            return None
        skill_dir = self._skills_dir / name
        if not skill_dir.exists():
            return None
        return SkillRunner(skill_dir, meta.entry, self._config)

    async def install(self, source: str) -> SkillMetadata:
        """安装 Skill

        source 可以是：
        - 本地目录路径
        - Git 仓库 URL
        """
        # 尝试 Git clone
        if source.startswith("http") or source.endswith(".git"):
            return await self._install_from_git(source)
        else:
            return self._install_from_local(source)

    async def _install_from_git(self, url: str) -> SkillMetadata:
        """从 Git 仓库安装"""
        try:
            import git as gitpython
            import tempfile

            with tempfile.TemporaryDirectory() as tmp_dir:
                logger.info("cloning_skill", url=url)
                gitpython.Repo.clone_from(url, tmp_dir, depth=1)
                return self._install_from_local(tmp_dir)
        except ImportError:
            raise RuntimeError("GitPython not installed. Run: pip install gitpython")
        except Exception as e:
            raise RuntimeError(f"Git clone failed: {e}")

    def _install_from_local(self, source_path: str) -> SkillMetadata:
        """从本地目录安装"""
        source_dir = Path(source_path)
        meta_path = source_dir / "metadata.json"

        if not meta_path.exists():
            raise FileNotFoundError(f"metadata.json not found in {source_path}")

        import json
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = SkillMetadata.model_validate(data)

        # 冲突检测
        installed = list(self._skills.values())
        conflicts = self._conflict_detector.scan(meta, installed)
        for c in conflicts:
            if c.severity == "error":
                raise RuntimeError(f"Conflict detected: {c.detail}")
            logger.warning("install_conflict", **c.model_dump())

        # 复制到 skills 目录
        target_dir = self._skills_dir / meta.name
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir)

        # 注册
        self._skills[meta.name] = meta
        logger.info("skill_installed", name=meta.name, version=meta.version)
        return meta

    def uninstall(self, name: str) -> None:
        """卸载 Skill"""
        if name not in self._skills:
            raise KeyError(f"Skill not found: {name}")

        skill_dir = self._skills_dir / name
        if skill_dir.exists():
            shutil.rmtree(skill_dir)

        del self._skills[name]
        logger.info("skill_uninstalled", name=name)
