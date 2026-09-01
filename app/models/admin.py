"""The web dashboard principal, and its sessions.

Deliberately a separate table from :class:`~app.models.identity.AccountExecutive`
rather than a unified ``app_user``. The mobile app is already in the field; unifying
the two would mean touching its live authentication path and backfilling every existing
AE, and any bug there logs out the field force. Two tables and a token audience cost a
little duplication in the token plumbing and nothing else.

The important invariant is ``ck_admin_org_binding``: a principal carries exactly one
organisation binding, as wide as its role allows and no wider. It is a database CHECK
and not a service-layer assertion because it is what separates three competing
companies reading one dataset.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, updated_at_column
from app.models.enums import AdminRole, AdminStatus
from app.models.organisation import DevicePartner, Mpx


class AdminUser(Base):
    """A DP, IOH, or MPX administrator of the web dashboard."""

    __tablename__ = "admin_user"
    __table_args__ = (
        CheckConstraint(
            "role IN ('DP_ADMIN','IOH_ADMIN','MPX_ADMIN')",
            name="ck_admin_role",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','SUSPENDED','INACTIVE')",
            name="ck_admin_status",
        ),
        CheckConstraint(
            "(role = 'DP_ADMIN'  AND device_partner_id IS NOT NULL AND mpx_id IS NULL) OR "
            "(role = 'MPX_ADMIN' AND mpx_id IS NOT NULL AND device_partner_id IS NULL) OR "
            "(role = 'IOH_ADMIN' AND device_partner_id IS NULL AND mpx_id IS NULL)",
            name="ck_admin_org_binding",
        ),
        # Login is case-insensitive, as it is for an AE code. Functional unique indexes
        # are invisible to autogenerate — see the migration.
        Index("uq_admin_username_ci", func.upper(text("username")), unique=True),
        Index(
            "uq_admin_email_ci",
            func.upper(text("email")),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
    )

    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    email: Mapped[str | None] = mapped_column(String(150))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        Enum(AdminRole, native_enum=False, create_constraint=False, length=20),
        nullable=False,
    )
    # Exactly one of the next two is set, decided by `role`. See ck_admin_org_binding.
    device_partner_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("device_partner.device_partner_id")
    )
    mpx_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("mpx.mpx_id"))
    must_change_pw: Mapped[bool] = mapped_column(nullable=False, server_default=text("TRUE"))
    status: Mapped[AdminStatus] = mapped_column(
        Enum(AdminStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]

    device_partner: Mapped[DevicePartner | None] = relationship(lazy="joined")
    mpx: Mapped[Mpx | None] = relationship(lazy="joined")


class AdminRefreshToken(Base):
    """One row per browser session. Mirrors ``auth_refresh_token`` exactly."""

    __tablename__ = "admin_refresh_token"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_admin_refresh_token_hash"),
        Index(
            "ix_admin_refresh_active",
            "admin_user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    token_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("admin_user.admin_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    device_label: Mapped[str | None] = mapped_column(String(120))
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
