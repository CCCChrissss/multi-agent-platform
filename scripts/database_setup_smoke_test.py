"""Dependency-installed smoke tests for scripts.database_setup; no real DB."""
from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import Mock

from scripts.database_setup import DatabaseTarget, ensure_database, ensure_vector, parse_target


def _connection(*fetchone_results):
    cursors = []
    for result in fetchone_results:
        cursor = Mock()
        cursor.fetchone.return_value = result
        cursors.append(cursor)
    conn = Mock()
    conn.execute.side_effect = cursors
    context = Mock()
    context.__enter__ = Mock(return_value=conn)
    context.__exit__ = Mock(return_value=False)
    return conn, Mock(return_value=context)


class DatabaseSetupTests(unittest.TestCase):
    def test_parse_target_preserves_connection_but_public_fields_hide_password(self):
        target = parse_target("postgresql://alice:secret@db.example:5433/demo")
        self.assertEqual((target.host, target.port, target.database), ("db.example", "5433", "demo"))
        self.assertIn("dbname=postgres", target.admin_conninfo)
        self.assertIn("password=secret", target.conninfo)
        public = f"host={target.host} port={target.port} database={target.database}"
        self.assertNotIn("secret", public)

    def test_check_does_not_create_a_missing_database(self):
        conn, connect = _connection(None)
        target = DatabaseTarget("target", "admin", "localhost", "5432", "demo")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(ensure_database(target, initialize=False, connect=connect))
        self.assertEqual(conn.execute.call_count, 1)

    def test_init_creates_a_missing_database_with_quoted_identifier(self):
        conn, connect = _connection(None, None)
        target = DatabaseTarget("target", "admin", "localhost", "5432", 'demo"quoted')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(ensure_database(target, initialize=True, connect=connect))
        create_query = conn.execute.call_args_list[1].args[0]
        self.assertEqual(create_query.as_string(), 'CREATE DATABASE "demo""quoted"')

    def test_vector_check_is_read_only_when_extension_is_not_enabled(self):
        conn, connect = _connection(("0.8.6",), None)
        target = DatabaseTarget("target", "admin", "localhost", "5432", "demo")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(ensure_vector(target, initialize=False, connect=connect))
        self.assertEqual(conn.execute.call_count, 2)

    def test_vector_init_is_idempotent_when_already_enabled(self):
        conn, connect = _connection(("0.8.6",), ("0.8.6",))
        target = DatabaseTarget("target", "admin", "localhost", "5432", "demo")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(ensure_vector(target, initialize=True, connect=connect))
        self.assertEqual(conn.execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
