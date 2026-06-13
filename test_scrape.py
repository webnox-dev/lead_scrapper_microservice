"""Test script — scrape leads from Google Maps and save to DB.

Pipeline architecture (both stages run in parallel):
  ┌─────────────┐    asyncio.Queue    ┌──────────────────┐
  │  DISCOVERY   │ ──── push ───────> │  ENRICHMENT (x3)  │
  │  (Stage 1)   │                    │  (Stage 2)         │
  │  Google Maps │                    │  Website scraping   │
  │  own session │                    │  own session each   │
  └─────────────┘                    └──────────────────┘

Key design for scale:
  - Each worker gets its own DB session (no shared session concurrency bugs)
  - Queue has max size for backpressure (discovery pauses when enrichment is full)
  - Batched DB commits (every N leads, not every single one)
  - Atomic counters for progress tracking

Usage:
    python test_scrape.py
"""

import asyncio
import sys
import time
from typing import Any
from uuid import UUID, uuid4

from core.browser_manager import get_browser_manager
from core.config import settings
from core.logging import configure_logging, get_logger
from db.session import AsyncSessionLocal, engine, init_db
from db.models.job import Job, JobStatus
from db.models.collection import Collection
from db.models.lead import Lead
from services.discovery import DiscoveryService
from services.enrichment import EnrichmentService

logger = get_logger(__name__)

