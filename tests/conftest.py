"""Test fixtures.

Tests run against **real PostgreSQL**, never SQLite. This schema leans on CHECK
constraints, a functional unique index, identity columns starting at 1000000000, JSONB
and a SQL function; SQLite silently accepts data PostgreSQL rejects, so a green SQLite
suite would prove nothing about the thing being shipped.

Each test runs inside a transaction that is rolled back. The session joins that
transaction with ``join_transaction_mode="create_savepoint"``, so application code is
free to call ``commit()`` and ``rollback()`` exactly as it does in production — those
land on a savepoint and the outer transaction still disappears at the end of the test.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

# Must happen before anything imports app.core.config: environment variables take
# precedence over .env, which is how the suite is kept off the development database.
_TEST_DB = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/api_fwa_sales_test",
)
_UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="fwa-test-uploads-"))

os.environ["DATABASE_URL"] = _TEST_DB
os.environ["APP_ENV"] = "test"
os.environ["APP_TIMEZONE"] = "Asia/Jakarta"
os.environ["JWT_SECRET"] = "test-jwt-secret-long-enough-for-hs256-signing"
os.environ["SERVICE_API_KEY"] = "test-service-key"
os.environ["FILE_URL_SECRET"] = "test-file-signing-secret-long-enough"
os.environ["FILE_STORAGE_DIR"] = str(_UPLOAD_DIR)
os.environ["LOGIN_RATE_LIMIT_ATTEMPTS"] = "5"
os.environ["LOGIN_RATE_LIMIT_WINDOW_SECONDS"] = "60"

import asyncpg  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import NullPool  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.v1 import auth as auth_router  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.deps import get_db  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.enums import AeStatus, CustomerStatus, InventoryStatus  # noqa: E402
from app.models.identity import AccountExecutive, Region  # noqa: E402
from app.models.inventory import DeviceModel, FwaInventory  # noqa: E402

TEST_PASSWORD = "TestPassword123!"


def _admin_dsn() -> str:
    """A DSN for the ``postgres`` maintenance database on the same server."""
    base = _TEST_DB.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + "/postgres"


async def _create_database_if_missing() -> None:
    name = _TEST_DB.rsplit("/", 1)[1]
    conn = await asyncpg.connect(_admin_dsn())
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def prepare_database() -> Iterator[None]:
    """Create the scratch database and bring it to head before anything runs."""
    from alembic import command
    from alembic.config import Config

    asyncio.run(_create_database_if_missing())

    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    command.upgrade(config, "head")
    yield


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    # Function-scoped on purpose: an engine built on one event loop cannot hand out
    # connections on another, and pytest-asyncio gives each test its own loop.
    settings = get_settings()
    created = create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"timezone": settings.timezone}},
    )
    yield created
    await created.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        maker = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as test_session:
            yield test_session
        await transaction.rollback()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        # Mirrors app.db.session.session_scope: the request commits on success and
        # rolls back on failure. Both land on a savepoint here.
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    # The login limiter is process-global; without this, tests leak attempts into
    # each other and the failure looks like a flaky 429.
    auth_router._login_limiter = None

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test/v1") as http_client:
        yield http_client

    fastapi_app.dependency_overrides.clear()
    auth_router._login_limiter = None


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def region(session: AsyncSession) -> Region:
    row = Region(
        region_code=f"RGN{uuid.uuid4().hex[:6]}", region_name="Bengkulu", province="Bengkulu"
    )
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def make_ae(session: AsyncSession, region: Region):
    async def factory(
        *,
        ae_code: str | None = None,
        password: str = TEST_PASSWORD,
        status: AeStatus = AeStatus.ACTIVE,
        must_change_pw: bool = False,
    ) -> AccountExecutive:
        ae = AccountExecutive(
            ae_code=ae_code or f"AE-TEST{uuid.uuid4().hex[:8].upper()}",
            full_name="Hendra Jaya",
            password_hash=hash_password(password),
            region_id=region.region_id,
            mpx_code="MPX-BKL-01",
            status=status,
            must_change_pw=must_change_pw,
        )
        session.add(ae)
        await session.flush()
        return ae

    return factory


@pytest.fixture
async def ae(make_ae) -> AccountExecutive:
    return await make_ae()


@pytest.fixture
async def other_ae(make_ae) -> AccountExecutive:
    return await make_ae()


@pytest.fixture
async def auth_headers(client: AsyncClient, ae: AccountExecutive) -> dict[str, str]:
    response = await client.post(
        "/auth/login", json={"aeCode": ae.ae_code, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


@pytest.fixture
async def other_auth_headers(client: AsyncClient, other_ae: AccountExecutive) -> dict[str, str]:
    response = await client.post(
        "/auth/login", json={"aeCode": other_ae.ae_code, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


@pytest.fixture
async def device_model(session: AsyncSession) -> DeviceModel:
    model = DeviceModel(model_code=f"HKM {uuid.uuid4().hex[:4]}", brand="HKM", sku="SKU-1")
    session.add(model)
    await session.flush()
    return model


# One counter, fixed widths. The CHECK constraints on fwa_inventory are strict about
# length (MSISDN 62+8-13, IMEI 14-16, ICCID 18-20), so a sloppy fixture fails the
# insert rather than the assertion.
_unit_counter = iter(range(10_000, 99_999))


@pytest.fixture
async def make_inventory(session: AsyncSession, device_model: DeviceModel):
    async def factory(
        *,
        ae: AccountExecutive | None = None,
        status: InventoryStatus = InventoryStatus.ALLOCATED,
    ) -> FwaInventory:
        suffix = next(_unit_counter)
        item = FwaInventory(
            msisdn=f"628588272{suffix:05d}",
            iccid=f"896201000020391{suffix:05d}",
            imei=f"3558066713{suffix:05d}",
            device_model_id=device_model.device_model_id,
            allocated_ae_id=ae.ae_id if ae is not None else None,
            allocated_at=datetime.now(),
            status=status,
        )
        session.add(item)
        await session.flush()
        return item

    return factory


@pytest.fixture
async def make_customer(session: AsyncSession):
    counter = iter(range(1000, 9999))

    async def factory(
        *,
        ae: AccountExecutive,
        status: CustomerStatus = CustomerStatus.PURCHASE,
        visit_date: date | None = None,
    ) -> Customer:
        customer = Customer(
            ae_id=ae.ae_id,
            visit_date=visit_date or date.today(),
            full_name="Susanto",
            phone_number=f"08223456{next(counter)}",
            address="Jl. Kebahagiaan No. 12",
            latitude=-3.7928,
            longitude=102.2608,
            geo_accuracy_m=12.5,
            geo_verified=True,
            status=status,
        )
        session.add(customer)
        await session.flush()
        return customer

    return factory


@pytest.fixture
def service_headers() -> dict[str, str]:
    return {"X-Service-Key": "test-service-key"}


@pytest.fixture
def today() -> date:
    return datetime.now(get_settings().tz).date()


@pytest.fixture
def yesterday(today: date) -> date:
    return today - timedelta(days=1)
