from __future__ import annotations

import asyncio
from pathlib import Path

from src.brain.core_benchmark_runner import run_core_benchmark_packs
from src.config import RaccoonConfig


def _config(tmp_path: Path) -> RaccoonConfig:
    return RaccoonConfig(
        db_path=tmp_path / "data" / "memcore.db",
        skills_dir=tmp_path / "skills",
        learning_staging_dir=tmp_path / "data" / "learning" / "staging",
    )


def test_core_benchmark_runner_runs_dual_pack_and_generates_non_empty_report(tmp_path: Path):
    result = asyncio.run(run_core_benchmark_packs(_config(tmp_path)))

    assert result["overall"]["total_cases"] == 24
    assert result["overall"]["matched_cases"] >= 20
    assert result["overall"]["mismatch_count"] == len(result["mismatches"])
    assert len(result["packs"]) == 2
    assert all(pack["total"] > 0 for pack in result["packs"])

    report = result["benchmark_report"]
    assert report["overall"]["runs"] > 0
    assert "execution_success_rate" in report["overall"]
    assert "browser_chain_execution_success_rate" in report["overall"]
