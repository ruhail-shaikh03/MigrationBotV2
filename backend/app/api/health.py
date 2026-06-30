from fastapi import APIRouter

router = APIRouter(tags=["Health"])

@router.get("/health")
async def health_check():
    """Simple API status checks."""
    return {"status": "ok", "service": "MigrationBot Enterprise API"}
