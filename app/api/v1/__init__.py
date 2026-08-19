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

__all__ = ["api_router"]
