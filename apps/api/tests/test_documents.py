from datetime import UTC, datetime

from manifest_files import file_state, save_manifest

from enigram.application.documents import documents_overview
from enigram.domain.job import Job

NOW = datetime(2026, 11, 22, tzinfo=UTC)


def _job(job_id: int, kind: str, payload: dict) -> Job:
    return Job(job_id, kind, payload, "running", None, None, NOW, NOW, None, "default")


class _Jobs:
    def __init__(self, jobs):
        self.jobs = jobs

    async def active_jobs(self):
        return self.jobs


async def test_a_repository_sync_is_not_listed_as_a_document(cfg):
    save_manifest(cfg, {"books": {"paper": {"title": "Paper", "status": "indexed"}}})
    jobs = _Jobs(
        [
            _job(1, "index_repo", {"url": "https://github.com/o/r", "ref": "main"}),
            _job(2, "index", {"book": "notes", "file": "notes.md"}),
        ]
    )

    docs = await documents_overview(cfg, file_state(cfg), jobs)

    assert [(d["book"], d["status"]) for d in docs] == [("notes", "running"), ("paper", "indexed")]
