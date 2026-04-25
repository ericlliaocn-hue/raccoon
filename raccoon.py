#!/usr/bin/env python3
"""浣熊 (Project Raccoon) CLI 入口

命令：
  raccoon                          # CLI 交互模式（前台）
  raccoon --http                   # HTTP Web UI 模式（前台）
  raccoon start                    # 后台启动
  raccoon stop                     # 停止
  raccoon restart                  # 重启
  raccoon status                   # 查看运行状态
  raccoon update                   # 自更新
  raccoon config [get|set|list]    # 配置管理
  raccoon skills [list|install|uninstall|info]  # Skill 管理
  raccoon logs                     # 查看日志
  raccoon doctor                   # 诊断检查
  raccoon benchmark core           # 核心场景基准报告
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="🦝 浣熊 - 事件驱动的 AI 智能体框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    # ─── start ────────────────────────────────────────────────
    p_start = sub.add_parser("start", help="后台启动 Raccoon")
    p_start.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_start.add_argument("--port", type=int, default=8900, help="监听端口")
    p_start.add_argument("--cli", action="store_true", help="以 CLI 模式启动（默认 HTTP）")

    # ─── stop ─────────────────────────────────────────────────
    sub.add_parser("stop", help="停止后台 Raccoon")

    # ─── restart ──────────────────────────────────────────────
    p_restart = sub.add_parser("restart", help="重启后台 Raccoon")
    p_restart.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_restart.add_argument("--port", type=int, default=8900, help="监听端口")
    p_restart.add_argument("--cli", action="store_true", help="以 CLI 模式重启")

    # ─── status ───────────────────────────────────────────────
    sub.add_parser("status", help="查看运行状态")

    # ─── update ───────────────────────────────────────────────
    p_update = sub.add_parser("update", help="自更新 Raccoon")
    p_update.add_argument("--version", default=None, help="指定版本号（默认最新）")
    p_update.add_argument("--check", action="store_true", help="仅检查是否有新版本")

    # ─── config ───────────────────────────────────────────────
    p_config = sub.add_parser("config", help="配置管理")
    p_config.add_argument("action", nargs="?", default="list", choices=["list", "get", "set"], help="操作")
    p_config.add_argument("key", nargs="?", default=None, help="配置键")
    p_config.add_argument("value", nargs="?", default=None, help="配置值（set 时需要）")

    # ─── skills ───────────────────────────────────────────────
    p_skills = sub.add_parser("skills", help="Skill 管理")
    p_skills.add_argument("action", nargs="?", default="list", choices=["list", "install", "uninstall", "info"], help="操作")
    p_skills.add_argument("target", nargs="?", default=None, help="Skill 名称或安装源")
    p_skills.add_argument("--force", action="store_true", help="强制安装（跳过冲突检测）")

    # ─── logs ─────────────────────────────────────────────────
    p_logs = sub.add_parser("logs", help="查看运行日志")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="显示行数")
    p_logs.add_argument("-f", "--follow", action="store_true", help="持续跟踪")
    p_logs.add_argument("--audit", action="store_true", help="查看审计日志")

    # ─── doctor ───────────────────────────────────────────────
    sub.add_parser("doctor", help="诊断检查")

    # ─── benchmark ────────────────────────────────────────────
    p_benchmark = sub.add_parser("benchmark", help="基准测试与报告")
    p_benchmark.add_argument("suite", nargs="?", default="core", choices=["core"], help="基准套件")
    p_benchmark.add_argument("--json", action="store_true", help="输出 JSON")
    p_benchmark.add_argument(
        "--store-only",
        action="store_true",
        help="仅输出当前 learning_runs 聚合，不运行双包基准",
    )

    # ─── schedule ─────────────────────────────────────────────
    p_schedule = sub.add_parser("schedule", help="定时任务管理")
    p_schedule.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "toggle"], help="操作")
    p_schedule.add_argument("--name", default=None, help="任务名称（add 时需要）")
    p_schedule.add_argument("--cron", default=None, help="cron 表达式，如 '0 8 * * *'")
    p_schedule.add_argument("--message", default=None, help="触发时发送的消息")
    p_schedule.add_argument("--id", default=None, dest="schedule_id", help="任务 ID（remove/toggle 时需要）")

    # ─── 前台模式参数 ─────────────────────────────────────────
    parser.add_argument("--http", action="store_true", help="前台启动 HTTP 模式")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8900, help="监听端口")

    args = parser.parse_args()

    # 分发
    dispatch = {
        "start": lambda: _cmd_start(args),
        "stop": _cmd_stop,
        "restart": lambda: _cmd_restart(args),
        "status": _cmd_status,
        "update": lambda: _cmd_update(args),
        "config": lambda: _cmd_config(args),
        "skills": lambda: _cmd_skills(args),
        "logs": lambda: _cmd_logs(args),
        "doctor": _cmd_doctor,
        "benchmark": lambda: _cmd_benchmark(args),
        "schedule": lambda: _cmd_schedule(args),
    }

    fn = dispatch.get(args.command)
    if fn:
        fn()
    elif args.http:
        _run_http(args.host, args.port)
    else:
        _run_cli()


# ═══════════════════════════════════════════════════════════════
# 子命令实现
# ═══════════════════════════════════════════════════════════════

def _cmd_start(args) -> None:
    from src.daemon import start
    ok = start(http=not args.cli, host=args.host, port=args.port)
    sys.exit(0 if ok else 1)


def _cmd_stop() -> None:
    from src.daemon import stop
    ok = stop()
    sys.exit(0 if ok else 1)


def _cmd_restart(args) -> None:
    from src.daemon import restart
    ok = restart(http=not args.cli, host=args.host, port=args.port)
    sys.exit(0 if ok else 1)


def _cmd_status() -> None:
    from src.daemon import status
    st = status()
    if st["running"]:
        print("🦝 Raccoon 运行中")
        print(f"   PID:   {st['pid']}")
        print(f"   模式:  {st['mode']}")
        print(f"   时长:  {st['uptime']}")
    else:
        print("🦝 Raccoon 未运行")


def _cmd_update(args) -> None:
    """自更新"""
    current = _get_version()

    if args.check:
        latest = _get_latest_version()
        if latest is None:
            print("❌ 无法检查新版本")
            sys.exit(1)
        if latest == current:
            print(f"✅ 已是最新版本 ({current})")
        else:
            print(f"🔄 有新版本可用: {current} → {latest}")
            print("   运行 `raccoon update` 更新")
        return

    # 执行更新
    print(f"🦝 当前版本: {current}")
    print("🔄 正在更新...")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "raccoon"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            new_ver = _get_version()
            print(f"✅ 更新完成: {current} → {new_ver}")
        else:
            print(f"❌ 更新失败:\n{result.stderr}")
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print("❌ 更新超时")
        sys.exit(1)


def _cmd_config(args) -> None:
    """配置管理"""
    from src.config import load_config, PROJECT_ROOT

    config_path = PROJECT_ROOT / "config.json"

    if args.action == "list":
        config = load_config()
        data = config.model_dump()
        for k, v in sorted(data.items()):
            # 隐藏敏感字段
            if "api_key" in k or "secret" in k:
                v = "***" + str(v)[-4:] if len(str(v)) > 4 else "***"
            print(f"  {k} = {v}")

    elif args.action == "get":
        if not args.key:
            print("❌ 请指定配置键: raccoon config get <key>")
            sys.exit(1)
        config = load_config()
        val = getattr(config, args.key, None)
        if val is None:
            print(f"❌ 未知配置键: {args.key}")
            sys.exit(1)
        if "api_key" in args.key or "secret" in args.key:
            val = "***" + str(val)[-4:] if len(str(val)) > 4 else "***"
        print(f"{args.key} = {val}")

    elif args.action == "set":
        if not args.key or args.value is None:
            print("❌ 用法: raccoon config set <key> <value>")
            sys.exit(1)

        # 读取现有配置
        if config_path.exists():
            with open(config_path) as f:
                data = json.load(f)
        else:
            data = {}

        # 类型转换
        value = args.value
        if value.lower() == "true":
            value = True
        elif value.lower() == "false":
            value = False
        elif value.isdigit():
            value = int(value)
        else:
            try:
                value = float(value)
            except ValueError:
                pass

        data[args.key] = value
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"✅ {args.key} = {value}")


def _cmd_skills(args) -> None:
    """Skill 管理"""
    from src.config import RaccoonConfig
    from src.skill_vault.vault_manager import VaultManager

    config = RaccoonConfig()
    vault = VaultManager(config)

    if args.action == "list":
        skills = vault.list_skills()
        if not skills:
            print("  (无已安装的 Skill)")
            return
        print(f"已安装 {len(skills)} 个 Skill:\n")
        for s in sorted(skills, key=lambda x: x.name):
            print(f"  📦 {s.name:<16} v{s.version:<8} {s.description}")

    elif args.action == "install":
        if not args.target:
            print("❌ 用法: raccoon skills install <git-url|local-path>")
            sys.exit(1)
        print(f"🔄 正在安装: {args.target}")
        try:
            meta = asyncio.run(vault.install(args.target))
            print(f"✅ 安装成功: {meta.name} v{meta.version}")
        except Exception as e:
            print(f"❌ 安装失败: {e}")
            sys.exit(1)

    elif args.action == "uninstall":
        if not args.target:
            print("❌ 用法: raccoon skills uninstall <skill-name>")
            sys.exit(1)
        try:
            vault.uninstall(args.target)
            print(f"✅ 已卸载: {args.target}")
        except KeyError as e:
            print(f"❌ {e}")
            sys.exit(1)

    elif args.action == "info":
        if not args.target:
            print("❌ 用法: raccoon skills info <skill-name>")
            sys.exit(1)
        meta = vault.get_skill(args.target)
        if not meta:
            print(f"❌ Skill 不存在: {args.target}")
            sys.exit(1)
        print(f"  名称:     {meta.name}")
        print(f"  版本:     {meta.version}")
        print(f"  描述:     {meta.description}")
        print(f"  入口:     {meta.entry}")
        triggers = ", ".join(meta.triggers) if meta.triggers else "(无)"
        print(f"  触发词:   {triggers}")
        print(f"  参数:     {json.dumps(meta.parameters, ensure_ascii=False, indent=4) if meta.parameters else '(无)'}")
        if meta.permissions:
            print(f"  权限:     {', '.join(meta.permissions)}")


def _cmd_logs(args) -> None:
    """查看日志"""
    from src.daemon import _PID_DIR

    if args.audit:
        # 审计日志
        log_dir = PROJECT_ROOT / "logs"
        log_files = sorted(log_dir.glob("audit_*.jsonl"), reverse=True)
        if not log_files:
            print("  (无审计日志)")
            return
        target = log_files[0]
        if args.follow:
            subprocess.run(["tail", "-f", "-n", str(args.lines), str(target)])
        else:
            subprocess.run(["tail", "-n", str(args.lines), str(target)])
    else:
        # 运行日志
        log_file = _PID_DIR / "raccoon.log"
        if not log_file.exists():
            print("  (无运行日志)")
            return
        if args.follow:
            subprocess.run(["tail", "-f", "-n", str(args.lines), str(log_file)])
        else:
            subprocess.run(["tail", "-n", str(args.lines), str(log_file)])


def _cmd_doctor() -> None:
    """诊断检查"""
    import importlib

    print("🦝 Raccoon 诊断检查\n")
    issues = []

    # 1. Python 版本
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        print(f"  ✅ Python {py_ver}")
    else:
        print(f"  ❌ Python {py_ver} (需要 >= 3.11)")
        issues.append("Python 版本过低")

    # 2. 依赖检查
    deps = [
        ("pydantic", "pydantic"),
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("structlog", "structlog"),
        ("openai", "openai"),
        ("httpx", "httpx"),
        ("psutil", "psutil"),
        ("click", "click"),
        ("rich", "rich"),
        ("aiosqlite", "aiosqlite"),
        ("bs4", "beautifulsoup4"),
        ("feedparser", "feedparser"),
        ("aiosmtplib", "aiosmtplib"),
    ]
    for mod, pkg in deps:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "?")
            print(f"  ✅ {pkg:<16} {ver}")
        except ImportError:
            print(f"  ❌ {pkg:<16} 未安装")
            issues.append(f"{pkg} 未安装")

    # 3. 配置文件
    config_path = PROJECT_ROOT / "config.json"
    if config_path.exists():
        print("  ✅ config.json 存在")
        try:
            from src.config import load_config
            config = load_config()
            if config.llm_api_key:
                print("  ✅ LLM API Key 已配置")
            else:
                print("  ⚠️  LLM API Key 未配置")
                issues.append("LLM API Key 未配置")
            if config.http_host == "127.0.0.1":
                print("  ✅ HTTP 默认仅监听本地")
            else:
                print(f"  ⚠️  HTTP 监听地址为 {config.http_host}，请确认已配置认证")
                issues.append("HTTP 未使用本地监听地址")
        except Exception as e:
            print(f"  ❌ 配置加载失败: {e}")
            issues.append("配置加载失败")
    else:
        print("  ⚠️  config.json 不存在（将使用默认配置）")

    # 3.5 版本同步检查
    version_sync = _collect_version_sync_state()
    if version_sync["ok"]:
        print(f"  ✅ 版本同步一致 ({version_sync['version']})")
    else:
        print("  ❌ 版本同步不一致")
        for detail in version_sync["details"]:
            print(f"     - {detail}")
        issues.append("版本号未同步")

    # 4. Skills 目录
    skills_dir = PROJECT_ROOT / "skills"
    if skills_dir.exists():
        skill_count = len([d for d in skills_dir.iterdir() if d.is_dir()])
        print(f"  ✅ Skills 目录 ({skill_count} 个)")
        try:
            import json
            from src.types import SkillMetadata

            invalid = 0
            for skill_dir in [d for d in skills_dir.iterdir() if d.is_dir()]:
                meta_path = skill_dir / "metadata.json"
                if not meta_path.exists():
                    invalid += 1
                    continue
                try:
                    SkillMetadata.model_validate(json.loads(meta_path.read_text(encoding="utf-8")))
                except Exception:
                    invalid += 1
            if invalid == 0:
                print("  ✅ Skill metadata 校验通过")
            else:
                print(f"  ⚠️  {invalid} 个 Skill metadata 无效")
                issues.append("存在无效 Skill metadata")
        except Exception as e:
            print(f"  ⚠️  Skill metadata 校验失败: {e}")
    else:
        print("  ❌ Skills 目录不存在")
        issues.append("Skills 目录不存在")

    # 5. 数据目录
    data_dir = PROJECT_ROOT / "data"
    if data_dir.exists():
        print("  ✅ 数据目录存在")
    else:
        print("  ⚠️  数据目录不存在（首次运行会自动创建）")

    # 5b. Learning staging
    try:
        from src.config import load_config
        config = load_config()
        staging_dir = config.learning_staging_dir
        if staging_dir.exists():
            leftovers = [p for p in staging_dir.iterdir() if p.is_dir()]
            if leftovers:
                print(f"  ⚠️  learning staging 残留 {len(leftovers)} 个目录")
            else:
                print("  ✅ learning staging 目录干净")
        else:
            print("  ✅ learning staging 尚未创建")
    except Exception as e:
        print(f"  ⚠️  learning staging 检查失败: {e}")

    # 5c. 学习失败 backlog
    try:
        backlog_file = config.learning_staging_dir.parent / "failure_backlog.jsonl"
        if backlog_file.exists():
            backlog_lines = sum(1 for _ in backlog_file.open("r", encoding="utf-8"))
            print(f"  ⚠️  learning failure backlog 累积 {backlog_lines} 条")
            if backlog_lines >= 20:
                issues.append("learning failure backlog 累积过多")
        else:
            print("  ✅ learning failure backlog 为空")
    except Exception as e:
        print(f"  ⚠️  learning failure backlog 检查失败: {e}")

    # 5d. LearningRun 运行质量
    try:
        from datetime import datetime, timezone, timedelta
        from src.brain.failure_guidance import failure_action
        from src.brain.learning_store import LearningRunStore
        from src.types import LearningRunStatus

        store = LearningRunStore(config)
        recent_runs = store.list_recent(limit=200)
        active_status = {
            LearningRunStatus.ANALYZING,
            LearningRunStatus.GENERATING,
            LearningRunStatus.VALIDATING,
            LearningRunStatus.PENDING_APPROVAL,
            LearningRunStatus.INSTALLING,
            LearningRunStatus.EXECUTING,
        }
        now = datetime.now(timezone.utc)
        stale = [
            run for run in recent_runs
            if run.status in active_status and (now - run.updated_at) > timedelta(minutes=30)
        ]
        if stale:
            print(f"  ⚠️  LearningRun 存在疑似卡死记录: {len(stale)}")
            issues.append("LearningRun 存在卡死记录")
        else:
            print("  ✅ LearningRun 无卡死记录")

        execution_runs = [r for r in recent_runs if r.execution_attempted]
        if execution_runs:
            recent_window = execution_runs[:50]
            previous_window = execution_runs[50:100]
            recent_rate = (
                sum(1 for r in recent_window if r.execution_success) / len(recent_window)
                if recent_window
                else 0.0
            )
            previous_rate = (
                sum(1 for r in previous_window if r.execution_success) / len(previous_window)
                if previous_window
                else None
            )
            if previous_rate is not None and previous_rate >= 0.5 and recent_rate + 0.1 < previous_rate:
                print(
                    "  ⚠️  最近执行成功率下滑: "
                    f"{recent_rate * 100:.1f}% (前一窗口 {previous_rate * 100:.1f}%)"
                )
                issues.append("execution_success 下滑")
            else:
                print(f"  ✅ 执行成功率稳定 ({recent_rate * 100:.1f}%)")
        else:
            print("  ℹ️  最近无执行样本，无法评估 execution_success 趋势")

        failures = [r for r in execution_runs if not r.execution_success and r.failure_code]
        if failures:
            counts: dict[str, int] = {}
            for run in failures:
                counts[run.failure_code or "unknown_error"] = counts.get(run.failure_code or "unknown_error", 0) + 1
            code, count = max(counts.items(), key=lambda item: item[1])
            ratio = count / len(failures)
            if ratio >= 0.4 and count >= 5:
                print(f"  ⚠️  失败码集中: {code} 占比 {ratio * 100:.1f}% ({count}/{len(failures)})")
                issues.append("失败码分布过于集中")
            else:
                print("  ✅ 失败码分布正常")
        else:
            print("  ✅ 最近无集中失败码")

        failure_topn = store.top_failure_clusters(days=7, limit=3)
        if failure_topn:
            print("  📌 失败码 Top3 修复建议:")
            for item in failure_topn:
                code = str(item.get("failure_code") or "unknown_error")
                print(
                    f"     - {code}: {item.get('failures', 0)} 次 / {item.get('conversations', 0)} 会话"
                    f" → {failure_action(code)}"
                )

        degraded: list[str] = []
        by_scenario: dict[str, list] = {}
        for run in execution_runs:
            if run.scenario_id:
                by_scenario.setdefault(run.scenario_id, []).append(run)
        for scenario_id, runs in by_scenario.items():
            recent = runs[:20]
            previous = runs[20:40]
            if len(recent) < 5 or len(previous) < 5:
                continue
            recent_rate = sum(1 for r in recent if r.execution_success) / len(recent)
            previous_rate = sum(1 for r in previous if r.execution_success) / len(previous)
            if recent_rate + 0.15 < previous_rate:
                degraded.append(
                    f"{scenario_id}({recent_rate * 100:.1f}%<-{previous_rate * 100:.1f}%)"
                )
        if degraded:
            print(f"  ⚠️  场景退化告警: {', '.join(degraded)}")
            issues.append("核心场景执行成功率退化")
        else:
            print("  ✅ 核心场景无明显退化")

        candidates = store.list_new_skill_candidates(days=7, min_failures=5, min_conversations=2, limit=20)
        if candidates:
            print(f"  ⚠️  新增 Skill 候选池待处理: {len(candidates)}")
        else:
            print("  ✅ 新增 Skill 候选池为空")
    except Exception as e:
        print(f"  ⚠️  LearningRun 质量检查失败: {e}")

    # 5c. MemCore 可读写
    try:
        from src.memcore.writer import MemCoreWriter
        from src.config import load_config

        config = load_config()
        writer = MemCoreWriter(config)
        asyncio.run(writer.init())
        asyncio.run(writer.close())
        print("  ✅ MemCore 可初始化")
    except Exception as e:
        print(f"  ❌ MemCore 初始化失败: {e}")
        issues.append("MemCore 初始化失败")

    # 5d. 浏览器会话池健康（仅检查统计，不触发启动）
    try:
        from skills.web_automate.session_manager import get_session_manager

        stats = get_session_manager().get_stats()
        idle = int(stats.get("idle", 0))
        total = int(stats.get("total", 0))
        max_per_mode = int(stats.get("max_sessions_per_mode", 3))
        if total > max_per_mode * 3:
            print(f"  ⚠️  浏览器会话偏多: total={total}, idle={idle}")
            issues.append("浏览器会话回收异常")
        else:
            print(f"  ✅ 浏览器会话池正常 (total={total}, idle={idle})")
    except Exception as e:
        print(f"  ⚠️  浏览器会话检查失败: {e}")

    # 6. 端口检查
    import socket
    port = 8900
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(("127.0.0.1", port))
    sock.close()
    if result == 0:
        print(f"  ⚠️  端口 {port} 已被占用（Raccoon 可能在运行）")
    else:
        print(f"  ✅ 端口 {port} 可用")

    # 7. 运行状态
    from src.daemon import status
    st = status()
    if st["running"]:
        print(f"  ✅ Raccoon 运行中 (PID: {st['pid']})")
    else:
        print("  ℹ️  Raccoon 未运行")

    # 总结
    print()
    if issues:
        print(f"⚠️  发现 {len(issues)} 个问题:")
        for i, issue in enumerate(issues, 1):
            print(f"   {i}. {issue}")
    else:
        print("✅ 一切正常！")


def _cmd_benchmark(args) -> None:
    """核心场景基准报告。默认跑双包夹具基准并输出 mismatch。"""
    if args.suite != "core":
        print(f"❌ 不支持的基准套件: {args.suite}")
        sys.exit(1)

    from src.config import load_config

    config = load_config()
    if args.store_only:
        from src.brain.core_benchmark import build_core_scenario_report
        from src.brain.learning_store import LearningRunStore

        store = LearningRunStore(config)
        report = build_core_scenario_report(
            store.aggregate_core_scenarios(),
            store.top_failure_clusters(days=7, limit=10),
        )
        if args.json:
            print(json.dumps({"mode": "store_only", "benchmark_report": report}, ensure_ascii=False, indent=2))
            sys.exit(0 if report["overall"]["pass"] else 2)

        overall = report["overall"]
        print("🧪 核心场景基准报告（store_only）\n")
        print(f"  样本数: {overall['runs']}")
        print(f"  决策成功率: {overall['decision_success_rate'] * 100:.1f}%")
        print(f"  执行成功率: {overall['execution_success_rate'] * 100:.1f}%")
        print(f"  浏览器链路执行成功率: {overall['browser_chain_execution_success_rate'] * 100:.1f}%")
        print(f"  卡死率: {overall['stuck_rate'] * 100:.2f}%")
        print(f"  发布门禁: {'✅ 通过' if overall['pass'] else '❌ 未通过'}")
        sys.exit(0 if overall["pass"] else 2)

    from src.brain.core_benchmark_runner import run_core_benchmark_packs_sync

    bundle = run_core_benchmark_packs_sync(config)
    report = bundle["benchmark_report"]
    if args.json:
        print(json.dumps(bundle, ensure_ascii=False, indent=2))
        sys.exit(0 if report["overall"]["pass"] else 2)

    overall = report["overall"]
    pack_overall = bundle.get("overall", {})
    print("🧪 核心场景基准报告（双包夹具）\n")
    print(f"  双包样本: {pack_overall.get('total_cases', 0)}")
    print(f"  双包匹配率: {pack_overall.get('pack_match_rate', 0.0) * 100:.1f}%")
    print(f"  mismatch 数量: {pack_overall.get('mismatch_count', 0)}")
    print(f"  learning_runs 样本数: {overall['runs']}")
    print(f"  决策成功率: {overall['decision_success_rate'] * 100:.1f}%")
    print(f"  执行成功率: {overall['execution_success_rate'] * 100:.1f}%")
    print(f"  浏览器链路执行成功率: {overall['browser_chain_execution_success_rate'] * 100:.1f}%")
    print(f"  卡死率: {overall['stuck_rate'] * 100:.2f}%")
    print(f"  发布门禁: {'✅ 通过' if overall['pass'] else '❌ 未通过'}")
    print()
    print("场景明细:")
    for row in report["scenarios"]:
        outcomes = row.get("handling_outcomes", {})
        print(
            f"  - {row['name']:<10} runs={row['runs']:<3d} "
            f"decision={row['decision_success_rate'] * 100:>5.1f}% "
            f"exec={row['execution_success_rate'] * 100:>5.1f}% "
            f"stuck={row['stuck_rate'] * 100:>4.1f}% "
            f"repair={row['avg_repair_count']:.2f} "
            f"quality={row['avg_quality_score']:.2f} "
            f"outcomes(c/r/e/f)={outcomes.get('clarified', 0)}/"
            f"{outcomes.get('reused', 0)}/"
            f"{outcomes.get('executed', 0)}/"
            f"{outcomes.get('failed', 0)}"
        )

    if report.get("failure_code_topn"):
        print("\nfailure_code TopN:")
        for item in report["failure_code_topn"]:
            print(
                f"  - {item.get('failure_code', 'unknown')}: "
                f"{item.get('failures', 0)} 次 / {item.get('conversations', 0)} 会话"
            )
            if item.get("action"):
                print(f"    修复动作: {item.get('action')}")

    if bundle.get("mismatches"):
        print("\nmismatch Top10:")
        for item in bundle["mismatches"][:10]:
            print(
                f"  - [{item.get('pack_id')}/{item.get('case_id')}] "
                f"expected={item.get('expected_outcome')} actual={item.get('actual_outcome')}"
                + (
                    f" reason={item.get('actual_reason')}"
                    if item.get("actual_reason")
                    else ""
                )
            )

    if not overall["pass"]:
        print("\n⚠️ 未达平衡档门槛，建议先修复集中失败码后再打版本标签。")
    sys.exit(0 if overall["pass"] else 2)


def _cmd_schedule(args) -> None:
    """定时任务管理"""
    from src.config import RaccoonConfig
    from src.scheduler.schedule_store import ScheduleStore
    from src.scheduler.cron_parser import CronParser, CronParseError
    from src.types import ScheduleEntry

    config = RaccoonConfig()
    store = ScheduleStore(config)
    store.recover()

    if args.action == "list":
        schedules = list(store.all_schedules())
        if not schedules:
            print("  (无定时任务)")
            return
        print(f"已创建 {len(schedules)} 个定时任务:\n")
        for s in sorted(schedules, key=lambda x: x.created_at):
            status = "✅" if s.enabled else "⏸️"
            next_str = s.next_run.strftime("%Y-%m-%d %H:%M") if s.next_run else "?"
            last_str = s.last_run.strftime("%Y-%m-%d %H:%M") if s.last_run else "-"
            print(f"  {status} [{s.schedule_id[:8]}] {s.name}")
            print(f"     cron: {s.cron}  消息: {s.message}")
            print(f"     下次: {next_str}  上次: {last_str}")

    elif args.action == "add":
        if not args.name or not args.cron or not args.message:
            print("❌ 用法: raccoon schedule add --name <名称> --cron <表达式> --message <消息>")
            sys.exit(1)
        try:
            CronParser(args.cron)
        except CronParseError as e:
            print(f"❌ 无效的 cron 表达式: {e}")
            sys.exit(1)
        entry = ScheduleEntry(
            name=args.name,
            cron=args.cron,
            message=args.message,
            conversation_id=f"schedule_{args.name}",
        )
        # 预计算 next_run
        try:
            from datetime import datetime, timezone
            cron = CronParser(args.cron)
            entry.next_run = cron.next_time(datetime.now(timezone.utc))
        except Exception:
            pass
        store.add(entry)
        next_str = entry.next_run.strftime("%Y-%m-%d %H:%M") if entry.next_run else "?"
        print(f"✅ 定时任务已创建: {entry.name}")
        print(f"   ID: {entry.schedule_id}")
        print(f"   cron: {entry.cron}")
        print(f"   下次触发: {next_str}")

    elif args.action == "remove":
        sid = args.schedule_id
        if not sid:
            print("❌ 用法: raccoon schedule remove --id <任务ID>")
            sys.exit(1)
        entry = store.remove(sid)
        if entry:
            print(f"✅ 已删除定时任务: {entry.name}")
        else:
            print(f"❌ 未找到任务: {sid}")

    elif args.action == "toggle":
        sid = args.schedule_id
        if not sid:
            print("❌ 用法: raccoon schedule toggle --id <任务ID>")
            sys.exit(1)
        entry = store.get(sid)
        if not entry:
            print(f"❌ 未找到任务: {sid}")
            sys.exit(1)
        entry.enabled = not entry.enabled
        store.update(entry)
        status = "启用" if entry.enabled else "禁用"
        print(f"✅ 已{status}定时任务: {entry.name}")


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _collect_version_sync_state() -> dict[str, object]:
    versions: dict[str, str] = {}
    details: list[str] = []

    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    init_path = PROJECT_ROOT / "src" / "__init__.py"
    index_path = PROJECT_ROOT / "src" / "adapters" / "static" / "index.html"
    changelog_path = PROJECT_ROOT / "CHANGELOG.md"

    try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
        versions["pyproject"] = str(data.get("project", {}).get("version", "")).strip()
    except Exception as e:
        details.append(f"读取 pyproject.toml 失败: {e}")

    try:
        text = init_path.read_text(encoding="utf-8")
        versions["src_init"] = _first_match(text, r'__version__\s*=\s*"([^"]+)"')
    except Exception as e:
        details.append(f"读取 src/__init__.py 失败: {e}")

    try:
        text = index_path.read_text(encoding="utf-8")
        versions["static_css"] = _first_match(text, r"/static/style\.css\?v=([0-9A-Za-z_.-]+)")
        versions["static_js"] = _first_match(text, r"/static/app\.js\?v=([0-9A-Za-z_.-]+)")
        versions["ui_badge"] = _first_match(text, r"Project Raccoon v([0-9A-Za-z_.-]+)")
    except Exception as e:
        details.append(f"读取 static/index.html 失败: {e}")

    try:
        text = changelog_path.read_text(encoding="utf-8")
        versions["changelog_latest"] = _first_match(text, r"^## \[([^\]]+)\] - ")
    except Exception as e:
        details.append(f"读取 CHANGELOG.md 失败: {e}")

    missing = [name for name, value in versions.items() if not value]
    if missing:
        details.append(f"缺少版本值: {', '.join(missing)}")

    unique_versions = sorted({value for value in versions.values() if value})
    if len(unique_versions) > 1:
        ordered = ", ".join(f"{name}={value}" for name, value in versions.items())
        details.append(f"检测到多个版本值: {ordered}")

    version = unique_versions[0] if len(unique_versions) == 1 else "unknown"
    return {"ok": not details, "version": version, "details": details, "versions": versions}


def _get_version() -> str:
    """获取当前版本"""
    try:
        from importlib.metadata import version
        return version("raccoon")
    except Exception:
        return "0.1.0"


def _get_latest_version() -> str | None:
    """查询最新版本"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", "raccoon"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            # 解析输出
            for line in result.stdout.splitlines():
                if "Available versions:" in line or "LATEST:" in line:
                    parts = line.split()
                    for p in parts:
                        p = p.strip(",()")
                        if p and p[0].isdigit():
                            return p
    except Exception:
        pass
    return None


def _run_cli() -> None:
    """前台 CLI 模式"""
    from src.adapters.cli_adapter import run_cli
    run_cli()


def _run_http(host: str, port: int) -> None:
    """前台 HTTP 模式"""
    import uvicorn
    from src.adapters.http_adapter import create_app
    from src.config import load_config

    config = load_config()
    app = create_app(config)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
