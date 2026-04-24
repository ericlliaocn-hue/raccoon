"""MemCore 单元测试"""

import pytest

from src.config import RaccoonConfig
from src.memcore.writer import MemCoreWriter
from src.memcore.reader import MemCoreReader
from src.memcore.lifecycle import MemCoreLifecycle
from src.types import MemoryEntry


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_memcore.db"


@pytest.fixture
def config(db_path):
    return RaccoonConfig(db_path=db_path)


@pytest.fixture
async def writer(config):
    w = MemCoreWriter(config)
    await w.init()
    yield w
    await w.close()


@pytest.fixture
async def reader(config):
    r = MemCoreReader(config)
    await r.init()
    yield r
    await r.close()


@pytest.mark.asyncio
async def test_write_and_read(writer, reader):
    entry = MemoryEntry(user_id="user1", key="name", value="Alice")
    await writer.write(entry)

    result = await reader.get("user1", "name")
    assert result is not None
    assert result.value == "Alice"


@pytest.mark.asyncio
async def test_write_preference(writer, reader):
    await writer.write_preference("user1", "language", "Chinese")
    result = await reader.get("user1", "language")
    assert result is not None
    assert result.value == "Chinese"
    assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_preference_conflict_archives_old(writer, reader):
    await writer.write_preference("user1", "language", "Chinese")
    await writer.write_preference("user1", "language", "English")

    # 新偏好
    result = await reader.get("user1", "language")
    assert result is not None
    assert result.value == "English"

    # 旧偏好已归档
    all_memories = await reader.get_all("user1", include_archived=True)
    assert len(all_memories) == 2
    archived = [m for m in all_memories if m.is_archived]
    assert len(archived) == 1
    assert archived[0].value == "Chinese"


@pytest.mark.asyncio
async def test_get_all(writer, reader):
    await writer.write(MemoryEntry(user_id="user1", key="k1", value="v1"))
    await writer.write(MemoryEntry(user_id="user1", key="k2", value="v2"))
    await writer.write(MemoryEntry(user_id="user2", key="k1", value="v3"))

    result = await reader.get_all("user1")
    assert len(result) == 2


@pytest.mark.asyncio
async def test_search(writer, reader):
    await writer.write(MemoryEntry(user_id="user1", key="favorite_color", value="blue"))
    await writer.write(MemoryEntry(user_id="user1", key="favorite_food", value="pizza"))

    result = await reader.search("user1", "favorite")
    assert len(result) == 2


@pytest.mark.asyncio
async def test_lifecycle_archive(config, writer, reader):
    entry = MemoryEntry(user_id="user1", key="temp", value="temporary")
    await writer.write(entry)

    lifecycle = MemCoreLifecycle(config)
    success = await lifecycle.archive(entry.memory_id)
    assert success

    # 归档后 get 不返回
    result = await reader.get("user1", "temp")
    assert result is None

    # 但 include_archived 可以看到
    all_mem = await reader.get_all("user1", include_archived=True)
    assert len(all_mem) == 1
    assert all_mem[0].is_archived


# ─── v0.3.4 新增测试 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_decay_archives_low_confidence(config, writer, reader):
    """#5: 衰减算法 — confidence 低于阈值的记忆被归档"""
    import datetime
    # 写入一条低置信度记忆，设置较旧的 updated_at
    entry = MemoryEntry(
        user_id="user1", key="old_memory", value="old_value",
        confidence=0.05,  # 很低的置信度
    )
    # 修改 updated_at 为 60 天前（让衰减生效）
    entry.updated_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
    await writer.write(entry)

    lifecycle = MemCoreLifecycle(config)
    archived = await lifecycle.decay(days=30, threshold=0.1)
    assert archived >= 1  # 至少归档了 1 条


