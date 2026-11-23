import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from db import DB

class DatabaseSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.db = DB(self.tmp.name)
        await self.db.init()
        await self.db.run("INSERT INTO users(id,first_name,joined_at) VALUES(1,'Test','2026-01-01T00:00:00+00:00')")
        self.offer = await self.db.run("INSERT INTO offers(title,min_leads,price,created_at) VALUES('Offer',2,0.8,'2026-01-01T00:00:00+00:00')")
        await self.db.run("INSERT INTO links(offer_id,url) VALUES(?,?)", (self.offer, "https://example.org/unique"))

    async def asyncTearDown(self):
        os.unlink(self.tmp.name)

    async def test_link_cannot_be_assigned_twice(self):
        a, b = await asyncio.gather(self.db.take_link(1, self.offer), self.db.take_link(2, self.offer))
        self.assertEqual(sum(x is not None for x in (a,b)), 1)

    async def test_pending_payout_is_unique_and_leads_are_reserved(self):
        link = await self.db.one("SELECT id FROM links LIMIT 1")
        rid, leads, amount = await self.db.create_payout(1, link["id"], 3, 0.8)
        self.assertEqual(leads, 3)
        self.assertEqual(amount, 2.4)
        with self.assertRaises(ValueError):
            await self.db.create_payout(1, link["id"], 3, 0.8)

    async def test_settlement_is_idempotent(self):
        link = await self.db.one("SELECT id FROM links LIMIT 1")
        rid, _, _ = await self.db.create_payout(1, link["id"], 3, 0.8)
        first, second = await asyncio.gather(
            self.db.settle_payout(rid, 10, "paid"),
            self.db.settle_payout(rid, 11, "paid"),
        )
        self.assertEqual(sum(x is not None for x in (first,second)), 1)

if __name__ == "__main__":
    unittest.main()
