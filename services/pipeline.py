"""Pipeline manager — runs discovery + enrichment as a background pipeline.

This is the core engine that the API uses to execute scraping jobs.
Each job runs as a background asyncio task with its own DB sessions.
"""

import asyncio
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from core.browser_manager import get_browser_manager
from core.config import settings
from core.logging import get_logger
from db.session import AsyncSessionLocal
from db.models.job import Job, JobStatus
from db.models.collection import Collection
from db.models.lead import Lead
from services.discovery import DiscoveryService
from services.enrichment import EnrichmentService
from services.enrichment.enrichment import clean_phone

logger = get_logger(__name__)


def detect_country_iso(address: str | None, area: str | None) -> str:
    """Detect the 2-letter ISO country code from address or search area."""
    full_text = f"{address or ''} {area or ''}".lower()
    
    if any(x in full_text for x in ["usa", "united states", "america"]):
        return "US"
    if any(x in full_text for x in ["uk", "united kingdom", "london", "great britain", "england"]):
        return "GB"
    if "canada" in full_text:
        return "CA"
    if "germany" in full_text:
        return "DE"
    if "australia" in full_text:
        return "AU"
    if "singapore" in full_text:
        return "SG"
    if any(x in full_text for x in ["uae", "dubai", "emirates", "united arab emirates"]):
        return "AE"
    
    # Check for Indian states / cities in address
    indian_states = [
        "tamil nadu", "karnataka", "maharashtra", "kerala", "andhra pradesh",
        "telangana", "west bengal", "rajasthan", "uttar pradesh", "gujarat",
        "madhya pradesh", "bihar", "punjab", "haryana", "odisha", "assam",
        "delhi", "coimbatore", "chennai", "bangalore", "mumbai"
    ]
    if any(s in full_text for s in indian_states) or "india" in full_text:
        return "IN"
        
    return "IN"

# How many leads to buffer before flushing to DB
COMMIT_BATCH_SIZE = 1

# Max queue size for backpressure between discovery → enrichment
QUEUE_MAX_SIZE = 30


class _Sentinel:
    """Typed sentinel to signal workers to stop."""
    pass


_DONE = _Sentinel()


class PipelineStats:
    """Thread-safe pipeline statistics."""

    def __init__(self) -> None:
        self.discovered = 0
        self.enriched = 0
        self.failed = 0
        self.skipped = 0
        self.queue_size = 0
        self.start_time = time.time()
        self._lock = asyncio.Lock()

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered,
            "enriched": self.enriched,
            "failed": self.failed,
            "skipped": self.skipped,
            "queue_size": self.queue_size,
            "elapsed_seconds": round(self.elapsed, 1),
        }


