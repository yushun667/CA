"""Database persistence and query API.

Contract (see ``ca.core.interfaces``)::

    SqliteStore(db_path)  # implements RecordStore

Writes are batched in transactions; ``finalize()`` builds indexes and runs
ANALYZE, inserts placeholder rows for unresolved callees and folds the WAL.
Query methods return dicts with stable keys documented in
:mod:`ca.store.sqlite_store`; ``iter_records()`` streams persisted rows for
export. Phase 3 adds candidate call edges (``ca.store.graph`` graph traversals)
and multi-repo workspace/project/variant metadata (``ca.store.workspace``).
"""

from ca.store.federated import FederatedStore, discover_databases
from ca.store.sqlite_store import SqliteStore

__all__ = ["FederatedStore", "SqliteStore", "discover_databases"]
