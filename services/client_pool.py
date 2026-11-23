"""Единый пул серверных Telegram-клиентов: один клиент на аккаунт."""
import asyncio
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from pyrogram import Client as PyrogramClient
from services.crypto import decrypt

log = logging.getLogger(__name__)

class ClientPool:
    def __init__(self, db):
        self.db = db
        self._clients = {}
        self._locks = {}

    def _lock(self, acc_id):
        return self._locks.setdefault(int(acc_id), asyncio.Lock())

    async def get(self, acc_id):
        async with self._lock(acc_id):
            row = await self.db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
            if not row:
                raise RuntimeError("Аккаунт не найден.")
            if row["status"] == "disabled":
                raise RuntimeError("Аккаунт отключён администратором.")
            client = self._clients.get(int(acc_id))
            if client is not None:
                connected = getattr(client, "is_connected", False)
                connected = connected() if callable(connected) else connected
                if connected:
                    return client
            if client is not None:
                await self._disconnect_unlocked(acc_id)
            session = decrypt(row["session_enc"])
            try:
                if row.get("client_type", "telethon") == "pyrogram":
                    client = PyrogramClient(
                        name=f"warden_{acc_id}", api_id=int(row["api_id"]),
                        api_hash=row["api_hash"], session_string=session,
                        in_memory=True, no_updates=True,
                    )
                    await asyncio.wait_for(client.connect(), timeout=25)
                else:
                    client = TelegramClient(
                        StringSession(session), int(row["api_id"]), row["api_hash"],
                        timeout=20, connection_retries=2, request_retries=2,
                    )
                    await asyncio.wait_for(client.connect(), timeout=25)
                self._clients[int(acc_id)] = client
                return client
            except Exception as exc:
                try:
                    if client:
                        await client.disconnect()
                except Exception:
                    log.exception("Не удалось закрыть незавершённый клиент аккаунта %s", acc_id)
                if "database is locked" in str(exc).lower():
                    raise RuntimeError("Хранилище сессии занято. Повторите действие через несколько секунд.") from exc
                raise RuntimeError(f"Не удалось подключить Telegram-аккаунт: {exc}") from exc

    async def _disconnect_unlocked(self, acc_id):
        client = self._clients.pop(int(acc_id), None)
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=10)
            except Exception:
                log.exception("Ошибка отключения клиента аккаунта %s", acc_id)

    async def disconnect(self, acc_id):
        async with self._lock(acc_id):
            await self._disconnect_unlocked(acc_id)

    async def disconnect_all(self):
        for acc_id in list(self._clients):
            await self.disconnect(acc_id)

    async def replace(self, acc_id):
        await self.disconnect(acc_id)

    async def shutdown(self):
        await self.disconnect_all()

pool = None

def configure(db):
    global pool
    pool = ClientPool(db)
    return pool
