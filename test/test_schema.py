import ast
import pathlib
import sqlite3
import unittest

class SchemaTests(unittest.TestCase):
    def test_schema_creates_all_tables_and_indexes(self):
        db_source = pathlib.Path(__file__).parents[1] / "db.py"
        tree = ast.parse(db_source.read_text(encoding="utf-8"))
        schema = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "SCHEMA" for target in node.targets))
        conn = sqlite3.connect(":memory:")
        conn.executescript(schema)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"users", "offers", "links", "teams", "events", "payout_requests",
                    "lead_log", "admins", "news", "settings", "admin_audit", "schema_migrations", "demo_account_slots"}
        self.assertTrue(expected.issubset(tables))
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_lead_log_user_link", indexes)
        self.assertIn("idx_demo_slots_available", indexes)

if __name__ == "__main__":
    unittest.main()
