"""Connections close even when initialization fails before yielding."""
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway.drama.store import DramaStore


class StoreCleanupTests(unittest.TestCase):
    def test_pragma_failure_closes_connection_and_preserves_error(self):
        store = object.__new__(DramaStore)
        store.path = Path('unused.sqlite3')
        for failed_statement in ('PRAGMA foreign_keys=ON', 'PRAGMA synchronous=FULL'):
            with self.subTest(statement=failed_statement):
                db = Mock(in_transaction=False)
                error = sqlite3.OperationalError('injected initialization failure')
                db.execute.side_effect = lambda sql: (_ for _ in ()).throw(error) if sql == failed_statement else None
                with patch('DGXGateway.dgx_gateway.drama.store.sqlite3.connect', return_value=db):
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        with store.connect():
                            self.fail('must not yield an uninitialized connection')
                self.assertIs(caught.exception, error)
                db.close.assert_called_once_with()
                db.rollback.assert_not_called()

