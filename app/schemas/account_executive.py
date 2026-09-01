"""The AE directory as the dashboard sees it.

Deliberately a separate schema from :class:`app.schemas.auth.AccountExecutiveOut`, which
the mobile app receives. That one stays byte-identical: ``brandScope`` is dashboard-only
information, and widening the mobile contract to carry a field the app does not use
would be a change to a deployed client for no reason.
"""

from __future__ import annotations

import uuid

from pydantic import Field

from app.models.enums import AeStatus, BrandScope
from app.schemas.auth import RegionOut
from app.schemas.common import CamelModel, nullable_field


class AllocatedStockLine(CamelModel):
    device_model_code: str | None = Field(
        default=None, description="Null when the unit has no catalogued model."
    )
    count: int


class AdminAccountExecutiveOut(CamelModel):
    """The *Tentang AE* panel: ID, Brand, Branch."""

    ae_id: uuid.UUID
    ae_code: str = Field(description="The panel's *ID*.", examples=["AE-BENGKULU2"])
    full_name: str
    # Declared oneOf [string, "null"]: the panel always renders a Brand row, so "not
    # recorded" has to be distinguishable from "the field was not returned".
    brand_scope: BrandScope | None = nullable_field(
        default=None,
        description=(
            "Which telco brands this AE may sell. HYBRID means either. A capability of "
            "the person, not a brand — null means not recorded."
        ),
    )
    region: RegionOut | None = Field(default=None, description="The panel's *Branch*.")
    mpx_code: str | None = None
    status: AeStatus


class AdminAccountExecutiveDetailOut(AdminAccountExecutiveOut):
    allocated_stock: list[AllocatedStockLine] = Field(
        description="Units currently ALLOCATED to this AE, grouped by device model."
    )
    allocated_total: int
