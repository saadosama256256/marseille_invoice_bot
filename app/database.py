
import os
import aiosqlite
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "./data/bot.db"))


async def init_db():
    """Create database and required tables."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                request_text TEXT NOT NULL,
                extracted_data TEXT,
                prepared_data TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                invoice_id TEXT UNIQUE,
                invoice_number TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        columns = {
            row[1]
            for row in await (await db.execute(
                "PRAGMA table_info(requests)"
            )).fetchall()
        }
        if "prepared_data" not in columns:
            await db.execute(
                "ALTER TABLE requests ADD COLUMN prepared_data TEXT"
            )
        if "invoice_number" not in columns:
            await db.execute(
                "ALTER TABLE requests ADD COLUMN invoice_number TEXT"
            )
        if "client_id" not in columns:
            await db.execute(
                "ALTER TABLE requests ADD COLUMN client_id TEXT"
            )
        if "client_state" not in columns:
            await db.execute(
                "ALTER TABLE requests ADD COLUMN client_state TEXT"
            )

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_requests_user
            ON requests(telegram_user_id)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_requests_status
            ON requests(status)
        """)

        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_invoice_number
            ON requests(invoice_number)
            WHERE invoice_number IS NOT NULL
        """)

        await db.commit()


async def create_request(user_id: int, request_text: str) -> int:
    """Save a new invoice request."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO requests (telegram_user_id, request_text)
            VALUES (?, ?)
            """,
            (user_id, request_text)
        )
        await db.commit()
        return cursor.lastrowid


async def update_request(request_id: int, status: str,
                         extracted_data: str = None,
                         prepared_data: str = None):
    """Update request status and extracted/prepared data."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE requests
            SET status = ?,
                extracted_data = COALESCE(?, extracted_data),
                prepared_data = COALESCE(?, prepared_data),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, extracted_data, prepared_data, request_id)
        )
        await db.commit()


async def claim_approval(request_id: int) -> bool:
    """Atomically claim an awaiting request for invoice creation."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE requests
            SET status = 'creating',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'awaiting_approval'
              AND invoice_id IS NULL
              AND prepared_data IS NOT NULL
            """,
            (request_id,)
        )
        await db.commit()
        return cursor.rowcount == 1


async def claim_client_creation(request_id: int) -> bool:
    """Atomically claim a request for creating/selecting its client once."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE requests
            SET client_state = 'creating',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND client_id IS NULL
              AND (
                    client_state IS NULL
                    OR client_state IN (
                        'pending', 'failed', 'unknown', 'needs_client_choice'
                    )
                  )
            """,
            (request_id,)
        )
        await db.commit()
        return cursor.rowcount == 1


async def set_client_created(request_id: int, client_id: str,
                             client_name: str = None):
    """Persist a confirmed client immediately after selection/creation."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE requests
            SET client_id = ?,
                client_state = 'created',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (client_id, request_id)
        )
        await db.commit()


async def set_client_state(request_id: int, state: str):
    """Update only the client workflow state."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE requests
            SET client_state = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (state, request_id)
        )
        await db.commit()


async def get_or_set_invoice_number(request_id: int, candidate: str) -> str:
    """Return the request invoice number, assigning it once if missing."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE requests
            SET invoice_number = COALESCE(invoice_number, ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (candidate, request_id)
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT invoice_number FROM requests WHERE id = ?",
            (request_id,)
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            raise RuntimeError("تعذر حفظ رقم الفاتورة.")
        return row[0]


async def set_invoice_created(request_id: int, invoice_id: str,
                              invoice_number: str):
    """Persist the Invoice Ninja result immediately after creation."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE requests
            SET invoice_id = ?,
                invoice_number = ?,
                status = 'created',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (invoice_id, invoice_number, request_id)
        )
        await db.commit()


async def set_request_status(request_id: int, status: str):
    """Update only the workflow status."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE requests
            SET status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, request_id)
        )
        await db.commit()


async def get_request(request_id: int):
    """Retrieve a request by its ID."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM requests WHERE id = ?",
            (request_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def set_invoice_id(request_id: int, invoice_id: str):
    """Save the created Invoice Ninja invoice ID."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE requests
            SET invoice_id = ?,
                status = 'created',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (invoice_id, request_id)
        )
        await db.commit()
