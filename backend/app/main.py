import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.db.engine import init_db
from app.api.health import router as health_router
from app.api.auth import router as auth_router
from app.api.admin import router as admin_router
from app.api.chat import router as chat_router

# Configure logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle hook managing app startup and shutdown events."""
    logger.info("Initializing database schemas on startup...")
    try:
        await init_db()
        logger.info("Database schemas initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to auto-create database tables on boot: {e}")
        # Non-fatal error during startup; let execution continue so tests/devs can debug
    
    yield
    logger.info("Shutting down MigrationBot API backend server.")


# Instantiate FastAPI app
app = FastAPI(
    title="MigrationBot Enterprise Portal API",
    description="Asynchronous event-driven backend supporting S/4HANA WRICEF trackers migration.",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS Middleware
# Next.js frontend runs on localhost:3000 by default, so we allow origin access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust to specific domains in production (e.g. settings.NEXTAUTH_URL)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register REST and WebSocket API Routers
# Mount chat router at root to expose WebSocket directly at '/ws'
app.include_router(chat_router)
app.include_router(health_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
