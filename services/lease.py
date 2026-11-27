"""Атомарная аренда аккаунтов и контроль срока действия."""
import asyncio, secrets, logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)
class AccountBusy(RuntimeError): pass
class NotYourAccount(RuntimeError): pass
class LeaseExpired(RuntimeError): pass
class AccountLimit(RuntimeError): pass
class AccountUnavailable(RuntimeError): pass

def utcnow():
    return datetime.now(timezone.utc)

def _stamp(dt):
    return dt.isoformat()

class LeaseService:
    def __init__(self, db, client_pool, ttl_min=30):
        self.db, self.pool, self.ttl_min = db, client_pool, ttl_min

    async def _event(self, acc_id, user_id, action, detail=None):
        await self.db.run("INSERT INTO account_events(account_id,user_id,action,detail) VALUES(?,?,?,?)",
                          (acc_id, user_id, action, detail))

    async def take(self, acc_id: int, user_id: int, ttl_min: int = 30) -> str:
        now = utcnow(); until = now + timedelta(minutes=ttl_min)
        token = secrets.token_urlsafe(32)
        async def op(conn):
            cur = await conn.execute("""UPDATE accounts SET status='leased', leased_by=?, lease_token=?,
                leased_at=?, lease_until=? WHERE id=? AND status IN ('free','leased')
                AND (status='free' OR leased_by=? OR lease_until IS NULL OR lease_until<=?)""",
                (user_id, token, _stamp(now), _stamp(until), acc_id, user_id, _stamp(now)))
            return cur.rowcount
        if await self.db.transaction(op) == 0:
            raise AccountBusy("Аккаунт занят другим пользователем.")
        # При повторной выдаче после истечения старой аренды не переиспользуем старое соединение.
        await self.pool.disconnect(acc_id)
        await self._event(acc_id, user_id, "take")
        return token

    async def take_for_class(self, acc_id: int, user_id: int, account_class: str, ttl_min: int = None) -> str:
        """Atomically lease an account while enforcing one active lease per class."""
        if account_class not in ("ordinary", "premium"):
            raise AccountUnavailable("Неизвестный тип аккаунта.")
        now = utcnow()
        until = now + timedelta(minutes=ttl_min or self.ttl_min)
        token = secrets.token_urlsafe(32)

        async def op(conn):
            cur = await conn.execute(
                "SELECT account_class,status,leased_by,lease_until FROM accounts WHERE id=?",
                (acc_id,),
            )
            account = await cur.fetchone()
            if not account or account["account_class"] != account_class or account["status"] in ("disabled", "dead"):
                raise AccountUnavailable("Этот аккаунт недоступен. Обновите список и попробуйте другой.")
            cur = await conn.execute(
                """SELECT id FROM accounts WHERE leased_by=? AND account_class=? AND status='leased'
                   AND id<>? AND lease_until>? LIMIT 1""",
                (user_id, account_class, acc_id, _stamp(now)),
            )
            if await cur.fetchone():
                raise AccountLimit("У вас уже есть активная аренда этого типа аккаунтов.")
            cur = await conn.execute(
                """UPDATE accounts SET status='leased',leased_by=?,lease_token=?,leased_at=?,lease_until=?
                   WHERE id=? AND account_class=? AND status IN ('free','leased')
                   AND (status='free' OR leased_by=? OR lease_until IS NULL OR lease_until<=?)""",
                (user_id, token, _stamp(now), _stamp(until), acc_id, account_class, user_id, _stamp(now)),
            )
            if cur.rowcount != 1:
                raise AccountBusy("Аккаунт занят другим пользователем.")
            return token

        result = await self.db.transaction(op)
        await self.pool.disconnect(acc_id)
        await self._event(acc_id, user_id, "take", account_class)
        return result

    async def guard_lease(self, acc_id, user_id):
        row = await self.db.one("SELECT leased_by,lease_until,status FROM accounts WHERE id=?", (acc_id,))
        if not row or row["leased_by"] != user_id or row["status"] != "leased":
            raise NotYourAccount("Аккаунт занят другим пользователем или аренда отсутствует.")
        try:
            if not row["lease_until"] or datetime.fromisoformat(row["lease_until"]) <= utcnow():
                raise LeaseExpired("Срок аренды истёк. Возьмите аккаунт повторно.")
        except ValueError as exc:
            raise LeaseExpired("Срок аренды некорректен. Обратитесь к администратору.") from exc
        await self.renew(acc_id, user_id)
        return row

    async def renew(self, acc_id, user_id, ttl_min=None):
        now = utcnow(); until = now + timedelta(minutes=ttl_min or self.ttl_min)
        async def op(conn):
            cur = await conn.execute("""UPDATE accounts SET lease_until=? WHERE id=? AND status='leased'
                AND leased_by=? AND lease_until>?""", (_stamp(until), acc_id, user_id, _stamp(now)))
            return cur.rowcount
        if await self.db.transaction(op) == 0:
            raise LeaseExpired("Аккаунт занят или срок аренды истёк.")

    async def release(self, acc_id: int, user_id: int) -> None:
        async def op(conn):
            cur = await conn.execute("""UPDATE accounts SET status='free',leased_by=NULL,lease_token=NULL,
                leased_at=NULL,lease_until=NULL WHERE id=? AND leased_by=?""", (acc_id, user_id))
            return cur.rowcount
        if await self.db.transaction(op) == 0:
            raise NotYourAccount("Это не ваш аккаунт.")
        await self._event(acc_id, user_id, "release")
        await self.pool.disconnect(acc_id)

    async def force_release(self, acc_id, admin_id):
        async def op(conn):
            cur = await conn.execute("""UPDATE accounts SET status='free',leased_by=NULL,lease_token=NULL,
                leased_at=NULL,lease_until=NULL WHERE id=? AND status='leased'""", (acc_id,))
            return cur.rowcount
        if await self.db.transaction(op) == 0:
            raise NotYourAccount("Аккаунт сейчас не арендован.")
        await self._event(acc_id, admin_id, "force_release")
        await self.pool.disconnect(acc_id)

    async def sweep_expired(self):
        now = _stamp(utcnow())
        async def op(conn):
            cur = await conn.execute("SELECT id FROM accounts WHERE status='leased' AND (lease_until IS NULL OR lease_until<=?)", (now,))
            expired = [row[0] for row in await cur.fetchall()]
            await conn.execute("""UPDATE accounts SET status='free',leased_by=NULL,lease_token=NULL,
                leased_at=NULL,lease_until=NULL WHERE status='leased' AND (lease_until IS NULL OR lease_until<=?)""", (now,))
            return expired
        expired_ids = await self.db.transaction(op)
        for acc_id in expired_ids:
            await self.pool.disconnect(acc_id)
            await self._event(acc_id, None, "release", "Автоматическое освобождение по TTL")

    async def scheduler(self, stop_event):
        while not stop_event.is_set():
            try:
                await self.sweep_expired()
            except Exception:
                log.exception("Ошибка очистки просроченных аренд")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue

lease_service = None

def configure(db, client_pool, ttl_min=30):
    global lease_service
    lease_service = LeaseService(db, client_pool, ttl_min)
    return lease_service
