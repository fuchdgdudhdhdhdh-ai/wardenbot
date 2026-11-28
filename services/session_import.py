"""Импорт авторизованных сессий Telethon и Pyrogram."""
import asyncio, json, os, shutil, sqlite3, tempfile
from datetime import datetime, timezone
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import AuthKeyUnregisteredError, ApiIdInvalidError, SessionPasswordNeededError
from pyrogram import Client as PyrogramClient
from services.crypto import encrypt
from db import now

class SessionImportError(RuntimeError): pass

class SessionImporter:
    def __init__(self, db, pool, added_by):
        self.db, self.pool, self.added_by = db, pool, added_by

    async def _save(self, session_string, api_id, api_hash, me, phone=None, client_type="telethon", label=None, action="import_session"):
        if not me or not getattr(me, "id", None):
            raise SessionImportError("Telegram не вернул данные пользователя аккаунта.")
        # Старый клиент отключается до замены шифротекста.
        old = await self.db.one("SELECT id FROM accounts WHERE tg_user_id=?", (int(me.id),))
        if old:
            await self.pool.disconnect(old["id"])
        username = getattr(me, "username", None)
        await self.db.run(
            """INSERT INTO accounts(label,api_id,api_hash,session_enc,phone,tg_user_id,tg_username,
                   status,added_by,client_type,created_at) VALUES(?,?,?,?,?,?,?,'free',?,?,?)
               ON CONFLICT(tg_user_id) WHERE tg_user_id IS NOT NULL DO UPDATE SET
                   label=excluded.label,api_id=excluded.api_id,api_hash=excluded.api_hash,
                   session_enc=excluded.session_enc,phone=excluded.phone,tg_username=excluded.tg_username,
                   status='free',leased_by=NULL,lease_token=NULL,leased_at=NULL,lease_until=NULL,
                   session_lost=0,check_failures=0,
                   added_by=excluded.added_by,client_type=excluded.client_type""",
            (label or f"Аккаунт {me.id}", int(api_id), api_hash, encrypt(session_string), phone,
             int(me.id), username, self.added_by, client_type, now()))
        row = await self.db.one("SELECT id FROM accounts WHERE tg_user_id=?", (int(me.id),))
        await self.db.run(
            "INSERT INTO account_events(account_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (row["id"], self.added_by, action, f"Импортирован {client_type}; tg_user_id={me.id}", now()))
        return row["id"]

    async def import_session_file(self, path, label=None):
        path = Path(path)
        if path.stat().st_size > 5 * 1024 * 1024:
            raise SessionImportError("Файл .session превышает 5 МБ.")
        try:
            conn = sqlite3.connect(str(path))
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            conn.close()
        except sqlite3.Error as exc:
            raise SessionImportError("Файл не является корректной SQLite-сессией.") from exc
        if {"server_address", "port"}.issubset(cols):
            return await self._import_telethon_file(path, label)
        if {"api_id", "test_mode", "user_id"}.issubset(cols):
            return await self._import_pyrogram_file(path, label)
        raise SessionImportError("Неизвестный формат .session. Ожидался файл Telethon или Pyrogram.")

    async def _import_telethon_file(self, path, label):
        api_id = int(os.getenv("API_ID") or 0); api_hash = os.getenv("API_HASH", "")
        if not api_id or not api_hash:
            raise SessionImportError("Для импорта Telethon задайте API_ID и API_HASH в .env.")
        client = TelegramClient(str(path), api_id, api_hash)
        try:
            await asyncio.wait_for(client.connect(), 25)
            me = await asyncio.wait_for(client.get_me(), 25)
            if not me:
                raise SessionImportError("Сессия не авторизована или отозвана.")
            session = StringSession.save(client.session)
            return await self._save(session, api_id, api_hash, me, getattr(me, "phone", None), "telethon", label)
        except AuthKeyUnregisteredError as exc:
            raise SessionImportError("Авторизация отозвана: AuthKeyUnregistered.") from exc
        except ApiIdInvalidError as exc:
            raise SessionImportError("API_ID/API_HASH не подходят для этой сессии.") from exc
        except asyncio.TimeoutError as exc:
            raise SessionImportError("Истекло время подключения к Telegram.") from exc
        finally:
            await client.disconnect()

    async def _import_pyrogram_file(self, path, label):
        api_hash = os.getenv("API_HASH", "")
        if not api_hash:
            raise SessionImportError("Для Pyrogram задайте API_HASH в .env.")
        client = PyrogramClient(name=path.stem, workdir=str(path.parent), api_hash=api_hash,
                                in_memory=False, no_updates=True)
        try:
            await asyncio.wait_for(client.connect(), 25)
            me = await asyncio.wait_for(client.get_me(), 25)
            if not me:
                raise SessionImportError("Сессия Pyrogram не авторизована.")
            session = await client.export_session_string()
            api_id = client.api_id or await client.storage.api_id()
            return await self._save(session, api_id, api_hash, me, getattr(me, "phone_number", None), "pyrogram", label)
        except asyncio.TimeoutError as exc:
            raise SessionImportError("Истекло время подключения к Telegram.") from exc
        finally:
            await client.disconnect()

    async def import_json(self, path, label=None):
        path = Path(path)
        if path.stat().st_size > 1024 * 1024:
            raise SessionImportError("JSON-файл превышает 1 МБ.")
        try:
            data = json.loads(path.read_text("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionImportError("Не удалось прочитать JSON.") from exc
        api_id = int(data.get("api_id") or os.getenv("API_ID") or 0)
        api_hash = data.get("api_hash") or os.getenv("API_HASH", "")
        session = data.get("session_string")
        if session:
            if not api_id or not api_hash:
                raise SessionImportError("Для JSON задайте api_id/api_hash или API_ID/API_HASH в .env.")
            return await self._verify_string(session, api_id, api_hash, label, "import_json")
        if all(k in data for k in ("auth_key", "dc_id", "user_id")):
            if not api_id or not api_hash:
                raise SessionImportError("Для дампа auth_key задайте API_ID и API_HASH в .env.")
            try:
                auth_key = data["auth_key"]
                if isinstance(auth_key, str):
                    import base64
                    auth_key = base64.b64decode(auth_key)
                from telethon.crypto import AuthKey
                from telethon.sessions import MemorySession
                mem = MemorySession()
                mem.set_dc(int(data["dc_id"]), data.get("server_address", "149.154.167.51"), int(data.get("port", 443)))
                mem.auth_key = AuthKey(data=auth_key)
                session = StringSession.save(mem)
            except Exception as exc:
                raise SessionImportError("Поля auth_key/dc_id не удалось преобразовать в Telethon StringSession.") from exc
            return await self._verify_string(session, api_id, api_hash, label, "import_json")
        raise SessionImportError("JSON должен содержать session_string либо auth_key, dc_id и user_id.")

    async def _verify_string(self, session, api_id, api_hash, label, action):
        client = TelegramClient(StringSession(session), api_id, api_hash)
        try:
            await asyncio.wait_for(client.connect(), 25)
            me = await asyncio.wait_for(client.get_me(), 25)
            if not me:
                raise SessionImportError("Сессия не авторизована или отозвана.")
            return await self._save(StringSession.save(client.session), api_id, api_hash, me,
                                    getattr(me, "phone", None), "telethon", label, action)
        except AuthKeyUnregisteredError as exc:
            raise SessionImportError("Сессия отозвана Telegram.") from exc
        except ApiIdInvalidError as exc:
            raise SessionImportError("Неверные API_ID/API_HASH.") from exc
        except asyncio.TimeoutError as exc:
            raise SessionImportError("Истекло время подключения к Telegram.") from exc
        finally:
            await client.disconnect()
