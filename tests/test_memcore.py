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
