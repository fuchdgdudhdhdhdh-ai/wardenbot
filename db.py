import aiosqlite
from pathlib import Path
from datetime import datetime, timezone

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, role TEXT NOT NULL DEFAULT 'Новичок', balance REAL NOT NULL DEFAULT 0,
 joined_at TEXT NOT NULL, banned INTEGER NOT NULL DEFAULT 0, restricted_until TEXT,
 mentor_id INTEGER, team_id INTEGER, referral_code TEXT UNIQUE,
 current_offer_id INTEGER, current_link_id INTEGER
);
CREATE TABLE IF NOT EXISTS offers(
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, min_leads INTEGER NOT NULL,
 price REAL NOT NULL, channel TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links(
 id INTEGER PRIMARY KEY AUTOINCREMENT, offer_id INTEGER NOT NULL, url TEXT NOT NULL UNIQUE,
 assigned_user INTEGER, assigned_at TEXT, active INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(offer_id) REFERENCES offers(id)
);
CREATE TABLE IF NOT EXISTS teams(
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL UNIQUE,
 name TEXT NOT NULL, tag TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, team_id INTEGER NOT NULL, title TEXT NOT NULL,
 body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawals(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL CHECK(amount > 0), status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','rejected')), created_at TEXT NOT NULL, reviewed_at TEXT, admin_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status,created_at);
CREATE TABLE IF NOT EXISTS payout_requests(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, link_id INTEGER,
 leads INTEGER NOT NULL CHECK(leads > 0), price REAL NOT NULL CHECK(price >= 0), amount REAL NOT NULL CHECK(amount >= 0),
 invoice TEXT, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','rejected')),
 cuts INTEGER NOT NULL DEFAULT 0 CHECK(cuts >= 0), admin_id INTEGER, mentor_id INTEGER,
 created_at TEXT NOT NULL, reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_offer_available ON links(offer_id,active,assigned_user,id);
CREATE INDEX IF NOT EXISTS idx_payout_status_created ON payout_requests(status,created_at);
CREATE TABLE IF NOT EXISTS lead_log(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, link_id INTEGER NOT NULL,
 delta INTEGER NOT NULL, created_at TEXT NOT NULL, admin_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lead_log_user_link ON lead_log(user_id,link_id);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news(
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL,
 url TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL, action TEXT NOT NULL,
 target_id INTEGER, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS demo_account_slots(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 account_type TEXT NOT NULL CHECK(account_type IN ('ordinary','premium')),
 label TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','assigned','completed')),
 assigned_user INTEGER,
 assigned_at TEXT,
 completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_demo_slots_available ON demo_account_slots(account_type,status,id);

CREATE TABLE IF NOT EXISTS accounts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 account_class TEXT NOT NULL DEFAULT 'ordinary' CHECK(account_class IN ('ordinary','premium')),
 label TEXT NOT NULL,
 available_for_issue INTEGER NOT NULL DEFAULT 1,
 api_id INTEGER NOT NULL,
 api_hash TEXT NOT NULL,
 session_enc BLOB NOT NULL,
 phone TEXT,
 tg_user_id INTEGER,
 tg_username TEXT,
 twofa_enc BLOB,
 twofa_shared INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'free' CHECK(status IN ('free','leased','disabled','dead')),
 leased_by INTEGER,
 lease_token TEXT,
 leased_at TEXT,
 lease_until TEXT,
 added_by INTEGER,
 client_type TEXT NOT NULL DEFAULT 'telethon',
 proxy TEXT,
 note TEXT,
 created_at TEXT NOT NULL,
 last_used_at TEXT,
 last_check_at TEXT,
 check_failures INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_tg_user ON accounts(tg_user_id) WHERE tg_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_accounts_pick ON accounts(account_class,status,id);
CREATE INDEX IF NOT EXISTS idx_accounts_lease ON accounts(leased_by,status);
CREATE TABLE IF NOT EXISTS account_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 account_id INTEGER NOT NULL,
 user_id INTEGER,
 action TEXT NOT NULL,
 detail TEXT,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_events_acc ON account_events(account_id,id);
CREATE TABLE IF NOT EXISTS account_logins(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 admin_id INTEGER NOT NULL,
 phone TEXT NOT NULL,
 api_id INTEGER NOT NULL,
 api_hash TEXT NOT NULL,
 phone_code_hash TEXT,
 temp_session_enc BLOB,
 state TEXT NOT NULL DEFAULT 'code' CHECK(state IN ('code','2fa')),
 attempts INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


class PgCursor:
    def __init__(self, rows=(), lastrowid=None):
        self.rows = list(rows)
        self.pos = 0
        self.lastrowid = lastrowid
    async def fetchone(self):
        if self.pos >= len(self.rows): return None
        row = self.rows[self.pos]
        self.pos += 1
        return row
    async def fetchall(self):
        rows = self.rows[self.pos:]
        self.pos = len(self.rows)
        return rows


class PgConnection:
    def __init__(self, conn): self.conn = conn
    async def execute(self, sql, args=()):
        import re
        query = sql.strip()
        ignored = bool(re.match(r"INSERT\s+OR\s+IGNORE\s+INTO", query, re.I))
        query = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", query, flags=re.I)
        n = 0
        def repl(_):
            nonlocal n
            n += 1
            return f"${n}"
        query = re.sub(r"\?", repl, query)
        is_insert = bool(re.match(r"INSERT\s+INTO", query, re.I))
        if ignored and "ON CONFLICT" not in query.upper(): query += " ON CONFLICT DO NOTHING"
        if is_insert and not re.search(r"\bRETURNING\b", query, re.I) and not re.match(r"INSERT INTO (settings|admins|schema_migrations)\b", query, re.I): query += " RETURNING id"
        if re.match(r"^(SELECT|WITH)", query, re.I) or " RETURNING " in query.upper():
            rows = await self.conn.fetch(query, *args)
            last = rows[0]["id"] if is_insert and rows and "id" in rows[0] else None
            return PgCursor(rows, last)
        await self.conn.execute(query, *args)
        return PgCursor()


class DB:
    def __init__(self, path):
        import os
        self.path = path
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        self.pool = None

    async def _pool(self):
        if self.pool is None:
            import asyncpg
            from urllib.parse import urlparse
            host = urlparse(self.database_url).hostname or ""
            ssl = "require" if host and not host.endswith(".internal") else None
            self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5, ssl=ssl, command_timeout=30)
        return self.pool

    async def init(self):
        if not self.database_url:
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.path, timeout=15) as db:
                await db.executescript(SCHEMA)
                cols = {row[1] for row in await (await db.execute("PRAGMA table_info(payout_requests)")).fetchall()}
                for col in ("admin_id", "mentor_id"):
                    if col not in cols: await db.execute(f"ALTER TABLE payout_requests ADD COLUMN {col} INTEGER")
                acc_cols = {row[1] for row in await (await db.execute("PRAGMA table_info(accounts)")).fetchall()}
                migrations = {"account_class":"TEXT NOT NULL DEFAULT 'ordinary'","twofa_enc":"BLOB","twofa_shared":"INTEGER NOT NULL DEFAULT 0","client_type":"TEXT NOT NULL DEFAULT 'telethon'","proxy":"TEXT","note":"TEXT","last_used_at":"TEXT","last_check_at":"TEXT","check_failures":"INTEGER NOT NULL DEFAULT 0"}
                for col, ddl in migrations.items():
                    if col not in acc_cols: await db.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")
                user_cols = {row[1] for row in await (await db.execute("PRAGMA table_info(users)")).fetchall()}
                if "balance" not in user_cols: await db.execute("ALTER TABLE users ADD COLUMN balance REAL NOT NULL DEFAULT 0")
                if "available_for_issue" not in acc_cols: await db.execute("ALTER TABLE accounts ADD COLUMN available_for_issue INTEGER NOT NULL DEFAULT 1")
                await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('channel','')")
                await db.commit()
            return

        import os, sqlite3
        pool = await self._pool()
        schema = "\n".join(x for x in SCHEMA.splitlines() if not x.strip().upper().startswith("PRAGMA"))
        schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
        schema = schema.replace("INTEGER PRIMARY KEY", "BIGINT PRIMARY KEY").replace("INTEGER", "BIGINT").replace(" BLOB", " BYTEA")
        async with pool.acquire() as conn:
            for statement in schema.split(";"):
                if statement.strip(): await conn.execute(statement)
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS balance DOUBLE PRECISION NOT NULL DEFAULT 0")
            await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS available_for_issue BIGINT NOT NULL DEFAULT 1")
            # Import the existing persistent SQLite file once, before the new database gets populated.
            if os.path.exists(self.path) and await conn.fetchval("SELECT COUNT(*) FROM users") == 0:
                src = sqlite3.connect(self.path)
                src.row_factory = sqlite3.Row
                tables = ["users","withdrawals","offers","links","teams","events","payout_requests","lead_log","admins","news","settings","admin_audit","schema_migrations","demo_account_slots","accounts","account_events","account_logins"]
                async with conn.transaction():
                    for table in tables:
                        try: rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
                        except sqlite3.Error: continue
                        if not rows: continue
                        cols = list(rows[0].keys())
                        names = ",".join(f'"{x}"' for x in cols)
                        marks = ",".join(f"${i+1}" for i in range(len(cols)))
                        await conn.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({marks}) ON CONFLICT DO NOTHING', [tuple(row[x] for x in cols) for row in rows])
                src.close()
                for table in tables:
                    try: await conn.execute(f"SELECT setval(pg_get_serial_sequence('public.{table}','id'), COALESCE((SELECT MAX(id) FROM {table}),1), true)")
                    except Exception: pass
            await conn.execute("INSERT INTO settings(key,value) VALUES('channel','') ON CONFLICT(key) DO NOTHING")
        print("PostgreSQL schema is ready")

    async def run(self, sql, args=()):
        if self.database_url:
            pool = await self._pool()
            async with pool.acquire() as conn: return (await PgConnection(conn).execute(sql, args)).lastrowid
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            await db.commit()
            return cur.lastrowid

    async def one(self, sql, args=()):
        if self.database_url:
            pool = await self._pool()
            async with pool.acquire() as conn:
                row = await (await PgConnection(conn).execute(sql, args)).fetchone()
                return dict(row) if row else None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            row = await cur.fetchone()
            return dict(row) if row else None

    async def all(self, sql, args=()):
        if self.database_url:
            pool = await self._pool()
            async with pool.acquire() as conn:
                return [dict(x) for x in await (await PgConnection(conn).execute(sql, args)).fetchall()]
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            return [dict(x) for x in await cur.fetchall()]


    async def register(self, u, mentor_id=None):
        async def op(conn):
            cur = await conn.execute("SELECT * FROM users WHERE id=?", (u.id,))
            old = await cur.fetchone()
            if old:
                await conn.execute("UPDATE users SET username=?,first_name=? WHERE id=?",
                                   (u.username, u.first_name, u.id))
                if mentor_id and mentor_id != u.id and not old["mentor_id"]:
                    cur = await conn.execute("SELECT role FROM users WHERE id=?", (mentor_id,))
                    mentor = await cur.fetchone()
                    cur = await conn.execute("SELECT COUNT(*) n FROM users WHERE mentor_id=?", (mentor_id,))
                    pupils = (await cur.fetchone())["n"]
                    if mentor and mentor["role"] == "Наставник" and pupils < 2:
                        await conn.execute("UPDATE users SET mentor_id=? WHERE id=? AND mentor_id IS NULL", (mentor_id, u.id))
            else:
                valid_mentor = None
                if mentor_id and mentor_id != u.id:
                    cur = await conn.execute("SELECT role FROM users WHERE id=?", (mentor_id,))
                    mentor = await cur.fetchone()
                    cur = await conn.execute("SELECT COUNT(*) n FROM users WHERE mentor_id=?", (mentor_id,))
                    pupils = (await cur.fetchone())["n"]
                    if mentor and mentor["role"] == "Наставник" and pupils < 2:
                        valid_mentor = mentor_id
                await conn.execute("""INSERT INTO users(id,username,first_name,joined_at,mentor_id,referral_code)
                                      VALUES(?,?,?,?,?,?)""",
                                   (u.id, u.username, u.first_name, now(), valid_mentor, str(u.id)))
            cur = await conn.execute("SELECT * FROM users WHERE id=?", (u.id,))
            return dict(await cur.fetchone())
        return await self.transaction(op)

    async def is_admin(self, uid, owner_id):
        return uid == owner_id or bool(await self.one("SELECT 1 FROM admins WHERE user_id=?", (uid,)))

    async def get_price(self, user):
        role_bonus = 0.1 if user["role"] in ("Опытный", "Наставник") else 0
        return role_bonus

    async def transaction(self, callback):
        if self.database_url:
            pool = await self._pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    return await callback(PgConnection(conn))
        async with aiosqlite.connect(self.path, timeout=15) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            try:
                result = await callback(conn)
                await conn.commit()
                return result
            except Exception:
                await conn.rollback()
                raise

    async def audit(self, admin_id, action, target_id=None, details=""):
        await self.run("INSERT INTO admin_audit(admin_id,action,target_id,details,created_at) VALUES(?,?,?,?,?)",
                       (admin_id, action, target_id, str(details)[:2000], now()))

    async def take_link(self, user_id, offer_id):
        async def op(conn):
            cur = await conn.execute("SELECT id,current_link_id FROM users WHERE id=?", (user_id,))
            user = await cur.fetchone()
            if not user or user["current_link_id"] is not None:
                return None
            cur = await conn.execute("SELECT id,url FROM links WHERE offer_id=? AND assigned_user IS NULL AND active=1 ORDER BY id LIMIT 1", (offer_id,))
            link = await cur.fetchone()
            if not link:
                return None
            cur = await conn.execute("UPDATE links SET assigned_user=?,assigned_at=? WHERE id=? AND assigned_user IS NULL RETURNING id",
                                     (user_id, now(), link["id"]))
            if not await cur.fetchone():
                return None
            await conn.execute("UPDATE users SET current_offer_id=?,current_link_id=? WHERE id=?",
                               (offer_id, link["id"], user_id))
            return dict(link)
        return await self.transaction(op)

    async def create_payout(self, user_id, link_id, leads, price, mentor_id=None):
        async def op(conn):
            cur = await conn.execute("SELECT id FROM payout_requests WHERE link_id=? AND status='pending'", (link_id,))
            if await cur.fetchone():
                raise ValueError("У вас уже есть заявка на выплату по этой ссылке.")
            cur = await conn.execute("""SELECT COALESCE(SUM(leads),0) used FROM payout_requests
                                        WHERE link_id=? AND status IN ('pending','paid')""", (link_id,))
            used = (await cur.fetchone())["used"]
            if leads <= used:
                raise ValueError("Нет новых лидов для выплаты.")
            payable = leads - used
            amount = round(payable * price, 1)
            cur = await conn.execute("""INSERT INTO payout_requests(user_id,link_id,leads,price,amount,mentor_id,created_at)
                                        VALUES(?,?,?,?,?,?,?)""", (user_id, link_id, payable, price, amount, mentor_id, now()))
            return cur.lastrowid, payable, amount
        return await self.transaction(op)

    async def settle_payout(self, request_id, admin_id, status, cuts=None):
        if status not in ("paid", "rejected"):
            raise ValueError("Invalid payout status")
        async def op(conn):
            cur = await conn.execute("SELECT * FROM payout_requests WHERE id=? AND status='pending'", (request_id,))
            row = await cur.fetchone()
            if not row:
                return None
            amount = row["amount"]
            if cuts is not None:
                if cuts < 0 or cuts > row["leads"]:
                    raise ValueError("Invalid cut count")
                amount = round((row["leads"] - cuts) * row["price"], 1)
            await conn.execute("""UPDATE payout_requests SET status=?,cuts=?,amount=?,reviewed_at=?,admin_id=?
                                  WHERE id=? AND status='pending'""",
                               (status, cuts or 0, amount, now(), admin_id, request_id))
            cur = await conn.execute("SELECT * FROM payout_requests WHERE id=?", (request_id,))
            return dict(await cur.fetchone())
        return await self.transaction(op)

    async def account_stats(self):
        rows = await self.all("SELECT account_class, status, COUNT(*) n FROM accounts GROUP BY account_class, status")
        stats = {"ordinary": {"free": 0, "leased": 0, "dead": 0, "disabled": 0},
                 "premium": {"free": 0, "leased": 0, "dead": 0, "disabled": 0}}
        for r in rows:
            stats.setdefault(r["account_class"], {})[r["status"]] = r["n"]
        return stats

    async def active_leases(self, user_id):
        return await self.all(
            "SELECT * FROM accounts WHERE leased_by=? AND status='leased' ORDER BY account_class", (user_id,))
