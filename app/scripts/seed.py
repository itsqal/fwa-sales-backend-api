"""Development fixtures.

Run with ``uv run python -m app.scripts.seed``. Idempotent: re-running updates rather
than duplicating, so it is safe to use as a "reset my dev data" button.

Deliberately **no incentive_rule rows.** The two agreed AE activation tiers are inserted
by migration 0002, so they are already present after ``alembic upgrade head`` and
seeding them again would double every payout. Nothing else about incentive is decided,
and a seeded "plausible" figure is exactly how an invented number becomes the number
everybody assumes was agreed.

The ``network_generation`` values below are **dev placeholders**, chosen only so that
seeded data exercises both tiers. They are not a statement about what these real CPE
models support; the live catalogue must be categorised by the business.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import dispose_engine, get_sessionmaker
from app.models.customer import Customer, CustomerStatusHistory
from app.models.enums import AeStatus, CustomerStatus, InventoryStatus, NetworkGeneration
from app.models.identity import AccountExecutive, Region
from app.models.incentive import AeDailyTarget
from app.models.inventory import DeviceModel, FwaInventory

DEV_PASSWORD = "Password123!"

REGIONS = [
    ("BENGKULU", "Bengkulu", "Bengkulu"),
    ("SIDOARJO", "Sidoarjo", "Jawa Timur"),
]

AES = [
    ("AE-BENGKULU1", "Hendra Jaya", "BENGKULU", "MPX-BKL-01"),
    ("AE-SIDOARJO2", "Rina Kusuma", "SIDOARJO", "MPX-SDA-02"),
]

DEVICE_MODELS = [
    ("HKM 127+", "HKM", "SKU-HKM-127P", NetworkGeneration.FOUR_G),
    ("RABIT CPE-XR", "RABIT", "SKU-RBT-XR", NetworkGeneration.FIVE_G),
    ("ADVAN V1 PRO", "ADVAN", "SKU-ADV-V1P", NetworkGeneration.FOUR_G),
]

CUSTOMER_NAMES = [
    ("Susanto", "082234567890", "Jl. Kebahagiaan No. 12"),
    ("Dewi Anggraini", "081298765432", "Jl. Melati Raya No. 45"),
    ("Bambang Wijaya", "085712345678", "Jl. Cendana Blok C7"),
    ("Sri Wahyuni", "087811223344", "Jl. Anggrek No. 3"),
    ("Agus Priyanto", "081355667788", "Perumahan Griya Indah B2"),
    ("Nur Halimah", "089912345670", "Jl. Kenanga No. 21"),
]


async def seed(session: AsyncSession) -> None:
    regions = {}
    for code, name, province in REGIONS:
        region = await session.scalar(select(Region).where(Region.region_code == code))
        if region is None:
            region = Region(region_code=code, region_name=name, province=province)
            session.add(region)
            await session.flush()
        regions[code] = region

    models = {}
    for model_code, brand, sku, generation in DEVICE_MODELS:
        model = await session.scalar(
            select(DeviceModel).where(DeviceModel.model_code == model_code)
        )
        if model is None:
            model = DeviceModel(
                model_code=model_code, brand=brand, sku=sku, network_generation=generation
            )
            session.add(model)
            await session.flush()
        else:
            # Re-running after 0002 must categorise catalogue rows seeded before it,
            # or the seeded AE accrues nothing and the Insentif tile stays at zero.
            model.network_generation = generation
        models[model_code] = model

    aes = {}
    for ae_code, full_name, region_code, mpx_code in AES:
        ae = await session.scalar(
            select(AccountExecutive).where(AccountExecutive.ae_code == ae_code)
        )
        if ae is None:
            ae = AccountExecutive(
                ae_code=ae_code,
                full_name=full_name,
                password_hash=hash_password(DEV_PASSWORD),
                region_id=regions[region_code].region_id,
                mpx_code=mpx_code,
                status=AeStatus.ACTIVE,
                # False so a dev login goes straight to the app. In production HQ
                # issues the password with this left TRUE.
                must_change_pw=False,
            )
            session.add(ae)
            await session.flush()
        aes[ae_code] = ae

    primary = aes["AE-BENGKULU1"]
    secondary = aes["AE-SIDOARJO2"]

    # Stock, pre-allocated. The AE stock-request workflow is out of scope for this
    # module — units simply arrive allocated.
    rng = random.Random(20260819)
    await _seed_inventory(session, primary, models, rng, prefix="62858827243", start=0, count=8)
    await _seed_inventory(session, secondary, models, rng, prefix="62811223344", start=0, count=4)

    today = datetime.now(get_settings().tz).date()
    await _seed_customers(session, primary, today)
    await _seed_targets(session, primary, today)


async def _seed_inventory(
    session: AsyncSession,
    ae: AccountExecutive,
    models: dict[str, DeviceModel],
    rng: random.Random,
    *,
    prefix: str,
    start: int,
    count: int,
) -> None:
    model_codes = list(models)
    for offset in range(count):
        msisdn = f"{prefix}{start + offset:02d}"
        if await session.get(FwaInventory, msisdn) is not None:
            continue
        model = models[model_codes[offset % len(model_codes)]]
        session.add(
            FwaInventory(
                msisdn=msisdn,
                iccid=f"8962010000{rng.randrange(10**9):09d}",
                imei=f"35580667{rng.randrange(10**7):07d}",
                device_model_id=model.device_model_id,
                allocated_ae_id=ae.ae_id,
                allocated_at=datetime.now(UTC),
                status=InventoryStatus.ALLOCATED,
            )
        )
    await session.flush()


async def _seed_customers(session: AsyncSession, ae: AccountExecutive, today: date) -> None:
    statuses = [
        CustomerStatus.PURCHASE,
        CustomerStatus.HOT_LEADS,
        CustomerStatus.EDUKASI,
        CustomerStatus.HOT_LEADS,
        CustomerStatus.PURCHASE,
        CustomerStatus.EDUKASI,
    ]
    paired = zip(CUSTOMER_NAMES, statuses, strict=True)
    for index, ((name, phone, address), status) in enumerate(paired):
        existing = await session.scalar(
            select(Customer).where(Customer.ae_id == ae.ae_id, Customer.phone_number == phone)
        )
        if existing is not None:
            continue
        customer = Customer(
            ae_id=ae.ae_id,
            visit_date=today - timedelta(days=index % 6),
            full_name=name,
            phone_number=phone,
            address=address,
            latitude=-3.7928 + index * 0.004,
            longitude=102.2608 + index * 0.004,
            geo_accuracy_m=12.5,
            geo_verified=True,
            status=status,
        )
        session.add(customer)
        await session.flush()
        session.add(
            CustomerStatusHistory(
                customer_id=customer.customer_id,
                new_status=status,
                changed_by=ae.ae_id,
                note="Seeded",
            )
        )
    await session.flush()


async def _seed_targets(session: AsyncSession, ae: AccountExecutive, today: date) -> None:
    """Daily targets exist: they are a plan set by the business, not a payout rule."""
    for offset in range(30):
        target_date = today - timedelta(days=offset)
        existing = await session.scalar(
            select(AeDailyTarget).where(
                AeDailyTarget.ae_id == ae.ae_id, AeDailyTarget.target_date == target_date
            )
        )
        if existing is None:
            session.add(
                AeDailyTarget(
                    ae_id=ae.ae_id,
                    target_date=target_date,
                    target_activations=4,
                    target_new_customers=6,
                )
            )
    await session.flush()


async def main() -> None:
    async with get_sessionmaker()() as session:
        await seed(session)
        await session.commit()
    await dispose_engine()

    print("Seeded development fixtures.")
    print(f"  Log in as AE-BENGKULU1 or AE-SIDOARJO2 with password: {DEV_PASSWORD}")
    print("  Incentive rules come from migration 0002 (4G Rp 35.000 / 5G Rp 135.000);")
    print("  the seeded device models carry placeholder 4G/5G values so both tiers fire.")


if __name__ == "__main__":
    asyncio.run(main())
