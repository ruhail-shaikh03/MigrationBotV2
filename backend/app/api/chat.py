import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.engine import get_db, AsyncSessionLocal
from app.deps import get_current_user
from app.models.user import User
from app.models.project import Project
from app.models.session import Session as UserSession
from app.core.permissions import get_user_permissions
from app.core.agentic_loop import run_agentic_loop
from app.core.column_mapper import COLUMN_ALIASES
from app.queue.events import queue_events_channel
from app.sheets.client import build_sheets_service
from app.sheets.meta import switch_module
from app.config import settings
from openai import AsyncOpenAI
from jose import jwt, JWTError

logger = logging.getLogger("chat")

router = APIRouter(tags=["Chat"])

# Instantiate the LLM Client
# We default to DeepSeek official base URL, but users can override or direct it
llm_client = AsyncOpenAI(
    api_key=settings.DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1" if "deepseek.com" in settings.DEEPSEEK_API_KEY or settings.DEEPSEEK_API_KEY.startswith("sk-") else "https://api.openai.com/v1"
)

async def authenticate_ws_user(token: str, db: AsyncSession) -> Optional[tuple[User, str]]:
    """Helper to authenticate WebSocket connections via token query parameter. Returns (User, google_access_token)."""
    try:
        # Standard decode
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        email = payload.get("email")
        google_access_token = payload.get("google_access_token", "mock-google-access-token")
    except JWTError:
        # Developer mock fallback
        if token.startswith("mock-") or "@" in token:
            email = token.replace("mock-", "")
            google_access_token = "mock-google-access-token"
        else:
            return None

    if not email:
        return None

    email_clean = email.lower().strip()
    result = await db.execute(select(User).where(User.email == email_clean))
    user = result.scalar()
    if not user:
        return None
    return user, google_access_token


