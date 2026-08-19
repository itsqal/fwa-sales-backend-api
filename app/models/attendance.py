"""Module D — attendance (CICO)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, updated_at_column
from app.models.enums import ApprovalStatus


class Attendance(Base):
    """One row per AE per day.

    ``approval_status`` exists because an out-of-geofence check-in is not rejected
    outright — it is routed for supervisor approval, matching the UI warning
    "Lokasi kamu berada di luar jangkauan".
    """

    __tablename__ = "attendance"
    __table_args__ = (
        CheckConstraint(
            "approval_status IN ('AUTO_APPROVED','PENDING_APPROVAL','APPROVED','REJECTED')",
            name="ck_att_approval",
        ),
        CheckConstraint(
            "check_out_at IS NULL OR check_in_at IS NULL OR check_out_at >= check_in_at",
            name="ck_att_order",
        ),
        UniqueConstraint("ae_id", "attendance_date", name="uq_att_ae_date"),
        UniqueConstraint("idempotency_key", name="uq_att_idem"),
    )

    attendance_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    ae_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("account_executive.ae_id"), nullable=False
    )
    attendance_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A storage key, not a URL. The signed URL is built per response and expires.
    check_in_photo_url: Mapped[str | None] = mapped_column(Text)
    check_in_lat: Mapped[float | None] = mapped_column()
    check_in_lng: Mapped[float | None] = mapped_column()
    check_in_address: Mapped[str | None] = mapped_column(Text)
    check_in_is_mocked: Mapped[bool] = mapped_column(nullable=False, server_default=text("FALSE"))
    within_geofence: Mapped[bool | None] = mapped_column()
    check_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_out_lat: Mapped[float | None] = mapped_column()
    check_out_lng: Mapped[float | None] = mapped_column()
    note: Mapped[str | None] = mapped_column(Text)
    document_url: Mapped[str | None] = mapped_column(Text)
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'AUTO_APPROVED'"),
    )
    approval_note: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]