# ── Configuration ──────────────────────────────────────────────
KEYWORD = "restaurants"
AREA = "Coimbatore"
MAX_LEADS = 5
ENRICHMENT_WORKERS = 3     # concurrent enrichment workers
QUEUE_MAX_SIZE = 20        # backpressure: discovery pauses when queue is full
COMMIT_BATCH_SIZE = 10     # commit to DB every N leads (for 100s+ scale)
# ───────────────────────────────────────────────────────────────


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
        self._lock = asyncio.Lock()
        self.results: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    async def record(
        self,
        biz: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> None:
        async with self._lock:
            self.results.append((biz, result))
            if result is not None:
                self.enriched += 1
            else:
                self.failed += 1


async def run_discovery(
    job_id: UUID,
    queue: asyncio.Queue[dict[str, Any] | _Sentinel],
    stats: PipelineStats,
    num_workers: int,
) -> list[dict[str, Any]]:
    """Stage 1 (Producer): Discover businesses and push to queue.

    Uses its own DB session — safe for concurrent access.
    """
    print(f"\n{'─' * 60}")
    print("🔍 STAGE 1 — Discovery started")
    print(f"{'─' * 60}")

    all_businesses: list[dict[str, Any]] = []
    pending_collections: list[Collection] = []

    async with AsyncSessionLocal() as db:
        async def on_lead_found(biz: dict[str, Any]) -> None:
            """Called for each business as soon as it's extracted."""
            all_businesses.append(biz)
            stats.discovered += 1

            col_id = uuid4()
            biz["collection_id"] = str(col_id)

            # Buffer collection for batched commit
            pending_collections.append(Collection(
                id=col_id,
                job_id=job_id,
                google_maps_id=biz.get("maps_url", "")[:255],
                company_name=biz["name"],
                phone=biz.get("phone"),
                website=biz.get("website"),
                rating=biz.get("rating"),
                review_count=biz.get("review_count"),
                keyword=KEYWORD,
                area=AREA,
                raw_data=biz,
            ))

            # Batch commit collections
            if len(pending_collections) >= COMMIT_BATCH_SIZE:
                db.add_all(pending_collections.copy())
                pending_collections.clear()
                await db.commit()

            # Push to enrichment queue (blocks if queue is full = backpressure)
            await queue.put(biz)

        discovery = DiscoveryService(db)
        results = await discovery.discover_businesses(
            keyword=KEYWORD,
            area=AREA,
            max_results=MAX_LEADS,
            on_lead_found=on_lead_found,
        )

        # Flush remaining collections
        if pending_collections:
            db.add_all(pending_collections)
            await db.commit()

        # Update job with discovery count
        job = await db.get(Job, job_id)
        if job:
            job.total_leads_found = len(results)
            await db.commit()

    # Signal all enrichment workers to stop
    for _ in range(num_workers):
        await queue.put(_DONE)

    print(f"\n✅ Discovery complete: {len(results)} businesses found")
    return results


async def run_enrichment_worker(
    worker_id: int,
    job_id: UUID,
    queue: asyncio.Queue[dict[str, Any] | _Sentinel],
    stats: PipelineStats,
) -> None:
    """Stage 2 (Consumer): Pull from queue and enrich each business.

    Each worker gets its own DB session — safe for concurrent use.
    """
    pending_leads: list[Lead] = []

    async with AsyncSessionLocal() as db:
        enrichment = EnrichmentService(db)

        while True:
            item = await queue.get()

            # Check for stop sentinel
            if isinstance(item, _Sentinel):
                queue.task_done()
                break

            biz = item
            website = biz.get("website", "")
            result = None

            if website:
                try:
                    result = await enrichment.enrich_website(
                        website=website,
                        name_hint=biz["name"],
                    )
                except Exception as e:
                    logger.warning(
                        "enrichment_worker_error",
                        worker=worker_id,
                        name=biz["name"],
                        error=str(e),
                    )
            else:
                print(f"   ⏭️  W{worker_id}: {biz['name']} — no website, skipping")
                stats.skipped += 1

            # Buffer lead for batched commit
            pending_leads.append(Lead(
                id=uuid4(),
                job_id=job_id,
                collection_id=UUID(biz["collection_id"]) if biz.get("collection_id") else None,
                company_name=biz["name"],
                website=website,
                emails=result.get("emails", []) if result else [],
                phones=result.get("phones", []) if result else (
                    [biz["phone"]] if biz.get("phone") else []
                ),
                whatsapp_numbers=result.get("whatsapp_numbers", []) if result else [],
                social_links=result.get("social_links", []) if result else [],
                pages_crawled=result.get("pages_crawled", 0) if result else 0,
                email_count=len(result.get("emails", [])) if result else 0,
                phone_count=len(result.get("phones", [])) if result else 0,
                enrichment_data=result,
            ))

            # Batch commit leads
            if len(pending_leads) >= COMMIT_BATCH_SIZE:
                db.add_all(pending_leads.copy())
                pending_leads.clear()
                await db.commit()

            await stats.record(biz, result)
            queue.task_done()

        # Flush remaining leads
        if pending_leads:
            db.add_all(pending_leads)
            await db.commit()


async def main() -> None:
    configure_logging(debug=True)
    browser = get_browser_manager()
    start_time = time.time()

    print("=" * 60)
    print("🚀 Scrapper v2 — Pipeline Test Run")
    print(f"   Keyword : {KEYWORD}")
    print(f"   Area    : {AREA}")
    print(f"   Max     : {MAX_LEADS} leads")
    print(f"   Workers : {ENRICHMENT_WORKERS} enrichment workers")
    print(f"   Queue   : max {QUEUE_MAX_SIZE} (backpressure)")
    print(f"   Batch   : commit every {COMMIT_BATCH_SIZE} leads")
    print(f"   DB      : {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}")
    print("=" * 60)

    # 1. Init DB tables
    print("\n📦 Initializing database...")
    await init_db()
    print("   ✅ Tables ready")

    # 2. Start browser
    print("\n🌐 Starting browser...")
    await browser.start()
    print("   ✅ Browser ready")

    # Pipeline infrastructure
    queue: asyncio.Queue[dict[str, Any] | _Sentinel] = asyncio.Queue(
        maxsize=QUEUE_MAX_SIZE,
    )
    stats = PipelineStats()

    try:
        # 3. Create job record
        async with AsyncSessionLocal() as db:
            job = Job(
                id=uuid4(),
                name=f"Test: {KEYWORD} in {AREA}",
                status=JobStatus.RUNNING.value,
                keywords=[KEYWORD],
                areas=[AREA],
                max_results=MAX_LEADS,
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)
            job_id = job.id
            print(f"\n📋 Job created: {job_id}")

        # ── Run pipeline ───────────────────────────────────
        print(f"\n{'─' * 60}")
        print("⚡ PIPELINE — Discovery + Enrichment running in parallel")
        print(f"{'─' * 60}")

        # Start enrichment workers FIRST so they're ready to consume
        worker_tasks = [
            asyncio.create_task(
                run_enrichment_worker(i, job_id, queue, stats)
            )
            for i in range(ENRICHMENT_WORKERS)
        ]

        # Run discovery (producer) — this feeds the queue
        discovery_task = asyncio.create_task(
            run_discovery(job_id, queue, stats, ENRICHMENT_WORKERS)
        )

        # Wait for both to complete
        businesses = await discovery_task
        await asyncio.gather(*worker_tasks)

        # ── Finalize job ───────────────────────────────────
        async with AsyncSessionLocal() as db:
            job = await db.get(Job, job_id)
            if job:
                job.total_leads_enriched = stats.enriched
                job.status = JobStatus.COMPLETED.value
                await db.commit()

        # ── Summary ────────────────────────────────────────
        elapsed = time.time() - start_time

        print(f"\n{'=' * 60}")
        print("📊 RESULTS SUMMARY")
        print(f"{'=' * 60}")
        print(f"   Job ID          : {job_id}")
        print(f"   Businesses found: {len(businesses)}")
        print(f"   Enriched        : {stats.enriched}")
        print(f"   Failed          : {stats.failed}")
        print(f"   Skipped (no URL): {stats.skipped}")
        print(f"   Time            : {elapsed:.1f}s")
        print()

        for i, (biz, result) in enumerate(stats.results, 1):
            print(f"   {i}. {biz['name']}")
            if biz.get("phone"):
                print(f"      📞 {biz['phone']}")
            if biz.get("website"):
                print(f"      🌐 {biz['website']}")
            if result:
                emails = result.get("emails", [])
                phones = result.get("phones", [])
                socials = result.get("social_links", [])
                print(f"      📧 {len(emails)} emails, 📱 {len(phones)} phones, 🔗 {len(socials)} socials")
            print()

        print(f"{'=' * 60}")
        print(f"✅ Done in {elapsed:.1f}s! Check 'collections' and 'leads' tables.")
        print(f"{'=' * 60}")

    finally:
        print("\n🛑 Stopping browser...")
        await browser.stop()
        await engine.dispose()
        print("   ✅ Cleanup complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        sys.exit(1)
