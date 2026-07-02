"""MongoDB connection, single shared Motor client, and index bootstrap.

Atlas gives us a replica set by default — which is mandatory, because the
payment webhook relies on multi-document transactions to atomically mark a
debt paid AND credit the merchant wallet. Without a replica set,
start_transaction() would throw.
"""
import logging
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import settings

log = logging.getLogger("paywise.db")

# Single shared client for the whole process.
client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None


async def connect() -> None:
    """Called once at FastAPI startup."""
    global client, db
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.mongodb_db_name]
    # Ping to fail fast if the URI is wrong.
    await client.admin.command("ping")
    await ensure_indexes(db)
    log.info("MongoDB connected → db=%s", settings.mongodb_db_name)


async def close() -> None:
    global client
    if client:
        client.close()
        log.info("MongoDB connection closed")


def get_db() -> AsyncIOMotorDatabase:
    if db is None:
        raise RuntimeError("Database not initialised — call connect() at startup.")
    return db


async def ensure_indexes(database: AsyncIOMotorDatabase) -> None:
    """Create the indexes that enforce our invariants.

    The UNIQUE indexes here are NOT optional cosmetic niceties — they are the
    actual engineering guarantee that prevents double-credit and duplicate
    debtors. Create them up-front so they exist before the first webhook.
    """
    await database.merchants.create_index("phone", unique=True)
    await database.merchants.create_index("public_id", unique=True)

    await database.debtors.create_index(
        [("merchant_id", 1), ("phone_normalized", 1)], unique=True
    )

    await database.debts.create_index("reference", unique=True)        # money join key
    await database.debts.create_index([("merchant_id", 1), ("status", 1)])
    await database.debts.create_index("debtor_id")

    await database.virtual_accounts.create_index("account_ref", unique=True)
    await database.virtual_accounts.create_index("account_number")
    await database.virtual_accounts.create_index("merchant_id")

    await database.checkpoints.create_index(
        [("thread_id", 1), ("checkpoint_ns", 1), ("checkpoint_id", 1)],
        unique=True,
    )
    await database.checkpoints.create_index("thread_id")

    await database.transactions.create_index("reference", unique=True)  # idempotency
    await database.transactions.create_index("debt_id")

    await database.withdrawals.create_index(
        "nomba_transfer_id", unique=True, sparse=True
    )

    log.info("MongoDB indexes ensured")
