"""Job API routes for Leads Platform v2."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import desc, func, select

from api.dependencies import DbSession
from core.logging import get_logger
from db.models.job import Job, JobStatus
from db.models.collection import Collection
from db.models.lead import Lead
from db.session import AsyncSessionLocal
from schemas import JobCreate, JobResponse
from services.pipeline import get_pipeline_manager

logger = get_logger(__name__)
router = APIRouter()


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_job(
    job_data: JobCreate,
    db: DbSession,
) -> dict:
    """Create a new job and trigger execution in background."""
    job_id = uuid4()
    job = Job(
        id=job_id,
        name=job_data.name,
        niche=job_data.niche,
        status=JobStatus.PENDING.value,
        keywords=job_data.keywords,
        areas=job_data.areas,
        max_results=job_data.max_results,
        concurrency=job_data.concurrency,
        total_keywords=len(job_data.keywords) * len(job_data.areas),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Start the job in background using PipelineManager
    pm = get_pipeline_manager()
    await pm.start_job(
        job_id=job.id,
        keywords=job.keywords,
        areas=job.areas,
        max_results=job.max_results,
        num_workers=job.concurrency,
    )

    logger.info("job_created_and_started", job_id=job.id, name=job.name)

    return {
        "id": job.id,
        "name": job.name,
        "niche": job.niche,
        "status": job.status,
        "keywords": job.keywords,
        "areas": job.areas,
        "keyword": job.keywords[0] if job.keywords else "",
        "location": job.areas[0] if job.areas else "",
        "total_leads": job.max_results,
        "scraped_leads": job.total_leads_enriched,
        "max_results": job.max_results,
        "concurrency": job.concurrency,
        "priority": job.priority,
        "total_keywords": job.total_keywords,
        "processed_keywords": job.processed_keywords,
        "total_leads_found": job.total_leads_found,
        "total_leads_enriched": job.total_leads_enriched,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_message": job.error_message,
    }


@router.get("/niches", response_model=list)
async def list_niches(db: DbSession) -> list:
    """List all unique niches (keywords) with stats."""
    result = await db.execute(select(Job))
    jobs = result.scalars().all()
    
    niche_stats = {}
    for job in jobs:
        # Determine niche name (use direct niche column, fallback to keywords)
        niche_names = []
        if job.niche:
            niche_names = [job.niche]
        elif job.keywords:
            niche_names = job.keywords
            
        for kw in niche_names:
            kw_clean = kw.strip().lower() if kw else ""
            if not kw_clean:
                continue
            if kw_clean not in niche_stats:
                niche_stats[kw_clean] = {
                    "name": kw.strip(),
                    "job_count": 0,
                    "lead_count": 0,
                }
            niche_stats[kw_clean]["job_count"] += 1
            niche_stats[kw_clean]["lead_count"] += job.total_leads_enriched
            
    return sorted(list(niche_stats.values()), key=lambda x: x["name"].lower())


@router.delete("/niches/{niche_name}", response_model=dict)
async def delete_niche(niche_name: str, db: DbSession) -> dict:
    """Delete all jobs and their leads for a given niche topic."""
    from sqlalchemy import Text
    from db.models.lead import Lead

    # Find all jobs belonging to this niche
    result = await db.execute(
        select(Job).where(
            (func.lower(Job.niche) == niche_name.lower()) |
            ((Job.niche == None) & func.lower(Job.keywords.cast(Text)).like(f'%"{niche_name.lower()}"%'))
        )
    )
    jobs = result.scalars().all()
    job_ids = [job.id for job in jobs]

    deleted_leads = 0
    deleted_jobs = len(job_ids)

    if job_ids:
        # Delete all leads for these jobs
        lead_result = await db.execute(
            select(Lead).where(Lead.job_id.in_(job_ids))
        )
        leads = lead_result.scalars().all()
        deleted_leads = len(leads)
        for lead in leads:
            await db.delete(lead)

        # Delete all collections for these jobs
        from db.models.collection import Collection
        col_result = await db.execute(
            select(Collection).where(Collection.job_id.in_(job_ids))
        )
        cols = col_result.scalars().all()
        for col in cols:
            await db.delete(col)

        # Delete the jobs themselves
        for job in jobs:
            await db.delete(job)

        await db.commit()

    logger.info("niche_deleted", niche=niche_name, jobs=deleted_jobs, leads=deleted_leads)
    return {"deleted_jobs": deleted_jobs, "deleted_leads": deleted_leads}


@router.put("/niches/{niche_name}", response_model=dict)
async def rename_niche(niche_name: str, new_name: str = Query(..., min_length=1), db: DbSession = None) -> dict:
    """Rename a niche topic in all jobs and leads."""
    from db.models.lead import Lead

    # 1. Update jobs
    result = await db.execute(
        select(Job).where(func.lower(Job.niche) == niche_name.lower())
    )
    jobs = result.scalars().all()
    for job in jobs:
        job.niche = new_name

    # 2. Update leads
    lead_result = await db.execute(
        select(Lead).where(func.lower(Lead.niche) == niche_name.lower())
    )
    leads = lead_result.scalars().all()
    for lead in leads:
        lead.niche = new_name

    await db.commit()
    logger.info("niche_renamed", old_name=niche_name, new_name=new_name, jobs=len(jobs), leads=len(leads))
    return {"jobs_updated": len(jobs), "leads_updated": len(leads)}


@router.get("", response_model=dict)
async def list_jobs(
    db: DbSession,
    status_filter: str | None = Query(None, alias="status"),
    niche: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List jobs with filtering and pagination."""
    query = select(Job)
    count_query = select(func.count(Job.id))

    if status_filter:
        query = query.where(Job.status == status_filter)
        count_query = count_query.where(Job.status == status_filter)

    if niche:
        from sqlalchemy import Text
        query = query.where(
            (func.lower(Job.niche) == niche.lower()) |
            ((Job.niche == None) & func.lower(Job.keywords.cast(Text)).like(f'%"{niche.lower()}"%'))
        )
        count_query = count_query.where(
            (func.lower(Job.niche) == niche.lower()) |
            ((Job.niche == None) & func.lower(Job.keywords.cast(Text)).like(f'%"{niche.lower()}"%'))
        )

    query = query.order_by(desc(Job.created_at)).offset(offset).limit(limit)

    result = await db.execute(query)
    jobs = result.scalars().all()

    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    pm = get_pipeline_manager()
    jobs_data = []
    for job in jobs:
        stats = pm.get_stats(job.id)
        total_found = stats.discovered if stats else job.total_leads_found
        total_enriched = (stats.enriched + stats.skipped + stats.failed) if stats else job.total_leads_enriched
        jobs_data.append({
            "id": job.id,
            "name": job.name,
            "niche": job.niche,
            "status": job.status,
            "keywords": job.keywords,
            "areas": job.areas,
            "keyword": job.keywords[0] if job.keywords else "",
            "location": job.areas[0] if job.areas else "",
            "total_leads": job.max_results,
            "scraped_leads": total_enriched,
            "max_results": job.max_results,
            "concurrency": job.concurrency,
            "priority": job.priority,
            "total_keywords": job.total_keywords,
            "processed_keywords": job.processed_keywords,
            "total_leads_found": total_found,
            "total_leads_enriched": total_enriched,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "error_message": job.error_message,
        })

    return {
        "items": jobs_data,
        "total": total,
        "page": offset // limit + 1 if limit > 0 else 1,
        "page_size": limit,
    }


