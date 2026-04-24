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

升级流程（v0.3.5 增强）：
1. 比较版本号（跳过相同版本）
2. 备份当前版本到 .backup/
3. 从 installed_from 拉取新版
4. 安装新版
5. 更新注册

热更新流程：
1. 从 installed_from 拉取最新代码
2. 比较版本号
3. 替换文件（不重启服务）
4. 更新注册
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import structlog

from src.config import RaccoonConfig
from src.skill_vault.conflict_detector import ConflictDetector
from src.skill_vault.sandbox_runner import SkillRunner
from src.types import SkillMetadata

logger = structlog.get_logger(__name__)


def _compare_versions(v1: str, v2: str) -> int:
    """比较两个版本号

    Returns:
        -1 if v1 < v2, 0 if v1 == v2, 1 if v1 > v2
    """
    def parse(v: str) -> list[int]:
        parts = v.strip().split(".")
        result = []
        for p in parts:
            try:
                result.append(int(p))
            except ValueError:
                result.append(0)
        return result

    p1 = parse(v1)
    p2 = parse(v2)
    # 补齐长度
    max_len = max(len(p1), len(p2))
    p1 = p1 + [0] * (max_len - len(p1))
    p2 = p2 + [0] * (max_len - len(p2))

    for a, b in zip(p1, p2):
        if a < b:
            return -1
        if a > b:
            return 1
    return 0


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
        """列出所有已安装的 Skill"""
        return list(self._skills.values())

    def list_enabled_skills(self) -> list[SkillMetadata]:
        """列出已启用的 Skill"""
        return [s for s in self._skills.values() if s.enabled]

    def toggle_skill(self, name: str) -> SkillMetadata:
        """切换 Skill 启用/禁用状态"""
        meta = self._skills.get(name)
        if not meta:
            raise KeyError(f"Skill not found: {name}")
        meta.enabled = not meta.enabled
        # 持久化到 metadata.json
        self._persist_skill_meta(name)
        logger.info("skill_toggled", name=name, enabled=meta.enabled)
        return meta

    def toggle_star(self, name: str) -> SkillMetadata:
        """切换 Skill 收藏/取消收藏状态"""
        meta = self._skills.get(name)
        if not meta:
            raise KeyError(f"Skill not found: {name}")
        meta.starred = not meta.starred
        self._persist_skill_meta(name)
        logger.info("skill_star_toggled", name=name, starred=meta.starred)
        return meta

    def _persist_skill_meta(self, name: str) -> None:
        """将 Skill 元数据写回 metadata.json"""
        meta = self._skills.get(name)
        if not meta:
            return
        import json
        meta_path = self._skills_dir / name / "metadata.json"
        if not meta_path.exists():
            return
        # 读取原始数据，只更新 enabled/starred/installed_from/installed_at 字段，保留其他原始格式
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data["enabled"] = meta.enabled
        data["starred"] = meta.starred
        if meta.installed_from is not None:
            data["installed_from"] = meta.installed_from
        if meta.installed_at is not None:
            data["installed_at"] = meta.installed_at
        meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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
                meta = self._install_from_local(tmp_dir)
                meta.installed_from = url
                meta.installed_at = datetime.now(timezone.utc).isoformat()
                self._persist_skill_meta(meta.name)
                return meta
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

    async def upgrade(self, name: str) -> SkillMetadata:
        """升级已安装的 Skill（带版本比较、备份、回滚）

        流程：
        1. 比较版本号（相同版本跳过）
        2. 备份当前版本到 .backup/
        3. 从 installed_from 拉取新版
        4. 安装新版
        5. 失败时自动回滚

        Returns:
            新版 SkillMetadata
        """
        meta = self._skills.get(name)
        if not meta:
            raise KeyError(f"Skill not found: {name}")
        source = meta.installed_from
        if not source or source == "builtin":
            raise RuntimeError(f"Skill '{name}' is builtin, cannot upgrade")

        # 1. 拉取新版元数据，比较版本
        try:
            import git as gitpython
            import tempfile

            with tempfile.TemporaryDirectory() as tmp_dir:
                gitpython.Repo.clone_from(source, tmp_dir, depth=1)
                new_meta_path = Path(tmp_dir) / "metadata.json"
                if not new_meta_path.exists():
                    raise RuntimeError(f"metadata.json not found in remote source")
                new_data = json.loads(new_meta_path.read_text(encoding="utf-8"))
                new_meta = SkillMetadata.model_validate(new_data)
                new_version = new_meta.version
                old_version = meta.version

                # 版本比较
                cmp = _compare_versions(new_version, old_version)
                if cmp == 0:
                    raise RuntimeError(f"Skill '{name}' 已是最新版本 ({old_version})，无需升级")
                if cmp < 0:
                    raise RuntimeError(f"Skill '{name}' 远程版本 ({new_version}) 低于本地版本 ({old_version})，请检查来源")
                logger.info("skill_upgrade_version_check", name=name, old=old_version, new=new_version)
        except ImportError:
            raise RuntimeError("GitPython not installed. Run: pip install gitpython")
        except RuntimeError:
            raise  # 透传版本比较错误
        except Exception as e:
            raise RuntimeError(f"Failed to check remote version: {e}")

        # 2. 备份当前版本
        skill_dir = self._skills_dir / name
        backup_dir = self._skills_dir / ".backup" / f"{name}_{old_version}"
        if skill_dir.exists():
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            shutil.copytree(skill_dir, backup_dir)
            logger.info("skill_backup_created", name=name, version=old_version, backup=str(backup_dir))

        # 3. 卸载旧版 + 安装新版
        try:
            self.uninstall(name)
            new_installed = await self.install(source)
            logger.info("skill_upgraded", name=name, old=old_version, new=new_installed.version)
            return new_installed
        except Exception as e:
            # 4. 安装失败，自动回滚
            logger.error("skill_upgrade_failed_rollback", name=name, error=str(e))
            try:
                if backup_dir.exists():
                    if skill_dir.exists():
                        shutil.rmtree(skill_dir)
                    shutil.copytree(backup_dir, skill_dir)
                    # 重新注册
                    rollback_meta_path = skill_dir / "metadata.json"
                    rollback_data = json.loads(rollback_meta_path.read_text(encoding="utf-8"))
                    rollback_meta = self._parse_metadata(rollback_data)
                    rollback_meta.installed_from = source
                    rollback_meta.installed_at = meta.installed_at
                    self._skills[name] = rollback_meta
                    self._persist_skill_meta(name)
                    logger.info("skill_rollback_success", name=name, version=old_version)
            except Exception as rollback_err:
                logger.error("skill_rollback_failed", name=name, error=str(rollback_err))
            raise RuntimeError(f"升级失败已回滚到 {old_version}：{e}")

    def rollback(self, name: str, version: str | None = None) -> SkillMetadata:
        """回滚 Skill 到指定版本（或最近的备份版本）

        Args:
            name: Skill 名称
            version: 目标版本号（为空则使用最近备份）

        Returns:
            回滚后的 SkillMetadata
        """
        backup_base = self._skills_dir / ".backup"
        if not backup_base.exists():
            raise RuntimeError(f"没有可用的备份")

        # 查找备份
        if version:
            backup_dir = backup_base / f"{name}_{version}"
        else:
            # 查找最近的备份（按名称前缀匹配）
            candidates = sorted(
                [d for d in backup_base.iterdir() if d.is_dir() and d.name.startswith(name)],
                key=lambda d: d.name,
                reverse=True,
            )
            if not candidates:
                raise RuntimeError(f"没有 Skill '{name}' 的备份")
            backup_dir = candidates[0]

        if not backup_dir.exists():
            raise RuntimeError(f"备份不存在：{backup_dir.name}")

        # 回滚
        skill_dir = self._skills_dir / name
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        shutil.copytree(backup_dir, skill_dir)

        # 重新注册
        meta_path = skill_dir / "metadata.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = self._parse_metadata(data)
        self._skills[name] = meta
        self._persist_skill_meta(name)
        logger.info("skill_rollback", name=name, version=meta.version)
        return meta

    def list_backups(self, name: str | None = None) -> list[dict]:
        """列出可用备份

        Args:
            name: Skill 名称（为空则列出所有）
        """
        backup_base = self._skills_dir / ".backup"
        if not backup_base.exists():
            return []

        result = []
        for d in backup_base.iterdir():
            if not d.is_dir():
                continue
            # 格式：{name}_{version}
            parts = d.name.rsplit("_", 1)
            skill_name = parts[0]
            skill_version = parts[1] if len(parts) > 1 else "unknown"

            if name and skill_name != name:
                continue

            # 读取备份的 metadata
            backup_meta = d / "metadata.json"
            description = ""
            if backup_meta.exists():
                try:
                    bdata = json.loads(backup_meta.read_text(encoding="utf-8"))
                    description = bdata.get("description", "")
                except Exception:
                    pass

            result.append({
                "name": skill_name,
                "version": skill_version,
                "description": description,
                "backup_dir": str(d),
                "created_at": datetime.fromtimestamp(d.stat().st_ctime, tz=timezone.utc).isoformat(),
            })

        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    async def hot_update(self, name: str) -> dict:
        """热更新 Skill（运行时更新代码，不重启服务）

        流程：
        1. 从 installed_from 拉取最新代码
        2. 比较版本号
        3. 替换文件
        4. 更新注册（内存中的 SkillMetadata）

        Returns:
            {"status": "updated"/"skipped"/"failed", "old_version", "new_version"}
        """
        meta = self._skills.get(name)
        if not meta:
            raise KeyError(f"Skill not found: {name}")
        source = meta.installed_from
        if not source or source == "builtin":
            return {"status": "skipped", "reason": "builtin skill cannot hot update"}

        old_version = meta.version

        # 拉取最新代码
        try:
            import git as gitpython
            import tempfile

            with tempfile.TemporaryDirectory() as tmp_dir:
                gitpython.Repo.clone_from(source, tmp_dir, depth=1)
                new_meta_path = Path(tmp_dir) / "metadata.json"
                if not new_meta_path.exists():
                    return {"status": "failed", "reason": "metadata.json not found in remote"}
                new_data = json.loads(new_meta_path.read_text(encoding="utf-8"))
                new_meta = SkillMetadata.model_validate(new_data)
                new_version = new_meta.version

                # 版本比较
                cmp = _compare_versions(new_version, old_version)
                if cmp <= 0:
                    return {"status": "skipped", "old_version": old_version, "new_version": new_version, "reason": "already up to date or remote version lower"}

                # 替换文件
                skill_dir = self._skills_dir / name
                # 保留 metadata.json 中的 enabled/starred/installed_from/installed_at
                preserved_fields = {
                    "enabled": meta.enabled,
                    "starred": meta.starred,
                    "installed_from": source,
                    "installed_at": meta.installed_at,
                }

                # 清理旧文件（保留 .backup 子目录）
                for item in skill_dir.iterdir():
                    if item.name == ".backup":
                        continue
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()

                # 复制新文件
                source_dir = Path(tmp_dir)
                for item in source_dir.iterdir():
                    if item.name == ".git":
                        continue
                    if item.is_dir():
                        shutil.copytree(item, skill_dir / item.name)
                    else:
                        shutil.copy2(item, skill_dir / item.name)

                # 更新注册
                new_meta.enabled = preserved_fields["enabled"]
                new_meta.starred = preserved_fields["starred"]
                new_meta.installed_from = preserved_fields["installed_from"]
                new_meta.installed_at = preserved_fields["installed_at"]
                self._skills[name] = new_meta
                self._persist_skill_meta(name)

                logger.info("skill_hot_updated", name=name, old=old_version, new=new_version)
                return {"status": "updated", "old_version": old_version, "new_version": new_version}

        except ImportError:
            return {"status": "failed", "reason": "GitPython not installed"}
        except Exception as e:
            logger.error("skill_hot_update_failed", name=name, error=str(e))
            return {"status": "failed", "reason": str(e)}
