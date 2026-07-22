from typing import List, Dict, Any, Optional
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc
from app.deps import get_current_user, get_db, get_google_token
from app.models.user import User
from app.models.project import Project
from app.models.permission import Permission
from app.models.audit_log import AuditLog
from app.config import settings
from pydantic import BaseModel, HttpUrl
from app.sheets.client import build_sheets_service
from app.core.schema_detect import parse_spreadsheet_url, detect_all_tabs
from app.api.chat import llm_client

logger = logging.getLogger("admin_api")

router = APIRouter(prefix="/admin", tags=["Admin"])

# Admin Guard Dependency
async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.email not in settings.admin_emails_list:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Admin privileges required."
        )
    return current_user


# Pydantic Schemas
class ProjectCreate(BaseModel):
    project_name: str
    spreadsheet_id: str
    default_tab: Optional[str] = "SD"
    company_prefix: Optional[str] = "FFC"
    schema_config: Optional[dict] = None

class FieldToggleRequest(BaseModel):
    tab: str
    field: str
    enabled: bool

class ProjectUpdate(BaseModel):
    project_name: Optional[str] = None
    default_tab: Optional[str] = None
    company_prefix: Optional[str] = None
    is_active: Optional[bool] = None
    schema_config: Optional[dict] = None

class ProjectDetectRequest(BaseModel):
    spreadsheet_url: str

class PermissionUpsert(BaseModel):
    user_email: str
    project_id: int
    role: str # admin, editor, viewer
    allowed_fields: Optional[List[str]] = ["*"]
    denied_operations: Optional[List[str]] = []


# --- Projects CRUD ---

@router.get("/projects", response_model=List[Dict[str, Any]], dependencies=[Depends(require_admin)])
async def list_projects(db: AsyncSession = Depends(get_db)):
    """List all projects registered in the system."""
    result = await db.execute(select(Project))
    projects = result.scalars().all()
    return [{
        "id": p.id,
        "project_name": p.project_name,
        "spreadsheet_id": p.spreadsheet_id,
        "default_tab": p.default_tab,
        "company_prefix": p.company_prefix,
        "is_active": p.is_active,
        "schema_config": p.schema_config,
        "created_at": p.created_at
    } for p in projects]


@router.post("/projects/detect-metadata", dependencies=[Depends(require_admin)])
async def detect_project_metadata(
    payload: ProjectDetectRequest,
    google_token: str = Depends(get_google_token)
):
    """
    Extracts the spreadsheet ID, connects to Sheets API to retrieve all tabs,
    and runs LLM-based schema auto-detection on each tracker tab.
    """
    try:
        spreadsheet_id = parse_spreadsheet_url(payload.spreadsheet_url)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    try:
        service = build_sheets_service(google_token)
        detect_result = await detect_all_tabs(service, spreadsheet_id, llm_client)
        return {
            "spreadsheet_id": spreadsheet_id,
            "detected_config": detect_result
        }
    except Exception as e:
        logger.error(f"Metadata auto-detection failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Auto-detection failed: {str(e)}"
        )


