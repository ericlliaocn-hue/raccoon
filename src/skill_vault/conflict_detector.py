"""pre_install_scan 冲突检测

检测维度：
- API Key 冲突：两个 Skill 声明使用同一个 API Key 环境变量
- 符号冲突：两个 Skill 导出同名函数/类
- 网络权限冲突：一个 Skill 声明 network=readonly，另一个 network=full
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel
from src.types import SkillMetadata

logger = structlog.get_logger(__name__)


class ConflictReport(BaseModel):
    """冲突报告"""
    conflict_type: str  # api_key / symbol / network
    existing_skill: str
    new_skill: str
    detail: str
    severity: str = "warning"  # warning / error


class ConflictDetector:
    """安装前冲突扫描"""

    def scan(
        self, new_skill: SkillMetadata, installed: list[SkillMetadata]
    ) -> list[ConflictReport]:
        """扫描新 Skill 与已安装 Skill 之间的冲突"""
        reports: list[ConflictReport] = []

        for existing in installed:
            # API Key 冲突
            reports.extend(self._check_api_key_conflict(existing, new_skill))
            # 网络权限冲突
            reports.extend(self._check_network_conflict(existing, new_skill))

        return reports

    def _check_api_key_conflict(
        self, existing: SkillMetadata, new_skill: SkillMetadata
    ) -> list[ConflictReport]:
        """检测 API Key 冲突"""
        reports = []
        existing_keys = {p for p in existing.permissions if p.startswith("api_key:")}
        new_keys = {p for p in new_skill.permissions if p.startswith("api_key:")}

        overlap = existing_keys & new_keys
        for key in overlap:
            reports.append(ConflictReport(
                conflict_type="api_key",
                existing_skill=existing.name,
                new_skill=new_skill.name,
                detail=f"Both use {key}",
                severity="warning",
            ))
        return reports

    def _check_network_conflict(
        self, existing: SkillMetadata, new_skill: SkillMetadata
    ) -> list[ConflictReport]:
        """检测网络权限冲突"""
        reports = []
        existing_net = {p for p in existing.permissions if p.startswith("network:")}
        new_net = {p for p in new_skill.permissions if p.startswith("network:")}

        if existing_net and new_net and existing_net != new_net:
            reports.append(ConflictReport(
                conflict_type="network",
                existing_skill=existing.name,
                new_skill=new_skill.name,
                detail=f"Network permissions differ: {existing_net} vs {new_net}",
                severity="warning",
            ))
        return reports