@router.get("/system/stats", response_model=dict)
async def get_system_stats() -> dict:
    """Get scraper system stats (browser slots, concurrency, etc.)."""
    from core.browser_manager import get_browser_manager
    from core.config import settings
    pm = get_pipeline_manager()
    browser = get_browser_manager()

    running_job_ids = [str(jid) for jid in pm._tasks.keys()]

    return {
        "jobs": {
            "queued_jobs": len(pm._queue),
            "running_jobs": len(pm._tasks),
            "worker_concurrency": settings.browser_concurrency,
            "running_job_ids": running_job_ids,
            "paused_jobs": [],
        },
        "browser": {
            "initialized": browser._browser is not None,
            "connected": browser._browser is not None,
            "pool_max": settings.browser_max_pages,
            "available_slots": settings.browser_concurrency - len(browser._page_pool),
            "in_use_contexts": len(browser._contexts),
        }
    }


@router.get("/{job_id}", response_model=dict)
async def get_job(
    job_id: UUID,
    db: DbSession,
) -> dict:
    """Get job by ID."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    pm = get_pipeline_manager()
    stats = pm.get_stats(job_id)
    total_found = stats.discovered if stats else job.total_leads_found
    total_enriched = (stats.enriched + stats.skipped + stats.failed) if stats else job.total_leads_enriched

    return {
        "id": job.id,
        "name": job.name,
        "status": job.status,
        "keywords": job.keywords,
        "areas": job.areas,
        "keyword": job.keywords[0] if job.keywords else "",
        "location": job.areas[0] if job.areas else "",
        "total_leads": job.max_results,
        "scraped_leads": total_enriched,
        "max_results": job.max_results,
        "concurrency": job.concurrency,
        "priority": job.priority,
        "total_keywords": job.total_keywords,
        "processed_keywords": job.processed_keywords,
        "total_leads_found": total_found,
        "total_leads_enriched": total_enriched,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_message": job.error_message,
    }


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(
    job_id: UUID,
    db: DbSession,
) -> None:
    """Delete a job and all its results (cascade)."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    # Cancel if running
    pm = get_pipeline_manager()
    await pm.cancel_job(job_id)

    # Delete from DB
    await db.delete(job)
    await db.commit()


