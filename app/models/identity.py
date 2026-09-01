"""Module A — identity and access."""

from __future__ import annotations

import uuid
from datetime import datetime, time

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, updated_at_column
from app.models.enums import AeStatus, BrandScope


class Region(Base):
    """Sales region. AE codes embed the region name (AE-BENGKULU1, AE-SIDOARJO2)."""

    __tablename__ = "region"
    __table_args__ = (UniqueConstraint("region_code", name="uq_region_code"),)

    region_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    region_code: Mapped[str] = mapped_column(String(50), nullable=False)
    region_name: Mapped[str] = mapped_column(String(120), nullable=False)
    province: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[created_at_column]


class AccountExecutive(Base):
    """The only user of this backend. ``ae_code`` is the natural key issued by IOH HQ."""

    __tablename__ = "account_executive"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','SUSPENDED','INACTIVE')",
            name="ck_ae_status",
        ),
        CheckConstraint(
            "brand_scope IS NULL OR brand_scope IN ('IM3','3ID','HYBRID')",
            name="ck_ae_brand_scope",
        ),
        # Login is case-insensitive, enforced by a functional unique index that
        # autogenerate cannot see. See the initial migration.
        Index("uq_ae_code_ci", func.upper(text("ae_code")), unique=True),
    )

    ae_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    ae_code: Mapped[str] = mapped_column(String(50), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(150))
    region_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("region.region_id")
    )
    mpx_code: Mapped[str | None] = mapped_column(String(50))
    # Which telco brands this AE may sell. Read only by the dashboard — the mobile
    # contract is unchanged by its presence. NULL means not recorded.
    brand_scope: Mapped[BrandScope | None] = mapped_column(
        Enum(
            BrandScope,
            native_enum=False,
            create_constraint=False,
            length=10,
            # "3ID" starts with a digit, so the member name differs from its value and
            # SQLAlchemy would otherwise persist the name. Same treatment as
            # NetworkGeneration.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        )
    )
    work_shift_start: Mapped[time] = mapped_column(
        Time, nullable=False, server_default=text("'08:00'")
    )
    work_shift_end: Mapped[time] = mapped_column(
        Time, nullable=False, server_default=text("'18:00'")
    )
    status: Mapped[AeStatus] = mapped_column(
        Enum(AeStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    must_change_pw: Mapped[bool] = mapped_column(nullable=False, server_default=text("TRUE"))
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]

    region: Mapped[Region | None] = relationship(lazy="joined")


class AuthRefreshToken(Base):
    """One row per device session. Only the token's digest is stored, never the token."""

    __tablename__ = "auth_refresh_token"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_token_hash"),
        Index(
            "ix_refresh_ae_active",
            "ae_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    token_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    ae_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("account_executive.ae_id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    device_label: Mapped[str | None] = mapped_column(String(120))
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
