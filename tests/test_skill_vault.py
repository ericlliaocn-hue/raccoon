"""SkillVault 单元测试"""

import json
import pytest
from pathlib import Path

from src.config import RaccoonConfig
from src.skill_vault.vault_manager import VaultManager
from src.skill_vault.conflict_detector import ConflictDetector
from src.types import SkillMetadata


# ─── ConflictDetector ──────────────────────────────────────────

@pytest.fixture
def detector():
    return ConflictDetector()


def test_no_conflict(detector):
    new = SkillMetadata(name="skill_a", permissions=["api_key:KEY_A"])
    existing = SkillMetadata(name="skill_b", permissions=["api_key:KEY_B"])
    reports = detector.scan(new, [existing])
    assert len(reports) == 0


def test_api_key_conflict(detector):
    new = SkillMetadata(name="skill_a", permissions=["api_key:OPENAI_KEY"])
    existing = SkillMetadata(name="skill_b", permissions=["api_key:OPENAI_KEY"])
    reports = detector.scan(new, [existing])
    assert len(reports) == 1
    assert reports[0].conflict_type == "api_key"


def test_network_conflict(detector):
    new = SkillMetadata(name="skill_a", permissions=["network:full"])
    existing = SkillMetadata(name="skill_b", permissions=["network:readonly"])
    reports = detector.scan(new, [existing])
    assert len(reports) == 1
    assert reports[0].conflict_type == "network"


# ─── VaultManager ──────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path):
    config = RaccoonConfig(skills_dir=tmp_path / "skills")
    return VaultManager(config)


def test_list_skills_empty(vault):
    assert vault.list_skills() == []


def test_install_from_local(vault, tmp_path):
    # 创建临时 Skill 目录
    skill_dir = tmp_path / "test_skill"
    skill_dir.mkdir()
    meta = {
        "name": "test_skill",
        "version": "0.1.0",
        "description": "Test skill",
        "trigger_words": ["test"],
    }
    (skill_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (skill_dir / "main.py").write_text(
        'import json, sys\ndata = json.loads(sys.stdin.read())\nprint(json.dumps({"echo": data.get("origin_message", "")}))',
        encoding="utf-8",
    )

    result = vault._install_from_local(str(skill_dir))
    assert result.name == "test_skill"
    assert len(vault.list_skills()) == 1


def test_get_skill(vault, tmp_path):
    skill_dir = tmp_path / "my_skill"
    skill_dir.mkdir()
    meta = {"name": "my_skill", "version": "1.0.0", "trigger_words": ["my"]}
    (skill_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    vault._install_from_local(str(skill_dir))
    skill = vault.get_skill("my_skill")
    assert skill is not None
    assert skill.version == "1.0.0"


def test_uninstall(vault, tmp_path):
    skill_dir = tmp_path / "to_remove"
    skill_dir.mkdir()
    meta = {"name": "to_remove", "version": "0.1.0", "trigger_words": []}
    (skill_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    vault._install_from_local(str(skill_dir))
    assert len(vault.list_skills()) == 1

    vault.uninstall("to_remove")
    assert len(vault.list_skills()) == 0


def test_uninstall_not_found(vault):
    with pytest.raises(KeyError):
        vault.uninstall("nonexistent")
