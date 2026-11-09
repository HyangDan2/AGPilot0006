from typing import Optional
from fastapi import Header, HTTPException, status, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from app.config import settings

bearer_scheme = HTTPBearer(auto_error=False)

class CallerContext(BaseModel):
    owner_id: str = "default_user"
    tenant_id: str = "default_tenant"
    access_scope: str = "read_write"
    is_authenticated: bool = True

def get_current_caller(
    auth_creds: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    x_service_token: Optional[str] = Header(None, alias="X-Service-Token"),
    x_owner_id: Optional[str] = Header("default_user", alias="X-Owner-Id"),
    x_tenant_id: Optional[str] = Header("default_tenant", alias="X-Tenant-Id"),
    x_access_scope: Optional[str] = Header("read_write", alias="X-Access-Scope"),
) -> CallerContext:
    """
    Authenticate incoming request using Bearer token or X-Service-Token header.
    Validates token against configured service secret.
    """
    token = None
    if auth_creds and auth_creds.credentials:
        token = auth_creds.credentials
    elif x_service_token:
        token = x_service_token

    # Check for dev bypass
    if not settings.AUTH_REQUIRED or (settings.ENV == "development" and settings.BYPASS_AUTH_IN_DEV):
        return CallerContext(
            owner_id=x_owner_id or "dev_user",
            tenant_id=x_tenant_id or "dev_tenant",
            access_scope=x_access_scope or "admin",
            is_authenticated=True,
        )

    if not token or token != settings.SERVICE_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return CallerContext(
        owner_id=x_owner_id or "default_user",
        tenant_id=x_tenant_id or "default_tenant",
        access_scope=x_access_scope or "read_write",
        is_authenticated=True,
    )

def verify_document_access(doc: dict, caller: CallerContext) -> None:
    """
    Verifies that the caller has permission to view or modify the document.
    Hides document existence with 404 if cross-tenant or unauthorized.
    """
    doc_tenant = doc.get("tenant_id") or "default_tenant"
    doc_owner = doc.get("owner_id") or "default_user"

    # Admin scope has universal access
    if caller.access_scope == "admin":
        return

    # Tenant isolation
    if doc_tenant != caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    # Owner isolation (unless public scope)
    doc_scope = doc.get("access_scope", "private")
    if doc_scope == "private" and doc_owner != caller.owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: you do not have permission for this document",
        )
