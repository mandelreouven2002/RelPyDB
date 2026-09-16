"""
Native C mirror for RelPy tables.

This is what makes the C engine an inseparable part of RelPy: a RelPy instance
keeps a single shared C engine (relpy._relpyengine.Database) holding a columnar
mirror of every scalar table (int / float / bool / str, no encryption). Ordinary
single-table aggregates (count/sum/avg/min/max), GROUP BY, and inner equi-joins
run in C automatically, over that shared engine.

The mirror is maintained lazily and cached: a write marks a table dirty, and the
next query that needs C rebuilds just that table (drop + rebuild). When nothing
has changed, queries reuse the built tables with no rebuild cost. The Python
engine remains the source of truth and handles everything C does not; the mirror
is a transparent optimization that returns identical results.
"""

from __future__ import annotations

from typing import Any

from . import _relpyengine

_TYPECODE = {int: "i", float: "f", bool: "b", str: "s"}
_NUMERIC = {int, float}


class CoreMirrorMixin:
    """Mixin (composed first into RelPy) keeping a shared C mirror of scalar tables."""

    def _init_core_mirror(self) -> None:
        self._core_engine = _relpyengine.Database()
        self._core_built: set[str] = set()   # tables currently present in the engine
        self._core_dirty: set[str] = set()   # tables needing a rebuild

    # --- invalidation: any write marks the table dirty ------------------
    def _core_mark_dirty(self, table_name: str | None) -> None:
        if table_name is not None:
            self._core_dirty.add(table_name)

    def create_table(self, table_name: str, *a, **k):
        result = super().create_table(table_name, *a, **k)
        self._core_mark_dirty(table_name)
        return result

    def add_column(self, table_name: str, *a, **k):
        result = super().add_column(table_name, *a, **k)
        self._core_mark_dirty(table_name)
        return result

    def insert(self, table_name: str, *a, **k):
        result = super().insert(table_name, *a, **k)
        self._core_mark_dirty(table_name)
        return result

    def insert_many(self, table_name: str, *a, **k):
        result = super().insert_many(table_name, *a, **k)
        self._core_mark_dirty(table_name)
        return result

    def update(self, table_name: str, *a, **k):
        result = super().update(table_name, *a, **k)
        self._core_mark_dirty(table_name)
        return result

    def delete(self, table_name: str, *a, **k):
        # a delete may cascade to other tables, so invalidate everything.
        result = super().delete(table_name, *a, **k)
        self._core_dirty |= set(self.schema.keys())
        return result

    # --- mirror maintenance --------------------------------------------
    def _core_table_eligible(self, table_name: str) -> bool:
        table_def = self.schema.get(table_name)
        if table_def is None:
            return False
        for column_def in table_def.columns.values():
            if getattr(column_def, "is_encrypted", False):
                return False
            if getattr(column_def, "storage_type", None) not in _TYPECODE:
                return False
        return True

    def _core_sync(self, table_name: str) -> bool:
        """Ensure ``table_name`` is current in the shared C engine. Returns True
        if the table is C-eligible and now available, else False."""
        if not self._core_table_eligible(table_name):
            return False

        needs_build = table_name not in self._core_built or table_name in self._core_dirty
        if not needs_build:
            return True

        if table_name in self._core_built:
            self._core_engine.drop_table(table_name)
            self._core_built.discard(table_name)

        table_def = self.schema[table_name]
        colnames = list(table_def.columns.keys())
        self._core_engine.create_table(table_name)
        for name in colnames:
            self._core_engine.add_column(
                table_name, name, _TYPECODE[table_def.columns[name].storage_type])
        rows = self.data.get(table_name, [])
        self._core_engine.insert_many(
            table_name, [tuple(r.get(c) for c in colnames) for r in rows])

        self._core_built.add(table_name)
        self._core_dirty.discard(table_name)
        return True

    def _core_mirror_for(self, table_name: str):
        """Returns (engine, colnames) for the (cached) C mirror, or None."""
        if not self._core_sync(table_name):
            return None
        return self._core_engine, list(self.schema[table_name].columns.keys())

    def _core_join_engine(self, left_table: str, right_table: str):
        """The shared engine with both tables synced, if both are C-eligible."""
        if not self._core_sync(left_table):
            return None
        if left_table != right_table and not self._core_sync(right_table):
            return None
        return self._core_engine

    def _core_numeric_column(self, table_name: str, column_name: str) -> bool:
        table_def = self.schema.get(table_name)
        if table_def is None or column_name not in table_def.columns:
            return False
        return table_def.columns[column_name].storage_type in _NUMERIC
