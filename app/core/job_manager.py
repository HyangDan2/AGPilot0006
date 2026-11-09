import concurrent.futures
import logging
import threading
import uuid
from typing import Callable, Any, Optional
from app.config import settings
from app.core.database import JobRepository, DocumentRepository

logger = logging.getLogger("document_intelligence.job_manager")

class JobManager:
    """
    Manages asynchronous document processing jobs with:
    - Persistent SQLite state
    - Concurrency limiting via threading.BoundedSemaphore
    - Timeout enforcement (PARSING_TIMEOUT_SECONDS)
    - Recovery of orphaned jobs on startup
    """
    _semaphore: Optional[threading.BoundedSemaphore] = None

    @classmethod
    def get_semaphore(cls) -> threading.BoundedSemaphore:
        if cls._semaphore is None:
            cls._semaphore = threading.BoundedSemaphore(settings.MAX_CONCURRENT_JOBS)
        return cls._semaphore

    @classmethod
    def recover_on_startup(cls) -> int:
        """Called at application startup to mark dangling jobs as interrupted."""
        recovered = JobRepository.recover_interrupted_jobs()
        if recovered:
            logger.warning(f"Recovered {len(recovered)} interrupted jobs from prior shutdown: {recovered}")
        return len(recovered)

    @classmethod
    def run_job(
        cls,
        document_id: str,
        job_fn: Callable[[str], Any]
    ) -> None:
        """
        Executes a document parsing job under semaphore and timeout.
        Updates persistent JobRepository and DocumentRepository status.
        """
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        JobRepository.create_job(job_id, document_id)

        sem = cls.get_semaphore()
        with sem:
            JobRepository.mark_started(job_id)
            try:
                # Execute with strict timeout enforcement
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(job_fn, document_id)
                    future.result(timeout=float(settings.PARSING_TIMEOUT_SECONDS))
                JobRepository.mark_completed(job_id)
            except concurrent.futures.TimeoutError:
                err_msg = f"Parsing timed out after {settings.PARSING_TIMEOUT_SECONDS} seconds"
                logger.error(f"Job {job_id} for document {document_id} failed: {err_msg}")
                JobRepository.mark_failed(job_id, err_msg)
                DocumentRepository.update_status(document_id, status="failed", error_message=err_msg)
            except Exception as e:
                err_msg = f"{type(e).__name__}: {str(e)}"
                logger.error(f"Job {job_id} for document {document_id} failed: {err_msg}", exc_info=True)
                JobRepository.mark_failed(job_id, err_msg)
                DocumentRepository.update_status(document_id, status="failed", error_message=err_msg)
