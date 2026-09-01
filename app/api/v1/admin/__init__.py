"""Admin-audience routers for the supply chain web dashboard.

Every router in this package mounts under ``/admin`` and requires an admin token.
Sliced by use case rather than by role: MSISDN PO is one feature that a Device Partner
and IOH see from two sides, and they share a status machine.
"""

from fastapi import APIRouter

from app.api.v1.admin import (
    account_executives,
    addresses,
    auth,
    device_po,
    fulfilment,
    msisdn_po,
    reference,
)

admin_router = APIRouter(prefix="/admin")
admin_router.include_router(auth.router)
admin_router.include_router(reference.router)
admin_router.include_router(msisdn_po.router)
admin_router.include_router(device_po.router)
admin_router.include_router(account_executives.router)
admin_router.include_router(addresses.router)
# Shipment and receipt hang off the device-PO prefix; stock and allocation are
# top-level. Mounted after device_po so the PO routes are registered first.
admin_router.include_router(fulfilment.po_router)
admin_router.include_router(fulfilment.shipment_router)
admin_router.include_router(fulfilment.stock_router)

__all__ = ["admin_router"]
