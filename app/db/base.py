"""Declarative base and the column types shared across models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from sqlalchemy import DateTime, text
from sqlalchemy.orm import DeclarativeBase, mapped_column

# TIMESTAMPTZ everywhere, defaulted by the database. The server is authoritative on
# time, so defaults are server-side rather than Python-side: a row written by a
# migration or a psql session gets the same treatment as one written by the API.
timestamptz = Annotated[datetime, mapped_column(DateTime(timezone=True))]

created_at_column = Annotated[
    datetime,
    mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
]
updated_at_column = Annotated[
    datetime,
    mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    ),
]


class Base(DeclarativeBase):
    pass
