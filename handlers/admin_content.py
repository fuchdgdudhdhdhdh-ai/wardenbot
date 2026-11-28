"""Управление удалением новостей и офферов в админ-панели."""
import bot as app
from aiogram import F
from aiogram.types import CallbackQuery


_original_admin_keyboard = app.build_admin_keyboard


async def _admin_keyboard_with_content():
    markup = await _original_admin_keyboard()
    rows = [[(button.text, button.callback_data) for button in row]
            for row in markup.inline_keyboard]
    rows.append([("🗑 Удаление новостей и офферов", "content_manage")])
    return app.kb(rows)


app.build_admin_keyboard = _admin_keyboard_with_content


# Обновление заявки и чтение результата выполняются одной атомарной операцией.
async def _settle_payout(self, request_id, admin_id, status, cuts=None):
    if status not in ("paid", "rejected"):
        raise ValueError("Invalid payout status")

    async def op(conn):
        cur = await conn.execute(
            "SELECT * FROM payout_requests WHERE id=? AND status='pending'",
            (request_id,),
        )
        request = await cur.fetchone()
        if request is None:
            return None

        amount = request["amount"]
        if cuts is not None:
            if cuts < 0 or cuts > request["leads"]:
                raise ValueError("Invalid cut count")
            amount = round((request["leads"] - cuts) * request["price"], 1)

        cur = await conn.execute(
            """UPDATE payout_requests
               SET status=?,cuts=?,amount=?,reviewed_at=?,admin_id=?
               WHERE id=? AND status='pending'
               RETURNING *""",
            (status, cuts or 0, amount, app.now(), admin_id, request_id),
        )
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    return await self.transaction(op)


app.DB.settle_payout = _settle_payout


async def _admin_only(c):
    if not await app.require_admin(c):
        await c.answer("Нет доступа", show_alert=True)
        return False
    return True


@app.dp.callback_query(F.data == "content_manage")
async def content_manage(c: CallbackQuery):
    if not await _admin_only(c):
        return
    await app.edit_panel(
        c,
        "Управление публикациями. Выберите, что нужно удалить:",
        reply_markup=app.kb([
            [("🗞 Удалить новость", "content_news")],
            [("📥 Удалить оффер", "content_offers")],
            [("Назад ↩️", "admin_home")],
        ]),
    )
    await c.answer()


@app.dp.callback_query(F.data == "content_news")
async def content_news(c: CallbackQuery):
    if not await _admin_only(c):
        return
    rows = await app.db.all(
        "SELECT id,title,created_at FROM news ORDER BY id DESC LIMIT 90"
    )
    buttons = [
        [(f"#{row['id']} · {row['title'][:42]}", f"content_news_del:{row['id']}")]
        for row in rows
    ]
    buttons.append([("Назад ↩️", "content_manage")])
    await app.edit_panel(
        c,
        "Выберите новость для удаления:" if rows else "Новостей пока нет.",
        reply_markup=app.kb(buttons),
    )
    await c.answer()


@app.dp.callback_query(F.data.startswith("content_news_del:"))
async def content_news_delete_ask(c: CallbackQuery):
    if not await _admin_only(c):
        return
    try:
        news_id = int(c.data.split(":", 1)[1])
    except ValueError:
        return await c.answer("Некорректный номер новости", show_alert=True)
    row = await app.db.one("SELECT id,title FROM news WHERE id=?", (news_id,))
    if not row:
        return await c.answer("Новость не найдена или уже удалена", show_alert=True)
    await app.edit_panel(
        c,
        f"Удалить новость #{news_id} «{row['title']}»?",
        reply_markup=app.kb([
            [("Да, удалить", f"content_news_ok:{news_id}")],
            [("Отмена ↩️", "content_news")],
        ]),
    )
    await c.answer()


@app.dp.callback_query(F.data.startswith("content_news_ok:"))
async def content_news_delete(c: CallbackQuery):
    if not await _admin_only(c):
        return
    try:
        news_id = int(c.data.split(":", 1)[1])
    except ValueError:
        return await c.answer("Некорректный номер новости", show_alert=True)
    row = await app.db.one(
        "DELETE FROM news WHERE id=? RETURNING id,title", (news_id,)
    )
    if row is None:
        return await c.answer("Новость уже удалена", show_alert=True)
    await app.db.audit(c.from_user.id, "news_deleted", news_id, row["title"])
    await app.edit_panel(
        c,
        f"Новость #{news_id} удалена.",
        reply_markup=app.kb([
            [("К новостям", "content_news")],
            [("В админ-панель", "admin_home")],
        ]),
    )
    await c.answer("Новость удалена")


