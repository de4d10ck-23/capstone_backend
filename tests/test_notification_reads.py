"""Read state must survive separate API workers and never hide database failures."""
import unittest
from datetime import timezone
from types import SimpleNamespace
class HTTPException(Exception):
    """Minimal HTTP boundary double for dependency-free service checks."""
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
from test_push_delivery import load_functions


class SharedDatabase:
    def __init__(self):
        self.rows = {}
        self.fail = False
    def table(self, name):
        assert name == "notification_reads"
        return Query(self)


class Query:
    def __init__(self, db):
        self.db, self.user, self.rows = db, None, None
        self.start, self.end = 0, 499
    def select(self, *_): return self
    def eq(self, field, value):
        assert field == "user_id"
        self.user = value
        return self
    def order(self, *_): return self
    def range(self, start, end):
        self.start, self.end = start, end
        return self
    def upsert(self, rows, on_conflict):
        assert on_conflict == "user_id,notification_id"
        self.rows = rows
        return self
    def execute(self):
        if self.db.fail: raise RuntimeError("Database unavailable")
        if self.rows is not None:
            for row in self.rows:
                self.db.rows[(row["user_id"], row["notification_id"])] = row
            return SimpleNamespace(data=self.rows)
        rows = sorted((r for r in self.db.rows.values() if r["user_id"] == self.user), key=lambda r:r["notification_id"])
        return SimpleNamespace(data=rows[self.start:self.end + 1])


def worker(db):
    return load_functions("services/notification_service.py",
        {"get_user_read_state", "mark_notification_read", "mark_all_notifications_read", "enrich_notifications_with_read_status"},
        get_supabase=lambda: db, HTTPException=HTTPException, timezone=timezone, PAGE_SIZE=500)


class ReadStateTests(unittest.TestCase):
    def test_same_account_across_workers_and_other_account_isolation(self):
        db = SharedDatabase()
        phone, laptop = worker(db), worker(db)
        notifications = [{"id":"one"}, {"id":"two"}]
        phone["mark_notification_read"]("alice", "one")
        laptop_state = laptop["enrich_notifications_with_read_status"](notifications, "alice")
        self.assertEqual([n["is_read"] for n in laptop_state], [True, False])
        self.assertEqual(sum(not n["is_read"] for n in laptop_state), 1)
        phone["mark_all_notifications_read"]("alice", notifications)
        self.assertTrue(all(n["is_read"] for n in laptop["enrich_notifications_with_read_status"](notifications, "alice")))
        self.assertFalse(any(n["is_read"] for n in laptop["enrich_notifications_with_read_status"](notifications, "bob")))
        self.assertFalse(laptop["enrich_notifications_with_read_status"]([{"id":"new"}], "alice")[0]["is_read"])

    def test_database_failure_is_reported(self):
        db = SharedDatabase()
        db.fail = True
        service = worker(db)
        for action, args in [("mark_notification_read", ("alice", "one")), ("get_user_read_state", ("alice",))]:
            with self.subTest(action=action), self.assertRaises(HTTPException) as result:
                service[action](*args)
            self.assertEqual(result.exception.status_code, 503)

    def test_large_mark_all_is_paginated_and_idempotent(self):
        db = SharedDatabase()
        service = worker(db)
        notifications = [{"id":str(i)} for i in range(1100)]
        service["mark_all_notifications_read"]("alice", notifications)
        service["mark_all_notifications_read"]("alice", notifications)
        other_worker = worker(db)
        self.assertEqual(len(other_worker["get_user_read_state"]("alice")[0]), 1100)
        self.assertEqual(len(db.rows), 1100)


if __name__ == "__main__":
    unittest.main()
