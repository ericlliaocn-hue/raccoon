"""system_info Skill - 查看系统信息

支持查看：CPU/内存/磁盘/进程/网络/系统版本

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [] }
"""
import json
import os
import platform
import subprocess
import sys
from datetime import timedelta


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    if b < 1048576:
        return f"{b / 1024:.1f}KB"
    if b < 1073741824:
        return f"{b / 1048576:.1f}MB"
    return f"{b / 1073741824:.1f}GB"


def _get_cpu_info() -> str:
    try:
        import psutil
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_count_logical = psutil.cpu_count(logical=True)
        cpu_count_physical = psutil.cpu_count(logical=False)
        freq = psutil.cpu_freq()
        freq_str = f" / 频率 {freq.current:.0f}MHz" if freq else ""
        return f"  CPU 使用率：{cpu_percent}%\n  核心数：{cpu_count_physical} 物理 / {cpu_count_logical} 逻辑{freq_str}"
    except ImportError:
        # fallback: 用 top 命令
        try:
            out = subprocess.check_output(
                ["top", "-l", "1", "-n", "0"],
                timeout=5, text=True,
            )
            for line in out.splitlines():
                if "CPU usage" in line:
                    return f"  {line.strip()}"
        except Exception:
            pass
        return "  CPU 信息：psutil 未安装，无法获取"


def _get_memory_info() -> str:
    try:
        import psutil
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        lines = [
            f"  内存：{_fmt_size(mem.used)} / {_fmt_size(mem.total)}（{mem.percent}%）",
            f"  交换：{_fmt_size(swap.used)} / {_fmt_size(swap.total)}（{swap.percent}%）",
        ]
        return "\n".join(lines)
    except ImportError:
        try:
            out = subprocess.check_output(
                ["sysctl", "hw.memsize"], timeout=3, text=True,
            )
            total = int(out.split(":")[1].strip())
            return f"  内存总量：{_fmt_size(total)}\n  （安装 psutil 可查看详细使用情况）"
        except Exception:
            return "  内存信息：psutil 未安装，无法获取"


def _get_disk_info() -> str:
    try:
        import psutil
        lines = []
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                lines.append(
                    f"  {part.mountpoint}：{_fmt_size(usage.used)} / {_fmt_size(usage.total)}（{usage.percent}%）"
                )
            except (PermissionError, OSError):
                continue
        return "\n".join(lines) if lines else "  磁盘信息：无法获取"
    except ImportError:
        try:
            out = subprocess.check_output(["df", "-h"], timeout=3, text=True)
            lines = out.splitlines()[:6]  # 限制行数
            return "\n".join(f"  {l}" for l in lines)
        except Exception:
            return "  磁盘信息：psutil 未安装，无法获取"


def _get_process_info() -> str:
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]):
            try:
                info = p.info
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # 按内存排序取 top 10
        procs.sort(key=lambda x: x.get("memory_percent") or 0, reverse=True)
        top = procs[:10]

        lines = [f"  进程总数：{len(procs)}，Top 10（按内存）："]
        for p in top:
            mem = p.get("memory_percent") or 0
            lines.append(f"    [{p['pid']}] {p['name']:<30s} 内存 {mem:.1f}%")
        return "\n".join(lines)
    except ImportError:
        try:
            out = subprocess.check_output(
                ["ps", "aux", "-r"], timeout=3, text=True,
            )
            lines = out.splitlines()[:11]
            return "\n".join(f"  {l}" for l in lines)
        except Exception:
            return "  进程信息：psutil 未安装，无法获取"


def _get_network_info() -> str:
    try:
        import psutil
        addrs = psutil.net_if_addrs()
        lines = []
        for iface, addr_list in addrs.items():
            for addr in addr_list:
                if addr.family.name == "AF_INET":
                    lines.append(f"  {iface}：{addr.address}")
        return "\n".join(lines) if lines else "  网络信息：未检测到"
    except ImportError:
        try:
            out = subprocess.check_output(
                ["ifconfig"], timeout=3, text=True,
            )
            lines = []
            for line in out.splitlines():
                if "inet " in line and "127.0.0.1" not in line:
                    lines.append(f"  {line.strip()}")
            return "\n".join(lines) if lines else "  网络信息：无法获取"
        except Exception:
            return "  网络信息：psutil 未安装，无法获取"


def _get_uptime() -> str:
    try:
        import psutil
        boot_time = psutil.boot_time()
        uptime = timedelta(seconds=int(__import__("time").time() - boot_time))
        return f"  运行时间：{uptime}"
    except ImportError:
        return ""


def _parse_focus(text: str) -> str:
    """从消息中解析关注点"""
    text = text.lower()
    if "cpu" in text:
        return "cpu"
    if "内存" in text or "memory" in text:
        return "memory"
    if "磁盘" in text or "disk" in text or "硬盘" in text:
        return "disk"
    if "进程" in text or "process" in text:
        return "process"
    if "网络" in text or "network" in text or "ip" in text:
        return "network"
    return "all"


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {})

    raw = (params.get("rest") or origin).strip()
    focus = _parse_focus(raw)

    # 系统基本信息
    sys_name = platform.system()
    sys_release = platform.release()
    sys_version = platform.mac_ver()[0] if sys_name == "Darwin" else platform.version()
    hostname = platform.node()
    arch = platform.machine()

    parts = []
    parts.append(f"🖥️ 系统信息")
    parts.append(f"  系统：{sys_name} {sys_release}（版本 {sys_version}）")
    parts.append(f"  主机：{hostname}")
    parts.append(f"  架构：{arch}")
    parts.append(f"  用户：{os.getenv('USER', 'unknown')}")

    uptime = _get_uptime()
    if uptime:
        parts.append(uptime)

    if focus in ("all", "cpu"):
        parts.append("")
        parts.append("⚡ CPU")
        parts.append(_get_cpu_info())

    if focus in ("all", "memory"):
        parts.append("")
        parts.append("💾 内存")
        parts.append(_get_memory_info())

    if focus in ("all", "disk"):
        parts.append("")
        parts.append("💿 磁盘")
        parts.append(_get_disk_info())

    if focus in ("all", "process"):
        parts.append("")
        parts.append("📋 进程")
        parts.append(_get_process_info())

    if focus in ("all", "network"):
        parts.append("")
        parts.append("🌐 网络")
        parts.append(_get_network_info())

    result = {
        "task_id": task_id,
        "reply": "\n".join(parts),
        "files": [],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
