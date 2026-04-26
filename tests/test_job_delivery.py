from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import RaccoonConfig
from src.jobs import JobArtifact, JobDeliveryCoordinator, JobDeliveryState, JobManager, JobStatus, JobStore


@pytest.mark.asyncio
async def test_job_store_persistence_round_trip(tmp_path) -> None:
    config = RaccoonConfig(db_path=tmp_path / "data" / "jobs.db")
    store = JobStore(config)
    manager = JobManager(store=store)

    created = await manager.create_job(
        kind="file_transfer",
        conversation_id="conv_a",
        delivery_required=True,
        delivery_max_attempts=3,
        message="准备中",
    )
    await manager.mark_running(created.job_id, message="执行中")
    completed = await manager.complete(
        created.job_id,
        message="结果生成完成",
        artifacts=[JobArtifact(name="report.txt", size_bytes=16)],
    )

    assert completed
    assert completed.status == JobStatus.UPLOADING
    assert completed.delivery_state == JobDeliveryState.PENDING

    reloaded = store.get(created.job_id)
    assert reloaded
    assert reloaded.kind == "file_transfer"
    assert reloaded.delivery_state == JobDeliveryState.PENDING
    assert len(reloaded.artifacts) == 1
    assert reloaded.artifacts[0].name == "report.txt"


@pytest.mark.asyncio
async def test_delivery_coordinator_retry_then_dlq(tmp_path) -> None:
    config = RaccoonConfig(db_path=tmp_path / "data" / "jobs_retry.db")
    store = JobStore(config)
    manager = JobManager(store=store)

    job = await manager.create_job(
        kind="video_render",
        conversation_id="conv_retry",
        delivery_required=True,
        delivery_max_attempts=2,
    )
    await manager.complete(job.job_id, message="渲染完成", artifacts=[JobArtifact(name="video.mp4")])

    async def _failing_delivery(_job):
        raise RuntimeError("push_failed")

    coordinator = JobDeliveryCoordinator(
        manager,
        store,
        config,
        delivery_handler=_failing_delivery,
        poll_interval_seconds=0.2,
    )

    await coordinator.run_once(now=datetime.now(timezone.utc))
    after_first = await manager.get_job(job.job_id)
    assert after_first
    assert after_first.status == JobStatus.UPLOADING
    assert after_first.delivery_state == JobDeliveryState.RETRY
    assert after_first.delivery_attempts == 1
    assert after_first.next_delivery_at is not None

    await coordinator.run_once(now=datetime.now(timezone.utc) + timedelta(minutes=10))
    after_second = await manager.get_job(job.job_id)
    assert after_second
    assert after_second.status == JobStatus.FAILED
    assert after_second.delivery_state == JobDeliveryState.DLQ
    assert after_second.delivery_attempts == 2
    assert after_second.error == "push_failed"


@pytest.mark.asyncio
async def test_delivery_coordinator_marks_sent(tmp_path) -> None:
    config = RaccoonConfig(db_path=tmp_path / "data" / "jobs_sent.db")
    store = JobStore(config)
    manager = JobManager(store=store)

    job = await manager.create_job(
        kind="file_transfer",
        conversation_id="conv_sent",
        delivery_required=True,
    )
    await manager.complete(job.job_id, message="文件已生成", artifacts=[JobArtifact(name="bundle.zip")])

    delivered_ids: list[str] = []

    async def _ok_delivery(_job):
        delivered_ids.append(_job.job_id)

    coordinator = JobDeliveryCoordinator(
        manager,
        store,
        config,
        delivery_handler=_ok_delivery,
    )
    await coordinator.run_once(now=datetime.now(timezone.utc))

    done = await manager.get_job(job.job_id)
    assert done
    assert done.status == JobStatus.DELIVERED
    assert done.delivery_state == JobDeliveryState.SENT
    assert delivered_ids == [job.job_id]
