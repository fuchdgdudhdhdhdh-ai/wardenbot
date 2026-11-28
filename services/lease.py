"""Атомарная аренда аккаунтов и контроль срока действия."""
import asyncio, secrets, logging
from math import ceil
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)
class AccountBusy(RuntimeError): pass
class AccountLimit(RuntimeError): pass
class AccountUnavailable(RuntimeError): pass
class NotYourAccount(RuntimeError): pass
class LeaseExpired(RuntimeError): pass
class LeaseLocked(RuntimeError): pass

LOCK_DEFAULT_MIN = 900

def utcnow():
    return datetime.now(timezone.utc)

def _stamp(dt):
    return dt.isoformat()

class LeaseService:
    def __init__(self, db, client_pool, ttl_min=900, lock_min=LOCK_DEFAULT_MIN, notify=None):
        self.db, self.pool = db, client_pool
        self.ttl_min, self.lock_min, self.notify = ttl_min, lock_min, notify

    def lock_left(self, leased_at) -> int:
        """Минуты до конца защитного окна; 0 — освобождать уже можно."""
        if not self.lock_min or not leased_at:
            return 0
        try:
            until = datetime.fromisoformat(leased_at) + timedelta(minutes=self.lock_min)
        except (TypeError, ValueError):
            return 0
        return max(0, int((until - utcnow()).total_seconds() // 60))

    async def _event(self, acc_id, user_id, action, detail=None):
        await self.db.run("INSERT INTO account_events(account_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                          (acc_id, user_id, action, detail, _stamp(utcnow())))

    async def take_for_class(self, acc_id: int, user_id: int, account_class: str):
        now = utcnow(); until = now + timedelta(minutes=self.ttl_min)
        token = secrets.token_urlsafe(32)
        async def op(conn):
            cur = await conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,))
            row = await cur.fetchone()
            if not row:
                return "missing", None
            if row["status"] in ("disabled", "dead"):
                return "off", None
            if row["status"] == "leased" and row["leased_by"] == user_id:
                return "yours", row["lease_token"]
            if row["status"] == "leased" and row["lease_until"] and row["lease_until"] > _stamp(now):
                return "busy", None
            cur = await conn.execute(
                "SELECT id FROM accounts WHERE leased_by=? AND status='leased' AND account_class=? AND lease_until>? LIMIT 1",
                (user_id, account_class, _stamp(now)))
            if await cur.fetchone() is not None:
                return "limit", None
            cur = await conn.execute(
                """UPDATE accounts SET status='leased',leased_by=?,lease_token=?,leased_at=?,lease_until=?,
                   session_lost=0,check_failures=0
                   WHERE id=? AND available_for_issue=1
                   AND (status='free' OR (status='leased' AND (lease_until IS NULL OR lease_until<=?)))
                   RETURNING id""",
                (user_id, token, _stamp(now), _stamp(until), acc_id, _stamp(now)))
            if await cur.fetchone() is None:
                return "busy", None
            return "ok", token
        result, value = await self.db.transaction(op)
        if result == "missing":
            raise AccountUnavailable("Аккаунт не найден.")
        if result == "off":
            raise AccountUnavailable("Аккаунт отключён или помечен мёртвым.")
        if result == "busy":
            raise AccountBusy("Аккаунт занят другим пользователем.")
        if result == "limit":
            raise AccountLimit("У вас уже есть аккаунт этого типа. Освободите его, чтобы взять другой.")
        if result == "yours":
            return value
        await self.pool.disconnect(acc_id)
        await self._event(acc_id, user_id, "take")
        return value

    async def take(self, acc_id: int, user_id: int) -> str:
        now = utcnow(); until = now + timedelta(minutes=self.ttl_min)
        token = secrets.token_urlsafe(32)
        async def op(conn):
            cur = await conn.execute(
                """UPDATE accounts SET status='leased',leased_by=?,lease_token=?,leased_at=?,lease_until=?
                   WHERE id=? AND status IN ('free','leased')
                   AND (status='free' OR leased_by=? OR lease_until IS NULL OR lease_until<=?)
                   RETURNING id""",
                (user_id, token, _stamp(now), _stamp(until), acc_id, user_id, _stamp(now)))
            return await cur.fetchone()
        if await self.db.transaction(op) is None:
            raise AccountBusy("Аккаунт занят другим пользователем.")
        await self.pool.disconnect(acc_id)
        await self._event(acc_id, user_id, "take")
        return token

    async def guard_lease(self, acc_id, user_id):
        row = await self.db.one("SELECT leased_by,lease_until,status FROM accounts WHERE id=?", (acc_id,))
        if not row or row["leased_by"] != user_id or row["status"] != "leased":
            raise NotYourAccount("Аккаунт занят другим пользователем или аренда отсутствует.")
        try:
            if not row["lease_until"] or datetime.fromisoformat(row["lease_until"]) <= utcnow():
                raise LeaseExpired("Срок аренды истёк. Возьмите аккаунт повторно.")
        except ValueError as exc:
            raise LeaseExpired("Срок аренды некорректен. Обратитесь к администратору.") from exc
        return row

    async def release(self, acc_id: int, user_id: int) -> None:
        row = await self.db.one("SELECT leased_by,leased_at FROM accounts WHERE id=?", (acc_id,))
        if not row or row["leased_by"] != user_id:
            raise NotYourAccount("Это не ваш аккаунт.")
        left = self.lock_left(row["leased_at"])
        if left:
            until = datetime.fromisoformat(row["leased_at"]) + timedelta(minutes=self.lock_min)
            raise LeaseLocked(f"Аккаунт закреплён за вами до {until:%d.%m %H:%M} UTC. "
                              f"Освободить можно через {ceil(left / 60)} ч.")
        async def op(conn):
            cur = await conn.execute(
                """UPDATE accounts SET status='free',leased_by=NULL,lease_token=NULL,
                   leased_at=NULL,lease_until=NULL,session_lost=0 WHERE id=? AND leased_by=? RETURNING id""",
                (acc_id, user_id))
            return await cur.fetchone()
        if await self.db.transaction(op) is None:
            raise NotYourAccount("Это не ваш аккаунт.")
        await self._event(acc_id, user_id, "release")
        await self.pool.disconnect(acc_id)

    async def force_release(self, acc_id, admin_id):
        async def op(conn):
            cur = await conn.execute(
                """UPDATE accounts SET status='free',leased_by=NULL,lease_token=NULL,
                   leased_at=NULL,lease_until=NULL,session_lost=0 WHERE id=? AND status='leased' RETURNING id""",
                (acc_id,))
            return await cur.fetchone()
        if await self.db.transaction(op) is None:
            raise NotYourAccount("Аккаунт сейчас не арендован.")
        await self._event(acc_id, admin_id, "force_release")
        await self.pool.disconnect(acc_id)

    async def enforce_fixed_ttl(self):
        """Shorten any pre-existing lease that exceeds the fixed 15-hour term."""
        rows = await self.db.all(
            "SELECT id,leased_at,lease_until FROM accounts WHERE status='leased' AND leased_at IS NOT NULL AND lease_until IS NOT NULL"
        )
        for row in rows:
            try:
                cap = datetime.fromisoformat(row["leased_at"]) + timedelta(minutes=self.ttl_min)
                current = datetime.fromisoformat(row["lease_until"])
            except (TypeError, ValueError):
                continue
            if current > cap:
                await self.db.run("UPDATE accounts SET lease_until=? WHERE id=? AND status='leased'",
                                  (_stamp(cap), row["id"]))

    async def sweep_expired(self):
        now = _stamp(utcnow())
        async def op(conn):
            cur = await conn.execute(
                "SELECT id FROM accounts WHERE status='leased' AND (lease_until IS NULL OR lease_until<=?)", (now,))
            expired = [row["id"] for row in await cur.fetchall()]
            await conn.execute(
                """UPDATE accounts SET status='free',leased_by=NULL,lease_token=NULL,
                   leased_at=NULL,lease_until=NULL,session_lost=0 WHERE status='leased'
                   AND (lease_until IS NULL OR lease_until<=?)""", (now,))
            return expired
        expired_ids = await self.db.transaction(op)
        for acc_id in expired_ids:
            await self.pool.disconnect(acc_id)
            await self._event(acc_id, None, "release", "Автоматическое освобождение по TTL")

    async def verify_session(self, acc_id) -> bool:
        try:
            client = await self.pool.get(acc_id)
            return bool(await client.get_me())
        except Exception:
            return False

    async def check_leased_sessions(self):
        """Пока идёт окно закрепления — убеждаемся, что серверную сессию не скинули."""
        if not self.lock_min:
            return
        rows = await self.db.all(
            "SELECT id,leased_by,leased_at,session_lost FROM accounts WHERE status='leased'")
        for row in rows:
            if self.lock_left(row["leased_at"]) <= 0:
                continue
            if await self.verify_session(row["id"]):
                await self.db.run("UPDATE accounts SET last_check_at=?,check_failures=0,session_lost=0 WHERE id=?",
                                  (_stamp(utcnow()), row["id"]))
                continue
            await self.pool.disconnect(row["id"])
            await self.db.run("UPDATE accounts SET session_lost=1,last_check_at=? WHERE id=?",
                              (_stamp(utcnow()), row["id"]))
            if row["session_lost"]:
                continue
            await self._event(row["id"], row["leased_by"], "session_lost", "Серверная сессия не отвечает")
            if self.notify:
                try:
                    await self.notify(row["id"], row["leased_by"])
                except Exception:
                    log.exception("Не удалось уведомить о потере сессии аккаунта %s", row["id"])

    async def scheduler(self, stop_event):
        tick = 0
        while not stop_event.is_set():
            try:
                await self.enforce_fixed_ttl()
                await self.sweep_expired()
                if tick % 5 == 0:
                    await self.check_leased_sessions()
            except Exception:
                log.exception("Ошибка обслуживания аренд")
            tick += 1
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue

lease_service = None

def configure(db, client_pool, ttl_min=900, lock_min=LOCK_DEFAULT_MIN, notify=None):
    global lease_service
    lease_service = LeaseService(db, client_pool, ttl_min, lock_min, notify)
    return lease_service