@router.post("/{job_id}/pause", response_model=dict)
async def pause_job(
    job_id: UUID,
    db: DbSession,
) -> dict:
    """Pause (cancel background task) a running job."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    pm = get_pipeline_manager()
    cancelled = await pm.cancel_job(job_id)

    job.status = JobStatus.PAUSED.value
    await db.commit()
    await db.refresh(job)

    return {
        "id": job.id,
        "status": job.status,
        "message": "Job paused successfully" if cancelled else "Job status updated to paused",
    }


@router.post("/{job_id}/resume", response_model=dict)
async def resume_job(
    job_id: UUID,
    db: DbSession,
) -> dict:
    """Resume a paused or failed job."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    pm = get_pipeline_manager()
    if pm.is_running(job_id):
        return {
            "id": job.id,
            "status": job.status,
            "message": "Job is already running",
        }

    # Restart background task
    await pm.start_job(
        job_id=job.id,
        keywords=job.keywords,
        areas=job.areas,
        max_results=job.max_results,
        num_workers=job.concurrency,
    )

    job.status = JobStatus.PENDING.value
    await db.commit()
    await db.refresh(job)

    return {
        "id": job.id,
        "status": job.status,
        "message": "Job resumed successfully",
    }


@router.post("/{job_id}/retry", response_model=dict)
async def retry_job(
    job_id: UUID,
    db: DbSession,
) -> dict:
    """Retry a failed job."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    pm = get_pipeline_manager()
    if pm.is_running(job_id):
        return {
            "id": job.id,
            "status": job.status,
            "message": "Job is already running",
        }

    job.error_message = None
    job.status = JobStatus.PENDING.value
    await db.commit()

    # Restart background task
    await pm.start_job(
        job_id=job.id,
        keywords=job.keywords,
        areas=job.areas,
        max_results=job.max_results,
        num_workers=job.concurrency,
    )

    return {
        "id": job.id,
        "status": job.status,
        "message": "Job retry started successfully",
    }


from sqlalchemy.orm import selectinload


@router.get("/{job_id}/results/enriched", response_model=list)
async def get_job_enriched_results(
    job_id: UUID,
    db: DbSession,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list:
    """Get enriched leads for a job."""
    query = select(Lead).where(Lead.job_id == job_id).options(selectinload(Lead.collection)).offset(offset).limit(limit)
    result = await db.execute(query)
    leads = result.scalars().all()

    # Find global duplicates (leads that exist in other jobs)
    other_websites = set()
    other_names = set()
    websites = [l.website for l in leads if l.website]
    company_names = [l.company_name for l in leads]

    if websites or company_names:
        if websites:
            web_q = select(Lead.website).where(Lead.job_id != job_id, Lead.website.in_(websites))
            web_res = await db.execute(web_q)
            other_websites = {w for w in web_res.scalars().all() if w}
        if company_names:
            name_q = select(Lead.company_name).where(Lead.job_id != job_id, Lead.company_name.in_(company_names))
            name_res = await db.execute(name_q)
            other_names = {n for n in name_res.scalars().all() if n}

    return [
        {
            "id": lead.id,
            "job_id": lead.job_id,
            "company_name": lead.company_name,
            "website": lead.website,
            "address": lead.address,
            "emails": lead.emails,
            "phones": lead.phones,
            "whatsapp_numbers": lead.whatsapp_numbers,
            "social_links": lead.social_links,
            "pages_crawled": lead.pages_crawled,
            "email_count": lead.email_count,
            "phone_count": lead.phone_count,
            "enrichment_data": lead.enrichment_data,
            "rating": lead.collection.rating if lead.collection else None,
            "review_count": lead.collection.review_count if lead.collection else None,
            "is_duplicate": (
                (lead.website in other_websites) if lead.website
                else (lead.company_name in other_names)
            ),
        }
        for lead in leads
    ]


@router.get("/{job_id}/results/raw", response_model=list)
async def get_job_raw_results(
    job_id: UUID,
    db: DbSession,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list:
    """Get raw collections for a job."""
    query = select(Collection).where(Collection.job_id == job_id).offset(offset).limit(limit)
    result = await db.execute(query)
    collections = result.scalars().all()

    return [
        {
            "id": col.id,
            "job_id": col.job_id,
            "google_maps_id": col.google_maps_id,
            "company_name": col.company_name,
            "address": col.address,
            "phone": col.phone,
            "website": col.website,
            "rating": col.rating,
            "review_count": col.review_count,
            "latitude": col.latitude,
            "longitude": col.longitude,
            "keyword": col.keyword,
            "area": col.area,
        }
        for col in collections
    ]


@router.get("/{job_id}/queue-stats", response_model=dict)
async def get_job_queue_stats(
    job_id: UUID,
    db: DbSession,
) -> dict:
    """Get real-time queue statistics for a job."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    pm = get_pipeline_manager()
    stats = pm.get_stats(job_id)

    if stats:
        queue_size = stats.queue_size
        failed = stats.failed
        discovered = stats.discovered
        enriched = stats.enriched + stats.skipped + stats.failed
    else:
        queue_size = 0
        failed = 0
        discovered = job.total_leads_found
        enriched = job.total_leads_enriched

    return {
        "main": queue_size,
        "retry": 0,
        "processing": queue_size,
        "failed": failed,
        "total_pending": queue_size,
        "job_status": job.status,
        "total_leads_found": discovered,
        "total_leads_enriched": enriched,
    }