@pytest.mark.asyncio
async def test_decay_reduces_confidence(config, writer, reader):
    """#5: 衰减算法 — 高置信度记忆 confidence 降低但不归档"""
    import datetime
    entry = MemoryEntry(
        user_id="user1", key="strong_memory", value="strong_value",
        confidence=0.9,
    )
    entry.updated_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
    await writer.write(entry)

    lifecycle = MemCoreLifecycle(config)
    await lifecycle.decay(days=30, threshold=0.1)
    # 高置信度记忆衰减后 confidence 降低但不会低于阈值
    result = await reader.get("user1", "strong_memory")
    assert result is not None
    assert result.confidence < 0.9  # confidence 应降低


@pytest.mark.asyncio
async def test_archive_unused(config, writer, reader):
    """#7: 归档长时间未使用且置信度低的记忆"""
    import datetime
    # 写入一条低置信度 + 未访问的记忆
    entry = MemoryEntry(
        user_id="user1", key="unused_memory", value="unused",
        confidence=0.2,
    )
    entry.updated_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
    await writer.write(entry)

    lifecycle = MemCoreLifecycle(config)
    archived = await lifecycle.archive_unused(days=30, confidence_threshold=0.3)
    assert archived >= 1


@pytest.mark.asyncio
async def test_access_tracking(config, writer, reader):
    """#6: 访问追踪 — 读取后 access_count 增加"""
    await writer.write(MemoryEntry(user_id="user1", key="track_test", value="hello"))

    # 第一次读取（_track_access 在 get 内部异步更新，返回的 entry 是更新前的快照）
    result = await reader.get("user1", "track_test")
    assert result is not None

    # 再次读取验证 access_count 已增加
    result2 = await reader.get("user1", "track_test")
    assert result2 is not None
    assert result2.access_count >= 1  # 至少被访问过一次


@pytest.mark.asyncio
async def test_access_stats(config, writer, reader):
    """#6: 复用率统计"""
    await writer.write(MemoryEntry(user_id="user1", key="stat1", value="v1"))
    await writer.write(MemoryEntry(user_id="user1", key="stat2", value="v2"))

    # 读取几次
    await reader.get("user1", "stat1")
    await reader.get("user1", "stat1")
    await reader.get("user1", "stat2")

    stats = await reader.get_access_stats("user1")
    assert stats["total_memories"] >= 2
    assert stats["total_accesses"] >= 3
    assert stats["reuse_rate"] > 0


@pytest.mark.asyncio
async def test_top_accessed(config, writer, reader):
    """#6: 高频访问记忆排行"""
    await writer.write(MemoryEntry(user_id="user1", key="freq1", value="frequent"))
    await writer.write(MemoryEntry(user_id="user1", key="freq2", value="less"))

    # 多次读取 freq1
    for _ in range(5):
        await reader.get("user1", "freq1")
    await reader.get("user1", "freq2")

    top = await reader.get_top_accessed("user1", limit=5)
    assert len(top) >= 2
    assert top[0].key == "freq1"  # freq1 访问最多


@pytest.mark.asyncio
async def test_learn_preference_from_behavior(config, writer, reader):
    """#8: 从用户行为学习偏好"""
    # 第一次行为 → 新偏好
    result = await writer.learn_preference_from_behavior("user1", "skill_used", "web_search")
    assert result is not None
    assert result.key == "frequent_skill"
    assert result.value == "web_search"

    # 验证偏好已写入
    pref = await reader.get("user1", "frequent_skill")
    assert pref is not None
    assert pref.value == "web_search"

    # 相同行为重复 → 不新增，只增加 access_count
    result2 = await writer.learn_preference_from_behavior("user1", "skill_used", "web_search")
    assert result2 is None  # 不返回新条目

    # 偏好变化 → 归档旧偏好，写入新偏好
    result3 = await writer.learn_preference_from_behavior("user1", "skill_used", "image_gen")
    assert result3 is not None
    assert result3.value == "image_gen"


@pytest.mark.asyncio
async def test_learn_preference_unknown_action(config, writer, reader):
    """#8: 未知行为类型不产生偏好"""
    result = await writer.learn_preference_from_behavior("user1", "unknown_action", "something")
    assert result is None