@app.dp.callback_query(F.data == "content_offers")
async def content_offers(c: CallbackQuery):
    if not await _admin_only(c):
        return
    rows = await app.db.all(
        "SELECT id,title,active FROM offers WHERE active=1 ORDER BY id DESC LIMIT 90"
    )
    buttons = [
        [(
            f"#{row['id']} · {row['title'][:38]} "
            f"{'🟢' if row['active'] else '⚪'}",
            f"content_offer_del:{row['id']}",
        )]
        for row in rows
    ]
    buttons.append([("Назад ↩️", "content_manage")])
    await app.edit_panel(
        c,
        "Выберите оффер для удаления:" if rows else "Офферов пока нет.",
        reply_markup=app.kb(buttons),
    )
    await c.answer()


@app.dp.callback_query(F.data.startswith("content_offer_del:"))
async def content_offer_delete_ask(c: CallbackQuery):
    if not await _admin_only(c):
        return
    try:
        offer_id = int(c.data.split(":", 1)[1])
    except ValueError:
        return await c.answer("Некорректный номер оффера", show_alert=True)
    row = await app.db.one("SELECT id,title FROM offers WHERE id=?", (offer_id,))
    if not row:
        return await c.answer("Оффер не найден или уже удалён", show_alert=True)
    links = await app.db.one(
        "SELECT COUNT(*) AS n FROM links WHERE offer_id=?", (offer_id,)
    )
    await app.edit_panel(
        c,
        f"Удалить оффер #{offer_id} «{row['title']}»?\n"
        f"Ссылок в оффере: {links['n']}. Свободные будут удалены, выданные отключены. "
        "Текущие ссылки участников будут сброшены, заработок сохранится.",
        reply_markup=app.kb([
            [("Да, удалить оффер", f"content_offer_ok:{offer_id}")],
            [("Отмена ↩️", "content_offers")],
        ]),
    )
    await c.answer()


@app.dp.callback_query(F.data.startswith("content_offer_ok:"))
async def content_offer_delete(c: CallbackQuery):
    if not await _admin_only(c):
        return
    try:
        offer_id = int(c.data.split(":", 1)[1])
    except ValueError:
        return await c.answer("Некорректный номер оффера", show_alert=True)

    async def op(conn):
        cur = await conn.execute(
            "SELECT id,title FROM offers WHERE id=?", (offer_id,)
        )
        offer = await cur.fetchone()
        if offer is None:
            return None
        await conn.execute(
            "UPDATE users SET current_link_id=NULL,current_offer_id=NULL "
            "WHERE current_offer_id=?",
            (offer_id,),
        )
        # Выданные ссылки и оффер остаются скрытыми: по ним считается заработок участников.
        await conn.execute(
            "DELETE FROM links WHERE offer_id=? AND assigned_user IS NULL", (offer_id,)
        )
        await conn.execute("UPDATE links SET active=0 WHERE offer_id=?", (offer_id,))
        cur = await conn.execute("SELECT COUNT(*) n FROM links WHERE offer_id=?", (offer_id,))
        if (await cur.fetchone())["n"]:
            await conn.execute("UPDATE offers SET active=0 WHERE id=?", (offer_id,))
        else:
            await conn.execute("DELETE FROM offers WHERE id=?", (offer_id,))
        return dict(offer)

    offer = await app.db.transaction(op)
    if offer is None:
        return await c.answer("Оффер уже удалён", show_alert=True)
    await app.db.audit(c.from_user.id, "offer_deleted", offer_id, offer["title"])
    await app.edit_panel(
        c,
        f"Оффер #{offer_id} удалён.",
        reply_markup=app.kb([
            [("К офферам", "content_offers")],
            [("В админ-панель", "admin_home")],
        ]),
    )
    await c.answer("Оффер удалён")
