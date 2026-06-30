import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.engine import init_db, drop_db, AsyncSessionLocal
from app.models.user import User
from app.models.project import Project
from app.models.permission import Permission
from app.models.audit_log import AuditLog
from app.models.session import Session

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
async def setup_test_db():
    # Setup: Create all tables before tests run
    # Drop first to ensure a clean state if a previous run crashed
    try:
        await drop_db()
    except Exception:
        pass
    await init_db()
    yield
    # Teardown: Drop all tables after tests run
    await drop_db()

@pytest.fixture
async def db_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session

async def test_db_connection(db_session: AsyncSession):
    # Verify the connection works and we can execute a basic query
    result = await db_session.execute(select(1))
    assert result.scalar() == 1

async def test_user_creation(db_session: AsyncSession):
    # Verify user profile insertion and fetch
    new_user = User(
        email="test@example.com",
        display_name="Test User",
        avatar_url="https://example.com/avatar.png",
        google_sub="sub-12345"
    )
    db_session.add(new_user)
    await db_session.commit()

    # Query the user back
    stmt = select(User).where(User.email == "test@example.com")
    result = await db_session.execute(stmt)
    user = result.scalar_one()

    assert user.id is not None
    assert user.display_name == "Test User"
    assert user.google_sub == "sub-12345"
    assert user.created_at is not None

async def test_project_schema_config_default(db_session: AsyncSession):
    # Verify project creation defaults the schema_config to an empty dict
    new_project = Project(
        project_name="Test Project",
        spreadsheet_id="spreadsheet-12345",
        default_tab="Sheet1",
        company_prefix="FF"
    )
    db_session.add(new_project)
    await db_session.commit()

    stmt = select(Project).where(Project.spreadsheet_id == "spreadsheet-12345")
    result = await db_session.execute(stmt)
    project = result.scalar_one()

    assert project.id is not None
    assert project.schema_config == {}
    assert project.is_active is True

async def test_rbac_cascading_deletes(db_session: AsyncSession):
    # Create user
    user = User(email="editor@example.com", display_name="Editor User")
    # Create project
    project = Project(project_name="P1", spreadsheet_id="s-p1")
    db_session.add_all([user, project])
    await db_session.commit()

    # Create permission mapping user to project
    permission = Permission(
        user_id=user.id,
        project_id=project.id,
        role="editor",
        allowed_fields=["RICEFW ID", "Status"],
        denied_operations=["add_row"]
    )
    db_session.add(permission)
    await db_session.commit()

    # Check permission exists
    stmt = select(Permission).where(Permission.user_id == user.id, Permission.project_id == project.id)
    result = await db_session.execute(stmt)
    perm = result.scalar_one()
    assert perm.role == "editor"
    assert perm.allowed_fields == ["RICEFW ID", "Status"]

    # Delete project and verify permissions row is cascade deleted
    await db_session.delete(project)
    await db_session.commit()

    stmt = select(Permission).where(Permission.user_id == user.id)
    result = await db_session.execute(stmt)
    perm_after_delete = result.scalar()
    assert perm_after_delete is None