class PipelineManager:
    """Manages running pipeline jobs.

    Holds references to all active pipelines so the API can:
    - Check status of running jobs
    - Cancel running jobs
    """

    _instance: "PipelineManager | None" = None

    def __new__(cls) -> "PipelineManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._pipelines = {}
            cls._instance._tasks = {}
        return cls._instance

    @property
    def _pipelines(self) -> dict[UUID, PipelineStats]:
        return self.__dict__.setdefault("_pipelines_store", {})

    @_pipelines.setter
    def _pipelines(self, value: dict) -> None:
        self.__dict__["_pipelines_store"] = value

    @property
    def _tasks(self) -> dict[UUID, asyncio.Task]:
        return self.__dict__.setdefault("_tasks_store", {})

    @_tasks.setter
    def _tasks(self, value: dict) -> None:
        self.__dict__["_tasks_store"] = value

    @property
    def _queue(self) -> list[dict[str, Any]]:
        return self.__dict__.setdefault("_queue_store", [])

    @_queue.setter
    def _queue(self, value: list) -> None:
        self.__dict__["_queue_store"] = value

    async def start_job(
        self,
        job_id: UUID,
        keywords: list[str],
        areas: list[str],
        max_results: int,
        num_workers: int = 3,
    ) -> PipelineStats:
        """Start a pipeline job in the background, or queue it if another job is running.

        Returns the PipelineStats object for tracking progress.
        """
        if job_id in self._tasks:
            raise ValueError(f"Job {job_id} is already running")

        for q_job in self._queue:
            if q_job["job_id"] == job_id:
                return q_job["stats"]

        stats = PipelineStats()
        self._pipelines[job_id] = stats

        # If any job is currently running, queue this job instead of running it immediately
        if len(self._tasks) > 0:
            logger.info("job_queued", job_id=job_id, active_jobs=list(self._tasks.keys()))
            self._queue.append({
                "job_id": job_id,
                "keywords": keywords,
                "areas": areas,
                "max_results": max_results,
                "num_workers": num_workers,
                "stats": stats,
            })
            return stats

        # Start execution immediately
        self._start_execution(job_id, keywords, areas, max_results, num_workers, stats)
        return stats

    def _start_execution(
        self,
        job_id: UUID,
        keywords: list[str],
        areas: list[str],
        max_results: int,
        num_workers: int,
        stats: PipelineStats,
    ) -> None:
        """Helper to create and run the background task."""
        task = asyncio.create_task(
            self._run_pipeline(job_id, keywords, areas, max_results, num_workers, stats)
        )
        self._tasks[job_id] = task
        # Auto-cleanup when done
        task.add_done_callback(lambda _: self._cleanup(job_id))

    def get_stats(self, job_id: UUID) -> PipelineStats | None:
        """Get stats for a running or recently completed job."""
        return self._pipelines.get(job_id)

    def is_running(self, job_id: UUID) -> bool:
        """Check if a job is currently running."""
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    async def cancel_job(self, job_id: UUID) -> bool:
        """Cancel a running job or remove it from the queue."""
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            return True

        # Remove from queue if it hasn't started yet
        for i, q_job in enumerate(self._queue):
            if q_job["job_id"] == job_id:
                self._queue.pop(i)
                logger.info("job_removed_from_queue", job_id=job_id)
                return True

        return False

    async def cancel_all_jobs(self) -> None:
        """Cancel all active pipeline jobs."""
        for job_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        self._queue.clear()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def _cleanup(self, job_id: UUID) -> None:
        """Remove completed task and start next job in the queue if any."""
        self._tasks.pop(job_id, None)
        if hasattr(self, "seen_maps_urls"):
            self.seen_maps_urls.pop(job_id, None)
        if hasattr(self, "seen_names_phones"):
            self.seen_names_phones.pop(job_id, None)
        if hasattr(self, "seen_names_websites"):
            self.seen_names_websites.pop(job_id, None)
        if hasattr(self, "seen_names"):
            self.seen_names.pop(job_id, None)

        # Trigger next job in queue if any
        if self._queue:
            next_job = self._queue.pop(0)
            logger.info("starting_queued_job", job_id=next_job["job_id"])
            self._start_execution(
                job_id=next_job["job_id"],
                keywords=next_job["keywords"],
                areas=next_job["areas"],
                max_results=next_job["max_results"],
                num_workers=next_job["num_workers"],
                stats=next_job["stats"],
            )

    async def _run_pipeline(
        self,
        job_id: UUID,
        keywords: list[str],
        areas: list[str],
        max_results: int,
        num_workers: int,
        stats: PipelineStats,
    ) -> None:
        """Run the full discovery → enrichment pipeline."""
        queue: asyncio.Queue[dict[str, Any] | _Sentinel] = asyncio.Queue(
            maxsize=QUEUE_MAX_SIZE,
        )

        # Ensure browser is started
        browser = get_browser_manager()
        await browser.start()

        try:
            # Mark job as running
            async with AsyncSessionLocal() as db:
                job = await db.get(Job, job_id)
                if job:
                    job.status = JobStatus.RUNNING.value
                    await db.commit()

            # Initialize in-memory seen sets for current job
            self.seen_maps_urls = getattr(self, "seen_maps_urls", {})
            self.seen_names_phones = getattr(self, "seen_names_phones", {})
            self.seen_names_websites = getattr(self, "seen_names_websites", {})
            self.seen_names = getattr(self, "seen_names", {})

            self.seen_maps_urls[job_id] = set()
            self.seen_names_phones[job_id] = set()
            self.seen_names_websites[job_id] = set()
            self.seen_names[job_id] = set()

            # Pre-populate from existing DB records if this is a resume/retry
            unenriched_collections = []
            async with AsyncSessionLocal() as db:
                # Load collections
                query_col = select(Collection).where(Collection.job_id == job_id)
                res_col = await db.execute(query_col)
                existing_cols = res_col.scalars().all()
                
                # Load leads
                query_lead = select(Lead).where(Lead.job_id == job_id)
                res_lead = await db.execute(query_lead)
                existing_leads = res_lead.scalars().all()
                
                lead_collection_ids = {l.collection_id for l in existing_leads if l.collection_id}

                for col in existing_cols:
                    if col.google_maps_id:
                        self.seen_maps_urls[job_id].add(col.google_maps_id.split("?")[0].strip())
                    name = col.company_name.lower().strip()
                    phone = col.phone.strip() if col.phone else ""
                    website = col.website.lower().strip() if col.website else ""
                    
                    if phone:
                        self.seen_names_phones[job_id].add((name, phone))
                    if website:
                        self.seen_names_websites[job_id].add((name, website))
                    self.seen_names[job_id].add(name)

                    if col.id not in lead_collection_ids:
                        unenriched_collections.append(col)

                # Initialize stats with existing counts
                existing_enriched_count = 0
                existing_skipped_count = 0
                existing_failed_count = 0
                for lead in existing_leads:
                    if not lead.website:
                        existing_skipped_count += 1
                    elif lead.enrichment_data:
                        existing_enriched_count += 1
                    else:
                        existing_failed_count += 1

                stats.discovered = len(existing_cols)
                stats.enriched = existing_enriched_count
                stats.skipped = existing_skipped_count
                stats.failed = existing_failed_count

            # Start enrichment workers first
            worker_tasks = [
                asyncio.create_task(
                    self._enrichment_worker(i, job_id, queue, stats)
                )
                for i in range(num_workers)
            ]

            # Get job niche
            async with AsyncSessionLocal() as db:
                job_obj = await db.get(Job, job_id)
                job_niche = job_obj.niche if job_obj else None

            # Push unenriched existing collections to queue so they get enriched
            for col in unenriched_collections:
                biz = col.raw_data or {}
                biz["collection_id"] = str(col.id)
                biz["name"] = col.company_name
                biz["phone"] = col.phone
                biz["website"] = col.website
                biz["rating"] = col.rating
                biz["review_count"] = col.review_count
                biz["keyword"] = col.keyword
                biz["niche"] = job_niche
                await queue.put(biz)

            # Run discovery for all keyword/area combinations
            all_found = len(existing_cols)
            for keyword in keywords:
                for area in areas:
                    if all_found >= max_results:
                        break

                    found = await self._run_discovery(
                        job_id=job_id,
                        keyword=keyword,
                        area=area,
                        niche=job_niche,
                        max_results=max_results,
                        seen_urls=self.seen_maps_urls.get(job_id, set()),
                        queue=queue,
                        stats=stats,
                    )
                    all_found += found

            # Signal workers to stop
            for _ in range(num_workers):
                await queue.put(_DONE)

            # Wait for all enrichment to finish
            await asyncio.gather(*worker_tasks)

            # Mark job completed
            async with AsyncSessionLocal() as db:
                job = await db.get(Job, job_id)
                if job:
                    job.total_leads_found = stats.discovered
                    job.total_leads_enriched = stats.enriched + stats.skipped + stats.failed
                    job.status = JobStatus.COMPLETED.value
                    await db.commit()

            logger.info(
                "pipeline_completed",
                job_id=job_id,
                **stats.to_dict(),
            )

        except asyncio.CancelledError:
            async with AsyncSessionLocal() as db:
                job = await db.get(Job, job_id)
                if job:
                    job.total_leads_found = stats.discovered
                    job.total_leads_enriched = stats.enriched + stats.skipped + stats.failed
                    job.status = JobStatus.PAUSED.value
                    await db.commit()
            logger.info("pipeline_cancelled", job_id=job_id)

        except Exception as e:
            async with AsyncSessionLocal() as db:
                job = await db.get(Job, job_id)
                if job:
                    job.total_leads_found = stats.discovered
                    job.total_leads_enriched = stats.enriched + stats.skipped + stats.failed
                    job.status = JobStatus.FAILED.value
                    job.error_message = str(e)
                    await db.commit()
            logger.error("pipeline_failed", job_id=job_id, error=str(e))

    async def _run_discovery(
        self,
        job_id: UUID,
        keyword: str,
        area: str,
        niche: str | None,
        max_results: int,
        seen_urls: set[str],
        queue: asyncio.Queue,
        stats: PipelineStats,
    ) -> int:
        """Run discovery for a single keyword/area pair."""
        pending: list[Collection] = []
        db_lock = asyncio.Lock()
        new_added_count = 0

        async with AsyncSessionLocal() as db:
            async def on_lead_found(biz: dict[str, Any]) -> None:
                nonlocal new_added_count
                maps_url = biz.get("maps_url", "").split("?")[0].strip()
                name = biz["name"].lower().strip()
                # Detect country code for formatting & validation
                country_iso = detect_country_iso(biz.get("address"), area)

                phone = biz.get("phone", "").strip() if biz.get("phone") else ""
                phone_cleaned = clean_phone(phone, country_iso) if phone else ""
                # Keep the cleaned phone on the biz dictionary
                if phone_cleaned:
                    biz["phone"] = phone_cleaned

                website = biz.get("website", "").lower().strip() if biz.get("website") else ""

                # Deduplication Check
                if maps_url and maps_url in self.seen_maps_urls.get(job_id, set()):
                    logger.info("dedupe_skip_maps_url", job_id=job_id, maps_url=maps_url)
                    return
                if phone_cleaned and (name, phone_cleaned) in self.seen_names_phones.get(job_id, set()):
                    logger.info("dedupe_skip_name_phone", job_id=job_id, name=name, phone=phone_cleaned)
                    return
                if website and (name, website) in self.seen_names_websites.get(job_id, set()):
                    logger.info("dedupe_skip_name_website", job_id=job_id, name=name, website=website)
                    return
                if not phone_cleaned and not website and name in self.seen_names.get(job_id, set()):
                    logger.info("dedupe_skip_name_only", job_id=job_id, name=name)
                    return

                # Mark as seen
                if maps_url:
                    self.seen_maps_urls[job_id].add(maps_url)
                if phone_cleaned:
                    self.seen_names_phones[job_id].add((name, phone_cleaned))
                if website:
                    self.seen_names_websites[job_id].add((name, website))
                self.seen_names[job_id].add(name)

                new_added_count += 1
                stats.discovered += 1
                stats.queue_size = queue.qsize()

                col_id = uuid4()
                biz["collection_id"] = str(col_id)
                biz["area"] = area
                biz["keyword"] = keyword
                biz["niche"] = niche

                async with db_lock:
                    pending.append(Collection(
                        id=col_id,
                        job_id=job_id,
                        google_maps_id=biz.get("maps_url", "")[:255],
                        company_name=biz["name"],
                        address=biz.get("address"),
                        phone=biz.get("phone"),
                        website=biz.get("website"),
                        rating=biz.get("rating"),
                        review_count=biz.get("review_count"),
                        keyword=keyword,
                        area=area,
                        raw_data=biz,
                    ))

                    # Batch commit
                    if len(pending) >= COMMIT_BATCH_SIZE:
                        db.add_all(pending.copy())
                        pending.clear()
                        await db.commit()

                # Push to enrichment (backpressure-aware)
                await queue.put(biz)

            discovery = DiscoveryService(db)
            await discovery.discover_businesses(
                keyword=keyword,
                area=area,
                max_results=max_results,
                on_lead_found=on_lead_found,
                seen_urls=seen_urls,
            )

            # Flush remaining
            async with db_lock:
                if pending:
                    db.add_all(pending)
                    await db.commit()

        return new_added_count

    async def _enrichment_worker(
        self,
        worker_id: int,
        job_id: UUID,
        queue: asyncio.Queue[dict[str, Any] | _Sentinel],
        stats: PipelineStats,
    ) -> None:
        """Enrichment worker — own DB session, batched commits."""
        pending: list[Lead] = []

        async with AsyncSessionLocal() as db:
            enrichment = EnrichmentService(db)

            while True:
                item = await queue.get()
                stats.queue_size = queue.qsize()

                if isinstance(item, _Sentinel):
                    queue.task_done()
                    break

                biz = item
                website = biz.get("website", "")
                result = None

                # Detect country code for formatting & validation
                country_iso = detect_country_iso(biz.get("address"), biz.get("area"))

                if website:
                    try:
                        result = await asyncio.wait_for(
                            enrichment.enrich_website(
                                website=website,
                                name_hint=biz["name"],
                                default_region=country_iso,
                                search_keyword=biz.get("keyword", ""),
                            ),
                            timeout=45.0
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "enrichment_timeout",
                            worker=worker_id,
                            name=biz["name"],
                            website=website,
                        )
                    except Exception as e:
                        logger.warning(
                            "enrichment_error",
                            worker=worker_id,
                            name=biz["name"],
                            error=str(e),
                        )

                if not website:
                    stats.skipped += 1
                elif result:
                    stats.enriched += 1
                else:
                    stats.failed += 1

                # Clean the initial Google Maps phone using country context
                fallback_phone = []
                if biz.get("phone"):
                    cleaned_fallback = clean_phone(biz["phone"], country_iso)
                    if cleaned_fallback:
                        fallback_phone = [cleaned_fallback]

                pending.append(Lead(
                    id=uuid4(),
                    job_id=job_id,
                    collection_id=UUID(biz["collection_id"]) if biz.get("collection_id") else None,
                    company_name=biz["name"],
                    niche=biz.get("niche") or biz.get("keyword"),
                    website=result.get("website", website) if result else website,
                    address=biz.get("address"),
                    emails=result.get("emails", []) if result else [],
                    phones=result.get("phones", []) if result else fallback_phone,
                    whatsapp_numbers=result.get("whatsapp_numbers", []) if result else [],
                    social_links=result.get("social_links", []) if result else [],
                    pages_crawled=result.get("pages_crawled", 0) if result else 0,
                    email_count=len(result.get("emails", [])) if result else 0,
                    phone_count=len(result.get("phones", [])) if result else 0,
                    enrichment_data=result,
                ))

                # Batch commit
                if len(pending) >= COMMIT_BATCH_SIZE:
                    db.add_all(pending.copy())
                    pending.clear()
                    await db.commit()

                queue.task_done()

            # Flush remaining
            if pending:
                db.add_all(pending)
                await db.commit()


def get_pipeline_manager() -> PipelineManager:
    """Get the singleton PipelineManager."""
    return PipelineManager()
