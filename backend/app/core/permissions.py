from typing import Tuple, List, Set, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.models.user import User
from app.models.permission import Permission

READ_ONLY_TOOLS: Set[str] = {"get_row", "search_rows", "summarize", "switch_module", "data_quality"}
WRITE_TOOLS: Set[str] = {"update_cell", "bulk_update", "format_row", "add_row"}

class PermissionChecker:
    """
    Checks if a user is allowed to perform a given tool execution for a project.
    Resolves roles hierarchically:
      1. Admin (from configurations) -> absolute access.
      2. Project specific RBAC mapping -> role, allowed columns, denied operations.
      3. Default policy fallback -> editor role with access to all columns.
    """
    def __init__(self, email: str, role: str, allowed_fields: List[str], denied_operations: List[str]):
        self.email = email.lower().strip()
        self.role = role.lower().strip()
        self.allowed_fields = allowed_fields or ["*"]
        self.denied_ops = set(denied_operations or [])

    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_execute(self, tool_name: str, args: dict) -> Tuple[bool, str]:
        """
        Evaluate if tool can run.
        Returns:
          (allowed: bool, reason: str)
        """
        if self.role == "admin":
            return True, ""

        if self.role == "viewer":
            if tool_name not in READ_ONLY_TOOLS:
                return False, (
                    f"You have read-only access and cannot run `{tool_name}`. "
                    "Contact an admin to request write access."
                )
            return True, ""

        if tool_name in self.denied_ops:
            return False, (
                f"You don't have permission to run `{tool_name}`. "
                "Contact an admin if you need this access."
            )

        # Field-level restriction check for update_cell
        if tool_name == "update_cell" and self.allowed_fields != ["*"]:
            updates = args.get("updates", [])
            blocked = [
                upd["field"] for upd in updates
                if upd.get("field") not in self.allowed_fields
            ]
            if blocked:
                fields = ", ".join(blocked)
                return False, (
                    f"You don't have write access to: **{fields}**. "
                    f"Your allowed fields are: {', '.join(self.allowed_fields)}."
                )

        # Field-level restriction check for bulk_update
        if tool_name == "bulk_update" and self.allowed_fields != ["*"]:
            field = args.get("set_field", "")
            if field not in self.allowed_fields:
                return False, (
                    f"You don't have write access to **{field}**. "
                    f"Your allowed fields are: {', '.join(self.allowed_fields)}."
                )

        return True, ""


async def get_user_permissions(db: AsyncSession, email: str, project_id: Optional[int] = None) -> PermissionChecker:
    """
    Query database to resolve PermissionChecker configuration for the user context.
    """
    email_clean = email.lower().strip()
    
    # 1. Admin config match
    if email_clean in settings.admin_emails_list:
        return PermissionChecker(email_clean, role="admin", allowed_fields=["*"], denied_operations=[])

    # Default fallback: Editor with full access
    default_checker = PermissionChecker(email_clean, role="editor", allowed_fields=["*"], denied_operations=[])

    if project_id is None:
        return default_checker

    # 2. Lookup User
    result = await db.execute(select(User.id).where(User.email == email_clean))
    user_id = result.scalar()
    if not user_id:
        return default_checker

    # 3. Lookup Permission mapping for this project
    perm_result = await db.execute(
        select(Permission).where(Permission.user_id == user_id, Permission.project_id == project_id)
    )
    perm = perm_result.scalar()

    if perm:
        return PermissionChecker(
            email=email_clean,
            role=perm.role,
            allowed_fields=perm.allowed_fields,
            denied_operations=perm.denied_operations
        )

    return default_checker