@router.post("/projects", response_model=Dict[str, Any], dependencies=[Depends(require_admin)])
async def create_project(
    payload: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_token: Optional[str] = Depends(get_google_token)
):
    """
    Registers a new spreadsheet project. Parses URL to spreadsheet ID if a full URL is provided.
    Auto-detects schema_config if not explicitly provided.
    """
    try:
        spreadsheet_id = parse_spreadsheet_url(payload.spreadsheet_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    # Check for unique spreadsheet_id
    existing = await db.execute(select(Project).where(Project.spreadsheet_id == spreadsheet_id))
    if existing.scalar():
        raise HTTPException(status_code=400, detail="Spreadsheet ID is already registered.")

    schema_config = payload.schema_config or {}
    if not schema_config and google_token:
        try:
            service = build_sheets_service(google_token)
            detect_result = await detect_all_tabs(service, spreadsheet_id, llm_client)
            if detect_result and "tabs" in detect_result:
                schema_config = detect_result
        except Exception as e:
            logger.warning(f"Auto-detection during project creation failed: {e}")

    new_project = Project(
        project_name=payload.project_name,
        spreadsheet_id=spreadsheet_id,
        default_tab=payload.default_tab,
        company_prefix=payload.company_prefix,
        schema_config=schema_config,
        created_by=current_user.id
    )
    db.add(new_project)
    await db.commit()
    await db.refresh(new_project)

    return {
        "id": new_project.id,
        "project_name": new_project.project_name,
        "spreadsheet_id": new_project.spreadsheet_id,
        "schema_config": new_project.schema_config,
        "status": "created"
    }


@router.put("/projects/{project_id}", response_model=Dict[str, Any], dependencies=[Depends(require_admin)])
async def update_project(project_id: int, payload: ProjectUpdate, db: AsyncSession = Depends(get_db)):
    """Modify project configuration details, including editing the schema_config JSON."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    if payload.project_name is not None:
        project.project_name = payload.project_name
    if payload.default_tab is not None:
        project.default_tab = payload.default_tab
    if payload.company_prefix is not None:
        project.company_prefix = payload.company_prefix
    if payload.is_active is not None:
        project.is_active = payload.is_active
    if payload.schema_config is not None:
        project.schema_config = payload.schema_config

    await db.commit()
    return {"id": project.id, "status": "updated"}


@router.delete("/projects/{project_id}", dependencies=[Depends(require_admin)])
async def delete_project(project_id: int, db: AsyncSession = Depends(get_db)):
    """Deletes a project. This automatically cascades to clean up permission mappings."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    await db.delete(project)
    await db.commit()
    return {"id": project_id, "status": "deleted"}


@router.patch("/projects/{project_id}/fields", dependencies=[Depends(require_admin)])
async def toggle_project_field(
    project_id: int,
    payload: FieldToggleRequest,
    db: AsyncSession = Depends(get_db)
):
    """Dynamically toggle critical fields on or off per tab in project schema_config."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    config = dict(project.schema_config or {})
    tabs = config.get("tabs", {})
    if payload.tab not in tabs:
        raise HTTPException(status_code=400, detail=f"Tab '{payload.tab}' not found in project schema.")

    tab_config = dict(tabs[payload.tab])
    critical_fields = list(tab_config.get("critical_fields", []))

    if payload.enabled and payload.field not in critical_fields:
        critical_fields.append(payload.field)
    elif not payload.enabled and payload.field in critical_fields:
        critical_fields.remove(payload.field)

    tab_config["critical_fields"] = critical_fields
    tabs[payload.tab] = tab_config
    config["tabs"] = tabs
    project.schema_config = config

    await db.commit()
    return {"id": project_id, "tab": payload.tab, "critical_fields": critical_fields, "status": "updated"}


# --- RBAC / Permissions CRUD ---

@router.get("/permissions", response_model=List[Dict[str, Any]], dependencies=[Depends(require_admin)])
async def list_permissions(db: AsyncSession = Depends(get_db)):
    """Return all permissions mappings including user emails and project names."""
    stmt = (
        select(Permission, User.email, Project.project_name)
        .join(User, Permission.user_id == User.id)
        .join(Project, Permission.project_id == Project.id)
    )
    result = await db.execute(stmt)
    records = result.all()

    return [{
        "id": r[0].id,
        "user_email": r[1],
        "project_name": r[2],
        "project_id": r[0].project_id,
        "role": r[0].role,
        "allowed_fields": r[0].allowed_fields,
        "denied_operations": r[0].denied_operations,
        "updated_at": r[0].updated_at
    } for r in records]


@router.post("/permissions", dependencies=[Depends(require_admin)])
async def upsert_permission(payload: PermissionUpsert, db: AsyncSession = Depends(get_db)):
    """Create or update permissions for a user on a project."""
    email_clean = payload.user_email.lower().strip()
    
    # Check if User exists. If not, create them
    user_res = await db.execute(select(User).where(User.email == email_clean))
    user = user_res.scalar()
    if not user:
        user = User(email=email_clean, display_name=email_clean.split("@")[0])
        db.add(user)
        await db.commit()
        await db.refresh(user)

    # Check if project exists
    proj_res = await db.execute(select(Project).where(Project.id == payload.project_id))
    if not proj_res.scalar():
        raise HTTPException(status_code=404, detail="Project not found.")

    # Check if Permission already exists
    perm_res = await db.execute(
        select(Permission).where(Permission.user_id == user.id, Permission.project_id == payload.project_id)
    )
    perm = perm_res.scalar()

    if perm:
        perm.role = payload.role.lower().strip()
        perm.allowed_fields = payload.allowed_fields
        perm.denied_operations = payload.denied_operations
    else:
        perm = Permission(
            user_id=user.id,
            project_id=payload.project_id,
            role=payload.role.lower().strip(),
            allowed_fields=payload.allowed_fields,
            denied_operations=payload.denied_operations
        )
        db.add(perm)

    await db.commit()
    return {"status": "success", "message": "Permissions updated successfully."}


@router.delete("/permissions/{permission_id}", dependencies=[Depends(require_admin)])
async def delete_permission(permission_id: int, db: AsyncSession = Depends(get_db)):
    """Remove a user permission mapping."""
    result = await db.execute(select(Permission).where(Permission.id == permission_id))
    perm = result.scalar()
    if not perm:
        raise HTTPException(status_code=404, detail="Permission mapping not found.")

    await db.delete(perm)
    await db.commit()
    return {"id": permission_id, "status": "deleted"}


# --- Audit Logs ---

@router.get("/audits", response_model=List[Dict[str, Any]], dependencies=[Depends(require_admin)])
async def list_audits(
    user_email: Optional[str] = None,
    tool_name: Optional[str] = None,
    ricefw_id: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db)
):
    """Retrieve audit logs with filters."""
    query = select(AuditLog)
    if user_email:
        query = query.where(AuditLog.user_email == user_email.lower().strip())
    if tool_name:
        query = query.where(AuditLog.tool_name == tool_name.strip())
    if ricefw_id:
        query = query.where(AuditLog.ricefw_id == ricefw_id.strip().upper())
        
    query = query.order_by(desc(AuditLog.timestamp)).limit(limit)
    result = await db.execute(query)
    audits = result.scalars().all()
    
    return [{
        "id": a.id,
        "timestamp": a.timestamp,
        "user_email": a.user_email,
        "session_id": a.session_id,
        "tool_name": a.tool_name,
        "spreadsheet_id": a.spreadsheet_id,
        "sheet_tab": a.sheet_tab,
        "ricefw_id": a.ricefw_id,
        "field": a.field,
        "old_value": a.old_value,
        "new_value": a.new_value,
        "args_json": a.args_json,
        "result_ok": a.result_ok,
        "error": a.error
    } for a in audits]


@router.get("/analytics/summary", dependencies=[Depends(require_admin)])
async def get_analytics_summary(db: AsyncSession = Depends(get_db)):
    """Compute high-level system analytics metrics."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func

    proj_res = await db.execute(select(func.count(Project.id)))
    projects_count = proj_res.scalar() or 0

    user_res = await db.execute(select(func.count(User.id)))
    users_count = user_res.scalar() or 0

    audit_res = await db.execute(select(func.count(AuditLog.id)))
    audits_total = audit_res.scalar() or 0

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    audits_today_res = await db.execute(select(func.count(AuditLog.id)).where(AuditLog.timestamp >= today_start))
    audits_today = audits_today_res.scalar() or 0

    errors_res = await db.execute(select(func.count(AuditLog.id)).where(AuditLog.result_ok == False))
    failed_operations = errors_res.scalar() or 0

    return {
        "projects_count": projects_count,
        "users_count": users_count,
        "audits_today": audits_today,
        "audits_total": audits_total,
        "failed_operations": failed_operations
    }
