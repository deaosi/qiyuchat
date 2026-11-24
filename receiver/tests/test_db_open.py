from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from src.db import DB, _SOURCE_ARCHIVE_DEFINITIONS
from src.push_sync import SCHEMA as PUSH_SCHEMA


class TestDatabaseOpen(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "console.db")
        with closing(DB(self.path)) as db:
            db._conn.executescript(PUSH_SCHEMA)

    def test_opens_existing_database_during_receiver_write(self):
        connect = sqlite3.connect

        def connect_without_waiting(path, **kwargs):
            return connect(path, **{**kwargs, "timeout": 0.1})

        with closing(connect(self.path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO qa_pair (session_id, idx, question) VALUES ('pending', 1, '问句')")
            with patch("src.db.sqlite3.connect", side_effect=connect_without_waiting):
                with closing(DB(self.path)) as reader:
                    self.assertEqual(reader.iter_qa_pairs(), [])
            writer.commit()

    def test_open_repairs_missing_and_obsolete_archive_triggers(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript("""
                DROP TRIGGER push_sync_forget_message_source;
                DROP TRIGGER push_sync_forget_session_source;
                CREATE TRIGGER push_sync_forget_message_source AFTER DELETE ON raw_message
                BEGIN SELECT 1; END;
            """)

        with closing(DB(self.path)) as db:
            definitions = dict(db._conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'push_sync_forget_%'"
            ))
            self.assertEqual(definitions, _SOURCE_ARCHIVE_DEFINITIONS)
