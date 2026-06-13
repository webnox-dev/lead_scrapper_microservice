"""API router registration."""

from fastapi import APIRouter

from api.routes.jobs import router as jobs_router
from api.routes.leads import router as leads_router
from api.routes.services import router as services_router

router = APIRouter()
router.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
router.include_router(leads_router, prefix="/leads", tags=["leads"])
router.include_router(services_router, tags=["services"])
