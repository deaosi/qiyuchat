from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import patch

from src.db import DB
from src.receiver import EventRouter, EVENT_MSG, EVENT_SESSION_START


class TestSharedDatabaseTransactions(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "callbacks.db")
        self.db = DB(self.path)
        self.addCleanup(self.db.close)

    def test_concurrent_writers_cannot_nest_in_another_threads_transaction(self):
        entered_first, attempt_second, entered_second, release_first = (Event() for _ in range(4))
        failures = []

        def first():
            try:
                with self.db.tx() as conn:
                    conn.execute("INSERT INTO push_event(event_key) VALUES ('first')")
                    entered_first.set()
                    if not release_first.wait(3):
                        raise TimeoutError("first writer was not released")
            except Exception as error:
                failures.append(error)

        def second():
            try:
                if not entered_first.wait(3):
                    raise TimeoutError("first writer did not start")
                attempt_second.set()
                with self.db.tx() as conn:
                    entered_second.set()
                    conn.execute("INSERT INTO push_event(event_key) VALUES ('second')")
            except Exception as error:
                failures.append(error)

        threads = [Thread(target=first), Thread(target=second)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(attempt_second.wait(3))
            self.assertFalse(entered_second.wait(.1))
            with closing(sqlite3.connect(self.path)) as reader:
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM push_event").fetchone()[0], 0)
        finally:
            release_first.set()
            for thread in threads:
                thread.join(timeout=3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertFalse(self.db._conn.in_transaction)
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM push_event").fetchone()[0], 2)

    def test_nested_failure_rolls_back_only_the_inner_work(self):
        with self.db.tx() as conn:
            conn.execute("INSERT INTO push_event(event_key) VALUES ('outer')")
            with self.assertRaisesRegex(RuntimeError, "retry"):
                with self.db.tx() as nested:
                    nested.execute("INSERT INTO push_event(event_key) VALUES ('inner')")
                    raise RuntimeError("retry")
            conn.execute("INSERT INTO push_event(event_key) VALUES ('after')")
        self.assertFalse(self.db._conn.in_transaction)
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual([row[0] for row in reader.execute("SELECT event_key FROM push_event ORDER BY event_key")],
                             ["after", "outer"])

    def test_failed_message_does_not_commit_dedup_until_its_content_is_saved(self):
        router = EventRouter(self.db, verify=False)
        query = {"eventType": [EVENT_SESSION_START], "time": ["1700000000"]}
        body = json.dumps({"uid": "fixture", "sessionId": "session-fixture"}).encode()
        self.assertEqual(router.handle(query, body)[0], 200)
        query["eventType"] = [EVENT_MSG]
        body = json.dumps({"uid": "fixture", "msgId": "message-fixture", "content": "source answer"}).encode()
        with patch.object(self.db, "append_message", side_effect=RuntimeError("write failed")):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(router.handle(query, body)[0], 500)
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM push_event WHERE event_type=?", (EVENT_MSG,)).fetchone()[0], 0)
        self.assertFalse(self.db._conn.in_transaction)
        self.assertEqual(router.handle(query, body)[0], 200)
        self.assertEqual(router.handle(query, body)[0], 200)
        self.assertEqual(len(self.db.get_messages("session-fixture")), 1)

    def test_customer_failure_rolls_back_binding_and_dedup_before_retry(self):
        router = EventRouter(self.db, verify=False)
        body = json.dumps({"uid": "fixture-customer", "msgId": "customer-fixture", "content": "source question"}).encode()
        with patch.object(self.db, "append_message", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                router.ingest_customer(body)
        self.assertFalse(self.db._conn.in_transaction)
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM push_event").fetchone()[0], 0)
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM uid_session").fetchone()[0], 0)
        self.assertEqual(router.ingest_customer(body)[0], 200)
        self.assertEqual(router.ingest_customer(body)[0], 200)
        self.assertEqual(len(self.db.get_messages("live-fixture-customer")), 1)
