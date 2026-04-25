from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from skills.change_detector import main as change_detector


def test_change_detector_snapshot_outputs_structured_artifacts(tmp_path):
    file_path = tmp_path / "price.txt"
    file_path.write_text("price=3999", encoding="utf-8")

    original_dir = change_detector.SNAPSHOT_DIR
    change_detector.SNAPSHOT_DIR = tmp_path / "snapshots"
    change_detector.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = change_detector.cmd_snapshot({"path": str(file_path), "subcmd": "snapshot"})
    finally:
        change_detector.SNAPSHOT_DIR = original_dir

    artifacts = result.get("artifacts", {})
    assert result["changed"] is False
    assert artifacts.get("subcmd") == "snapshot"
    assert artifacts.get("target") == str(file_path)
    assert artifacts.get("path") == str(file_path)
    assert artifacts.get("snapshot_id")
    assert artifacts.get("content_hash")


def test_shell_exec_outputs_trace_artifacts():
    script = Path(__file__).resolve().parent.parent / "skills" / "shell_exec" / "main.py"
    payload = {
        "task_id": "task-shell-1",
        "origin_message": "执行 `echo hello`",
        "params": {"rest": "echo hello"},
    }
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    output = json.loads(proc.stdout.strip())
    artifacts = output.get("artifacts", {})
    assert artifacts.get("command") == "echo hello"
    assert artifacts.get("task_id") == "task-shell-1"
    assert artifacts.get("trace")
    assert "exit_code" in artifacts