@router.websocket("/ws")
async def websocket_chat_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
    project_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """
    WebSocket endpoint for real-time, streaming conversational interactions.
    Handles message reception, authorization, and streams responses token-by-token.
    """
    await websocket.accept()
    logger.info("WebSocket connection requested.")

    # 1. Authenticate connection
    auth_result = await authenticate_ws_user(token, db)
    if not auth_result:
        logger.warning("WebSocket authentication failed.")
        await websocket.send_json({"type": "error", "message": "Authentication failed."})
        await websocket.close(code=1008)
        return

    user, google_access_token = auth_result
    logger.info(f"WebSocket authenticated for user: {user.email}")

    # 2. Set up or load Session state context
    active_project_id = project_id
    if not active_project_id:
        # Fallback to last active project if not specified
        sess_res = await db.execute(
            select(UserSession).where(UserSession.user_id == user.id).order_by(UserSession.last_active.desc())
        )
        user_sess = sess_res.scalar()
        if user_sess:
            active_project_id = user_sess.project_id

    # If still no project, locate default or first available project
    if not active_project_id:
        proj_res = await db.execute(select(Project).where(Project.is_active == True).limit(1))
        default_proj = proj_res.scalar()
        if default_proj:
            active_project_id = default_proj.id

    if not active_project_id:
        await websocket.send_json({
            "type": "error", 
            "message": "No active projects found. Please contact an admin to register a project."
        })
        await websocket.close(code=1008)
        return

    # Load project details
    proj_res = await db.execute(select(Project).where(Project.id == active_project_id))
    project = proj_res.scalar()
    if not project:
        await websocket.send_json({"type": "error", "message": f"Project ID {active_project_id} not found."})
        await websocket.close(code=1008)
        return

    # Check / Load active User Session in database
    sess_res = await db.execute(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.project_id == project.id)
    )
    user_sess = sess_res.scalar()
    if not user_sess:
        user_sess = UserSession(
            user_id=user.id,
            project_id=project.id,
            active_tab=project.default_tab or "SD"
        )
        db.add(user_sess)
        await db.commit()
        await db.refresh(user_sess)

    # Initialize in-memory conversation history for this WS session
    message_history = []

    # Send success initialization packet
    await websocket.send_json({
        "type": "connection_ok",
        "user_email": user.email,
        "project_name": project.project_name,
        "active_tab": user_sess.active_tab
    })

    # Define message sender helper
    # The agentic loop and the queue-event forwarder both write to this socket, so
    # serialize sends to avoid interleaving two frames on the same connection.
    send_lock = asyncio.Lock()

    async def send_msg(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def forward_queue_updates() -> None:
        """Relays queue_update events published by the out-of-process write worker to this client."""
        sub_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = sub_client.pubsub()
        try:
            await pubsub.subscribe(queue_events_channel(user.email))
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    await send_msg(json.loads(message["data"]))
                except Exception as e:
                    # Socket is gone or the payload is unreadable; stop relaying
                    logger.warning(f"Failed to forward queue update to {user.email}: {e}")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Redis being unavailable must not take down the chat connection itself
            logger.warning(f"Queue update subscription unavailable for {user.email}: {e}")
        finally:
            try:
                await pubsub.aclose()
                await sub_client.aclose()
            except Exception:
                pass

    queue_listener = asyncio.create_task(forward_queue_updates())

    # 3. Message loop
    try:
        while True:
            # Receive raw text message
            data = await websocket.receive_text()
            try:
                packet = json.loads(data)
            except Exception:
                packet = {"type": "message", "content": data.strip()}

            # Route on the frame's declared type instead of only ever reading `content` —
            # previously a {"type": "ping"} heartbeat yielded content="" and fell through
            # to the empty-message guard below, so `pong` only fired if a user literally
            # typed the word "ping" into the chat box.
            packet_type = packet.get("type", "message")

            if packet_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if packet_type == "switch_tab":
                tab_name = str(packet.get("tab_name", "")).strip()
                if not tab_name:
                    await websocket.send_json({"type": "error", "message": "switch_tab requires a non-empty tab_name."})
                    continue

                async with AsyncSessionLocal() as fresh_db:
                    stmt_sess = select(UserSession).where(UserSession.id == user_sess.id)
                    sess_res = await fresh_db.execute(stmt_sess)
                    current_sess = sess_res.scalar_one()

                    stmt_proj = select(Project).where(Project.id == current_sess.project_id)
                    proj_res = await fresh_db.execute(stmt_proj)
                    current_project = proj_res.scalar_one()

                    checker = await get_user_permissions(fresh_db, user.email, current_project.id)
                    allowed, reason = checker.can_execute("switch_module", {})
                    if not allowed:
                        await websocket.send_json({"type": "error", "message": f"Permission denied: {reason}"})
                        continue

                    service = build_sheets_service(google_access_token)
                    result = await switch_module(
                        spreadsheet_id=current_project.spreadsheet_id,
                        tab_name=tab_name,
                        db=fresh_db,
                        user_email=user.email,
                        session_id=current_sess.id,
                        service=service
                    )

                if result.get("ok"):
                    await websocket.send_json({"type": "tab_switched", "active_tab": result["active_tab"]})
                else:
                    await websocket.send_json({"type": "error", "message": result.get("error", "Failed to switch tab.")})
                continue

            if packet_type != "message":
                # Unrecognised control frame type — ignore rather than misinterpret as chat text.
                continue

            user_msg = packet.get("content", "").strip()
            if not user_msg:
                continue

            logger.info(f"Received message from {user.email}: '{user_msg}'")

            # We use a short-lived DB session inside the loop iteration to fetch
            # the latest state of project config / user sessions (avoiding stale ORM cache)
            async with AsyncSessionLocal() as fresh_db:
                # Reload session details
                stmt_sess = select(UserSession).where(UserSession.id == user_sess.id)
                sess_res = await fresh_db.execute(stmt_sess)
                current_sess = sess_res.scalar_one()

                # Reload project details
                stmt_proj = select(Project).where(Project.id == current_sess.project_id)
                proj_res = await fresh_db.execute(stmt_proj)
                current_project = proj_res.scalar_one()

                # Load permission controls
                checker = await get_user_permissions(fresh_db, user.email, current_project.id)

                # Resolve tab-specific schema if using the new multi-tab format
                proj_schema = current_project.schema_config
                active_tab_schema = proj_schema.get("tabs", {}).get(current_sess.active_tab, {}) if "tabs" in proj_schema else proj_schema
                
                # Fetch or map dynamic columns
                column_map = active_tab_schema.get("column_map") or proj_schema.get("column_map") or COLUMN_ALIASES

                # Execute the agentic loop
                message_history = await run_agentic_loop(
                    user_message=user_msg,
                    message_history=message_history,
                    user_email=user.email,
                    session_id=current_sess.id,
                    spreadsheet_id=current_project.spreadsheet_id,
                    active_tab=current_sess.active_tab,
                    schema_config=current_project.schema_config,
                    column_map=column_map,
                    checker=checker,
                    llm_client=llm_client,
                    send_websocket_msg=send_msg,
                    db_session=fresh_db,
                    google_access_token=google_access_token
                )

                # Update session activity timestamp
                current_sess.last_active = datetime.now(timezone.utc)
                await fresh_db.commit()

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for user: {user.email}")
    except Exception as e:
        logger.error(f"WebSocket server error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": f"Server error: {str(e)}"})
        except Exception:
            pass
    finally:
        # Tear down the queue subscription with the connection
        queue_listener.cancel()
        try:
            await queue_listener
        except (asyncio.CancelledError, Exception):
            pass


@router.get("/api/projects", response_model=List[Dict[str, Any]])
async def list_user_projects(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """List active projects in the system for the current authenticated user."""
    result = await db.execute(select(Project).where(Project.is_active == True))
    projects = result.scalars().all()
    return [{
        "id": p.id,
        "project_name": p.project_name,
        "spreadsheet_id": p.spreadsheet_id,
        "default_tab": p.default_tab,
        "company_prefix": p.company_prefix,
        "schema_config": p.schema_config
    } for p in projects]

