import asyncio
import sys
import traceback
import signal

# Proactor event loop is default on Windows since Python 3.8, no need to set it manually

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

def dump_stacks(sig, frame):
    sys.stderr.write("=== DUMPING STACKS FOR ALL THREADS ===\n")
    for thread_id, stack in sys._current_frames().items():
        sys.stderr.write(f"\nThread {thread_id}:\n")
        traceback.print_stack(stack, file=sys.stderr)
    sys.stderr.write("=== END OF STACKS DUMP ===\n")
    sys.stderr.flush()

try:
    signal.signal(signal.SIGUSR1, dump_stacks)
except Exception:
    pass

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api import router as api_router
from api.dependencies import VerifyAPIKey
from api.routes.jobs import job_websocket
from core.browser_manager import get_browser_manager
from core.config import settings
from core.logging import configure_logging, get_logger
from db.session import engine, init_db

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    logger.info(f"🚀 Starting {settings.app_name} v{settings.app_version}...")
    configure_logging(debug=settings.debug)
    
    # Initialize DB tables
    await init_db()
    logger.info("✅ Database initialized")
    
    # Startup recovery: find running/pending jobs and mark them failed/paused
    from db.session import AsyncSessionLocal
    from db.models.job import Job, JobStatus
    from sqlalchemy import update
    import datetime
    
    async with AsyncSessionLocal() as session:
        try:
            stmt = (
                update(Job)
                .where(Job.status.in_([JobStatus.RUNNING.value, JobStatus.PENDING.value]))
                .values(
                    status=JobStatus.FAILED.value,
                    failed_at=datetime.datetime.now(datetime.timezone.utc),
                    error_message="Job was interrupted by server restart.",
                )
            )
            res = await session.execute(stmt)
            await session.commit()
            if res.rowcount > 0:
                logger.info(f"🔄 Recovered {res.rowcount} zombie jobs interrupted by server restart")
        except Exception as e:
            logger.error("Failed to run startup job recovery", error=str(e))
    
    # Pre-start browser manager to warm context/page pool
    browser = get_browser_manager()
    await browser.start()
    logger.info("✅ Browser manager started")
    
    yield
    
    # Shutdown
    logger.info("🛑 Shutting down app...")
    
    # Cancel all running pipeline jobs first
    try:
        from services.pipeline import get_pipeline_manager
        pm = get_pipeline_manager()
        await pm.cancel_all_jobs()
        logger.info("✅ All running pipeline jobs cancelled")
    except Exception as e:
        logger.warning("Error cancelling pipeline jobs during shutdown", error=str(e))
    
    # Close browser manager
    browser = get_browser_manager()
    try:
        await asyncio.wait_for(browser.stop(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("Browser manager shutdown timed out, forcing exit")
    except Exception as e:
        logger.warning("Error during browser manager shutdown", error=str(e))
    logger.info("✅ Browser manager stopped")
    
    # Dispose SQLAlchemy database engine connections
    await engine.dispose()
    logger.info("✅ Database connections closed")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Production-grade parallel Google Maps & website scraper microservice",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

# Compression Middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)

# CORS Middleware (Enable client requests from other origins/ports)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex="https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health Check Route (Bypasses API Key Auth)
@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


# Serve static documentation and GUI app directly (Bypasses API Key Auth)
@app.get("/", response_class=FileResponse)
async def get_index() -> str:
    """Serve the GUI application dashboard."""
    return "static/index.html"


@app.get("/docs.html", response_class=FileResponse)
async def get_docs() -> str:
    """Serve the API reference documentation."""
    return "static/docs.html"


# Mount static directory for general asset access
app.mount("/static", StaticFiles(directory="static"), name="static")


# Register WebSocket endpoints directly (Bypasses API Key Auth)
app.add_api_websocket_route("/ws/jobs/{job_id}", job_websocket)
app.add_api_websocket_route("/api/v1/ws/jobs/{job_id}", job_websocket)

# Include core API routes under the API Key security dependency
app.include_router(api_router, prefix="/api/v1", dependencies=[VerifyAPIKey])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_config=None,  # Use custom structlog logging configuration
    )
