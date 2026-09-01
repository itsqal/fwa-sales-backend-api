"""v1 router assembly. One module per OpenAPI tag."""

from fastapi import APIRouter

from app.api.v1 import (
    activations,
    attendance,
    auth,
    customers,
    files,
    internal,
    inventory,
    reports,
    sync,
)
from app.api.v1.admin import admin_router

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(customers.router)
api_router.include_router(inventory.router)
api_router.include_router(activations.router)
api_router.include_router(attendance.router)
api_router.include_router(reports.router)
api_router.include_router(sync.router)
api_router.include_router(internal.router)
api_router.include_router(files.router)

# Admin-audience routes for the supply chain dashboard. Mounted last, and under its
# own prefix, so nothing here can shadow an AE path the mobile app depends on.
api_router.include_router(admin_router)

__all__ = ["api_router"]
