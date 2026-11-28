"""Background delivery of Telegram login codes to the current account renter."""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import bot as app

log = logging.getLogger(__name__)
SERVICE_CHAT_ID = 777000
CODE_RE = re.compile(r"(?:login\s+code|код\s+входа|код|code)\D{0,35}(\d{5,6})", re.IGNORECASE)


def _message_text(message):
    return (getattr(message, "message", None) or getattr(message, "text", None) or "").strip()


def _message_date(message):
    value = getattr(message, "date", None)
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _extract_code(message):
    match = CODE_RE.search(_message_text(message))
    return match.group(1) if match else None


async def _messages(client):
    if hasattr(client, "iter_messages"):
        result = await client.get_messages(SERVICE_CHAT_ID, limit=10)
        return list(result) if result else []
    result = []
    async for message in client.get_chat_history(SERVICE_CHAT_ID, limit=10):
        result.append(message)
    return result


async def _cursor(acc_id):
    row = await app.db.one("SELECT value FROM settings WHERE key=?", (f"login_code_cursor:{acc_id}",))
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


async def _save_cursor(acc_id, message_id):
    await app.db.run(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"login_code_cursor:{acc_id}", str(message_id)),
    )


async def _deliver(acc_id, renter_id, code):
    row = await app.db.one(
        "SELECT leased_by,lease_until,status FROM accounts WHERE id=?", (acc_id,)
    )
    if not row or row["status"] != "leased" or row["leased_by"] != renter_id:
        return
    try:
        if not row["lease_until"] or datetime.fromisoformat(row["lease_until"]) <= datetime.now(timezone.utc):
            return
    except (TypeError, ValueError):
        return
    await app.bot.send_message(
        renter_id,
        f"🔐 Код входа для аккаунта #{acc_id}: <code>{code}</code>\n"
        "Используйте его только для входа в этот аккаунт. Не пересылайте код другим людям.",
        parse_mode="HTML",
    )
    log.info("Login code delivered for leased account #%s to renter %s", acc_id, renter_id)


async def _scan_account(acc_id, renter_id, cursors):
    client = await app.client_pool_service.get(acc_id)
    messages = await _messages(client)
    if not messages:
        return
    cursor = cursors.get(acc_id)
    if cursor is None:
        cursor = await _cursor(acc_id)
    for message in sorted(messages, key=lambda item: getattr(item, "id", 0)):
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id <= cursor:
            continue
        code = _extract_code(message)
        sent_at = _message_date(message)
        if code and sent_at and sent_at >= datetime.now(timezone.utc) - timedelta(minutes=3):
            await _deliver(acc_id, renter_id, code)
        cursor = max(cursor, message_id)
        await _save_cursor(acc_id, cursor)
    cursors[acc_id] = cursor


async def monitor_login_codes(stop_event):
    """Poll service chats for active leases; persist cursors to avoid duplicate delivery."""
    cursors = {}
    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc).isoformat()
            rows = await app.db.all(
                "SELECT id,leased_by FROM accounts "
                "WHERE status='leased' AND leased_by IS NOT NULL AND lease_until>?",
                (now,),
            )
            active = {int(row["id"]) for row in rows}
            for row in rows:
                acc_id, renter_id = int(row["id"]), int(row["leased_by"])
                try:
                    await _scan_account(acc_id, renter_id, cursors)
                except Exception as exc:
                    message = str(exc).lower()
                    if "auth key" in message and ("not registered" in message or "unregistered" in message):
                        log.warning("Telegram auth key revoked for monitored account #%s", acc_id)
                        changed = await app.db.one(
                            "UPDATE accounts SET session_lost=1 WHERE id=? AND session_lost=0 RETURNING id",
                            (acc_id,),
                        )
                        try:
                            await app.client_pool_service.disconnect(acc_id)
                        except Exception:
                            log.exception("Failed disconnecting revoked account #%s", acc_id)
                        if changed:
                            try:
                                await app.alert_session_lost(acc_id, renter_id)
                            except Exception:
                                log.exception("Failed notifying about revoked account #%s", acc_id)
                    else:
                        log.warning("Login-code monitor failed for account #%s: %s", acc_id, exc)
            for stale in set(cursors) - active:
                cursors.pop(stale, None)
        except Exception:
            log.exception("Login-code monitor cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass
