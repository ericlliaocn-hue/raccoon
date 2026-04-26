import pytest

from src.jobs import JobArtifact, JobManager, JobStatus


@pytest.mark.asyncio
async def test_job_lifecycle_complete() -> None:
    manager = JobManager()
    job = await manager.create_job(
        kind="file_transfer",
        conversation_id="conv_1",
        user_id="user_1",
        message="开始传输",
    )
    assert job.status == JobStatus.QUEUED
    assert job.progress == 0

    running = await manager.mark_running(job.job_id, message="执行中")
    assert running
    assert running.status == JobStatus.RUNNING

    progressing = await manager.update_progress(job.job_id, progress=65, message="上传中")
    assert progressing
    assert progressing.progress == 65

    done = await manager.complete(
        job.job_id,
        message="已交付",
        artifacts=[JobArtifact(name="result.zip", size_bytes=256)],
    )
    assert done
    assert done.status == JobStatus.DELIVERED
    assert done.progress == 100
    assert len(done.artifacts) == 1


@pytest.mark.asyncio
async def test_job_fail_and_filter() -> None:
    manager = JobManager()
    ok = await manager.create_job(kind="video_render", conversation_id="conv_ok")
    bad = await manager.create_job(kind="video_render", conversation_id="conv_bad")

    await manager.mark_running(ok.job_id)
    await manager.complete(ok.job_id, message="ok")

    await manager.mark_running(bad.job_id)
    await manager.fail(bad.job_id, error="render_failed")

    failed = await manager.list_jobs(status=JobStatus.FAILED)
    assert len(failed) == 1
    assert failed[0].job_id == bad.job_id
    assert failed[0].error == "render_failed"