# WebSocket Router (registered directly in main.py)
async def job_websocket(websocket: WebSocket, job_id: UUID):
    """WebSocket endpoint for job updates."""
    await websocket.accept()
    logger.info("websocket_connected", job_id=job_id)

    pm = get_pipeline_manager()

    try:
        # Loop and send updates
        while True:
            # Check DB
            async with AsyncSessionLocal() as db:
                job = await db.get(Job, job_id)

            if not job:
                await websocket.send_json({
                    "type": "error",
                    "message": "Job not found",
                })
                break

            stats = pm.get_stats(job_id)

            status_val = job.status
            total_found = stats.discovered if stats else job.total_leads_found
            total_enriched = (stats.enriched + stats.skipped + stats.failed) if stats else job.total_leads_enriched
            
            # Simple progress calculation
            progress_pct = 0.0
            if job.total_keywords > 0:
                progress_pct = (job.processed_keywords / job.total_keywords) * 100
            elif stats and stats.discovered > 0:
                progress_pct = (total_enriched / stats.discovered) * 100

            await websocket.send_json({
                "type": "progress",
                "payload": {
                    "job_id": str(job.id),
                    "status": status_val,
                    "total_leads_found": total_found,
                    "total_leads_enriched": total_enriched,
                    "progress_percent": progress_pct,
                    "message": f"Processing leads... Status: {status_val}",
                    "queue_depth": stats.queue_size if stats else 0,
                }
            })

            # Check if job is finished
            if status_val in (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.PAUSED.value):
                # Send one final completion message
                await websocket.send_json({
                    "type": "completed",
                    "payload": {
                        "job_id": str(job.id),
                        "status": status_val,
                    }
                })
                break

            await asyncio.sleep(2.0)

    except WebSocketDisconnect:
        logger.info("websocket_disconnected", job_id=job_id)
    except Exception as e:
        logger.error("websocket_error", job_id=job_id, error=str(e))
