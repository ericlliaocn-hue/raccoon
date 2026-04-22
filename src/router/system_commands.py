"""Layer3: / 和 @ 指令解析

系统指令格式：
- /help → 帮助
- /status [task_id] → 任务状态
- /cancel <task_id> → 取消任务
- /skills → 列出已安装 Skill
- /install <url> → 安装 Skill
- /uninstall <name> → 卸载 Skill
- /config → 查看配置
- @<skill_name|别名> <args> → 直接调用 Skill（支持中英文别名）
"""

from __future__ import annotations

import re

import structlog
from src.types import RouteResult, RouteType

logger = structlog.get_logger(__name__)

# 系统指令正则
CMD_PATTERN = re.compile(r"^/(\w+)(?:\s+(.*))?$")
# @skill 格式：支持中英文别名（\w 不匹配中文，改用 \S+ 匹配非空白）
SKILL_DIRECT_PATTERN = re.compile(r"^@(\S+)(?:\s+(.*))?$")


class SystemCommands:
    """系统指令解析层"""

    # 内置指令列表
    BUILTIN_COMMANDS = {
        "help": "显示帮助信息",
        "status": "查询任务状态",
        "cancel": "取消任务",
        "skills": "列出已安装的 Skill",
        "install": "安装 Skill",
        "uninstall": "卸载 Skill",
        "config": "查看当前配置",
    }

    def __init__(self) -> None:
        # 别名映射：alias → skill_name（由 Router 注册时填充）
        self._alias_map: dict[str, str] = {}

    def register_aliases(self, skill_name: str, aliases: list[str]) -> None:
        """注册 Skill 的别名"""
        for alias in aliases:
            alias_lower = alias.lower()
            if alias_lower in self._alias_map:
                existing = self._alias_map[alias_lower]
                logger.warning(
                    "alias_conflict",
                    alias=alias_lower,
                    existing_skill=existing,
                    new_skill=skill_name,
                )
            self._alias_map[alias_lower] = skill_name

    def unregister_aliases(self, aliases: list[str]) -> None:
        """移除 Skill 的别名"""
        for alias in aliases:
            alias_lower = alias.lower()
            self._alias_map.pop(alias_lower, None)

    def resolve_alias(self, name: str) -> str:
        """将别名解析为 skill_name，无匹配则原样返回"""
        return self._alias_map.get(name.lower(), name)

    def parse(self, text: str) -> RouteResult | None:
        """解析系统指令，返回 RouteResult 或 None（非指令）"""
        text = text.strip()

        # /command 格式
        cmd_match = CMD_PATTERN.match(text)
        if cmd_match:
            command = cmd_match.group(1).lower()
            args = cmd_match.group(2) or ""
            return self._resolve_command(command, args)

        # @skill 格式（支持别名）
        skill_match = SKILL_DIRECT_PATTERN.match(text)
        if skill_match:
            raw_name = skill_match.group(1)
            args = skill_match.group(2) or ""
            # 别名解析：@图片生成 → image_gen
            resolved_name = self.resolve_alias(raw_name)
            return RouteResult(
                route_type=RouteType.SKILL,
                skill_name=resolved_name,
                params={"args": args, "direct_call": True},
                confidence=1.0,
            )

        return None

    def _resolve_command(self, command: str, args: str) -> RouteResult:
        """将指令映射到 RouteResult"""
        if command in ("status",):
            return RouteResult(
                route_type=RouteType.TASK_STATUS,
                params={"task_id": args.strip() if args.strip() else None},
                confidence=1.0,
            )
        elif command in ("cancel",):
            return RouteResult(
                route_type=RouteType.TASK_OPERATION,
                params={"operation": "cancel", "task_id": args.strip()},
                confidence=1.0,
            )
        elif command in ("skills", "install", "uninstall", "config", "help"):
            return RouteResult(
                route_type=RouteType.SYSTEM,
                params={"command": command, "args": args},
                confidence=1.0,
            )
        else:
            # 未知指令 → 系统指令处理（会返回"未知命令"提示）
            return RouteResult(
                route_type=RouteType.SYSTEM,
                params={"command": command, "args": args, "unknown": True},
                confidence=1.0,
            )
