"""
SQL backend built on SQLAlchemy Core.

One class, :class:`SQLBackend`, serves every SQL database RelPy supports --
SQLite, PostgreSQL, MySQL/MariaDB, Oracle, Azure SQL, and Amazon RDS/Aurora --
because they are all reached through a SQLAlchemy connection URL and dialect.
That means adding a new SQL database is usually just a new URL and driver, with
no new RelPy code.

The backend mirrors RelPy's in-memory tables into real SQL tables. It never
runs the query engine in SQL; queries still execute in memory (so the library
behaves identically whether or not a backend is attached). The backend's job is
durability, reflection of existing databases, and raw SQL passthrough.
"""

from __future__ import annotations

from typing import Any

from ..exceptions import (
    BackendConnectionError,
    BackendNotAvailableError,
    BackendOperationError,
)
from ..indexes import INTERNAL_ROW_ID
from ..schema_types import ForeignKeyDef
from .base import StorageBackend
from ..typemap import (
    python_type_from_sqlalchemy,
    sqlalchemy_column_type,
)


def _require_sqlalchemy():
    try:
        from .._bootstrap import ensure
        return ensure("sqlalchemy", "SQLAlchemy")
    except ImportError as exc:
        raise BackendNotAvailableError(
            "SQLAlchemy is required for SQL backends. Install it with "
            "`pip install sqlalchemy` and the driver for your database "
            "(e.g. psycopg for PostgreSQL, PyMySQL for MySQL, "
            "oracledb for Oracle, pyodbc for Azure SQL)."
        ) from exc


class SQLBackend(StorageBackend):
    """
    A durable SQL mirror of a RelPy database.

    Args:
        url:
            A SQLAlchemy connection URL, e.g.
            ``"sqlite:///data.db"``,
            ``"postgresql+psycopg://user:pass@host/db"``,
            ``"mysql+pymysql://user:pass@host:3306/db"``,
            ``"oracle+oracledb://user:pass@host:1521/?service_name=FREEPDB1"``,
            ``"mssql+pyodbc://user:pass@host/db?driver=ODBC+Driver+18+for+SQL+Server"``.
        connect_args:
            Optional dict passed straight to the driver.
        echo:
            If True, SQLAlchemy logs every SQL statement (useful for debugging).
        engine_options:
            Any extra keyword arguments forwarded to ``create_engine``.
    """

    is_in_memory = False

    def __init__(
        self,
        url: str,
        *,
        connect_args: dict[str, Any] | None = None,
        echo: bool = False,
        **engine_options: Any,
    ) -> None:
        sa = _require_sqlalchemy()

        self._sa = sa
        self.url = url

        try:
            self.engine = sa.create_engine(
                url,
                echo=echo,
                connect_args=connect_args or {},
                **engine_options,
            )
            self.name = self.engine.dialect.name
        except Exception as exc:
            raise BackendConnectionError(
                f"Could not create a database engine for URL {url!r}: {exc}"
            ) from exc

        # Verify the connection eagerly so configuration errors surface at
        # connect time rather than on the first query.
        try:
            with self.engine.connect() as connection:
                connection.execute(sa.text("SELECT 1"))
        except Exception as exc:
            raise BackendConnectionError(
                f"Could not connect to the database at {url!r}: {exc}"
            ) from exc

        # A fresh MetaData that we populate as tables are materialized.
        self._metadata = sa.MetaData()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        try:
            self.engine.dispose()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Building SQLAlchemy tables from RelPy schema
    # ------------------------------------------------------------------ #

    def _key_like_columns(self, db: Any, table_name: str) -> set[str]:
        """Columns that participate in a PK, FK, or index (need bounded VARCHAR)."""
        table_def = db.schema[table_name]
        key_like: set[str] = set(table_def.primary_key)

        for local_column, fk in table_def.foreign_keys.items():
            key_like.add(local_column)
            if fk.target_table == table_name:
                key_like.add(fk.target_column)

        for index_def in getattr(db, "indexes", {}).values():
            if index_def.table_name == table_name:
                key_like.update(index_def.columns)

        return key_like

    def _build_sa_table(self, db: Any, table_name: str):
        """Create (in metadata) a SQLAlchemy Table mirroring a RelPy table."""
        sa = self._sa
        table_def = db.schema[table_name]
        key_like = self._key_like_columns(db, table_name)

        # Drop any stale definition from our metadata cache first.
        existing = self._metadata.tables.get(table_name)
        if existing is not None:
            self._metadata.remove(existing)

        columns = []
        for column_name, column_def in table_def.columns.items():
            columns.append(
                sa.Column(
                    column_name,
                    sqlalchemy_column_type(
                        column_def, key_like=column_name in key_like
                    ),
                    primary_key=column_name in table_def.primary_key,
                    nullable=column_def.nullable,
                )
            )

        constraints = []
        for local_column, fk in table_def.foreign_keys.items():
            constraints.append(
                sa.ForeignKeyConstraint(
                    [local_column],
                    [f"{fk.target_table}.{fk.target_column}"],
                    name=f"fk_{table_name}_{local_column}",
                )
            )

        return sa.Table(table_name, self._metadata, *columns, *constraints)

    def _public_rows(self, db: Any, table_name: str) -> list[dict[str, Any]]:
        """Clean logical rows for a table (decrypted, no internal id)."""
        rows = []
        for stored_row in db.data.get(table_name, []):
            public = db._stored_row_to_python_dict(
                stored_row, table_name=table_name, decrypt=True
            )
            public.pop(INTERNAL_ROW_ID, None)
            rows.append(public)
        return rows

    # ------------------------------------------------------------------ #
    # Schema + data mirroring
    # ------------------------------------------------------------------ #

    def rebuild_table(self, db: Any, table_name: str) -> None:
        """
        Recreate a table in SQL from RelPy's current schema and rows.

        Drop-if-exists, create, then bulk insert -- all inside one transaction
        so the mirror is never left half-updated.
        """
        sa = self._sa
        sa_table = self._build_sa_table(db, table_name)
        rows = self._public_rows(db, table_name)

        try:
            with self.engine.begin() as connection:
                sa_table.drop(bind=connection, checkfirst=True)
                sa_table.create(bind=connection, checkfirst=False)
                if rows:
                    connection.execute(sa_table.insert(), rows)
        except Exception as exc:
            raise BackendOperationError(
                f"Failed to rebuild table {table_name!r} in the "
                f"{self.name} backend: {exc}"
            ) from exc

    def drop_table(self, table_name: str) -> None:
        sa_table = self._metadata.tables.get(table_name)
        try:
            with self.engine.begin() as connection:
                if sa_table is not None:
                    sa_table.drop(bind=connection, checkfirst=True)
                else:
                    connection.execute(
                        self._sa.text(f"DROP TABLE IF EXISTS {table_name}")
                    )
        except Exception as exc:
            raise BackendOperationError(
                f"Failed to drop table {table_name!r}: {exc}"
            ) from exc

    def insert_rows(self, db: Any, table_name: str, rows: list[dict[str, Any]]) -> None:
        """Append rows to an already-materialized table (the insert hot path)."""
        if not rows:
            return

        sa_table = self._metadata.tables.get(table_name)
        if sa_table is None:
            # Table not materialized yet -- build it (with all current rows).
            self.rebuild_table(db, table_name)
            return

        try:
            with self.engine.begin() as connection:
                connection.execute(sa_table.insert(), rows)
        except Exception as exc:
            raise BackendOperationError(
                f"Failed to insert into {table_name!r} in the "
                f"{self.name} backend: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Raw SQL passthrough
    # ------------------------------------------------------------------ #

    def execute_sql(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        sa = self._sa
        try:
            with self.engine.begin() as connection:
                result = connection.execute(sa.text(sql), params or {})
                if result.returns_rows:
                    return [dict(mapping) for mapping in result.mappings().all()]
                return []
        except Exception as exc:
            raise BackendOperationError(f"SQL execution failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Reflection: load an existing database into RelPy
    # ------------------------------------------------------------------ #

    def reflect_into(self, db: Any) -> None:
        """
        Read an existing database's schema and rows into ``db`` so the whole
        RelPy query API immediately works over real, pre-existing data.
        """
        sa = self._sa
        metadata = sa.MetaData()

        try:
            metadata.reflect(bind=self.engine)
        except Exception as exc:
            raise BackendOperationError(
                f"Failed to reflect schema from the {self.name} backend: {exc}"
            ) from exc

        # Order tables so parents (referenced tables) come before children.
        try:
            ordered_tables = metadata.sorted_tables
        except Exception:
            ordered_tables = list(metadata.tables.values())

        for sa_table in ordered_tables:
            self._reflect_one_schema(db, sa_table)

        # Keep our own metadata cache in sync with what now exists.
        self._metadata = sa.MetaData()
        for sa_table in ordered_tables:
            self._build_sa_table(db, sa_table.name)

        # Load rows for every reflected table.
        for sa_table in ordered_tables:
            self._reflect_one_data(db, sa_table)

        db._rebuild_all_indexes()
        db._refresh_all_primary_key_lookups()

    def _reflect_one_schema(self, db: Any, sa_table) -> None:
        table_name = sa_table.name

        if not db.NAME_PATTERN.match(table_name):
            # Skip tables whose names RelPy cannot represent.
            return

        db.create_table(table_name)

        primary_key_columns = [column.name for column in sa_table.primary_key.columns]
        single_pk = primary_key_columns[0] if len(primary_key_columns) == 1 else None

        for column in sa_table.columns:
            python_type = python_type_from_sqlalchemy(column.type)
            is_single_pk = column.name == single_pk
            db.add_column(
                table_name,
                column.name,
                python_type,
                is_primary_key=is_single_pk,
                nullable=(False if is_single_pk else bool(column.nullable)),
            )

        if len(primary_key_columns) > 1:
            try:
                db.set_primary_key(table_name, primary_key_columns)
            except Exception:
                # Composite PK that RelPy cannot enforce (e.g. duplicate data);
                # leave the table without a declared PK rather than failing.
                pass

        # Restore single-column foreign keys where RelPy can represent them.
        for fk in sa_table.foreign_keys:
            try:
                local_column = fk.parent.name
                target_table = fk.column.table.name
                target_column = fk.column.name
                db.schema[table_name].foreign_keys[local_column] = ForeignKeyDef(
                    local_column=local_column,
                    target_table=target_table,
                    target_column=target_column,
                    on_delete="RESTRICT",
                )
            except Exception:
                continue

    def _reflect_one_data(self, db: Any, sa_table) -> None:
        table_name = sa_table.name
        if table_name not in db.schema:
            return

        sa = self._sa
        column_names = list(db.schema[table_name].columns.keys())

        try:
            with self.engine.connect() as connection:
                result = connection.execute(sa.select(sa_table))
                rows = [dict(mapping) for mapping in result.mappings().all()]
        except Exception as exc:
            raise BackendOperationError(
                f"Failed to read rows from {table_name!r}: {exc}"
            ) from exc

        # Keep only columns RelPy knows about, in schema order.
        clean_rows = [
            {name: row.get(name) for name in column_names} for row in rows
        ]

        db.data[table_name] = clean_rows
        db._ensure_table_row_ids(table_name)
        db._refresh_row_positions(table_name)

        # Advance AutoNumber counters so future inserts don't collide.
        table_def = db.schema[table_name]
        for column_name, column_def in table_def.columns.items():
            if column_def.is_auto_number and clean_rows:
                highest = max(
                    (row.get(column_name) or 0) for row in clean_rows
                )
                table_def.auto_sequences[column_name] = max(
                    table_def.auto_sequences.get(column_name, 0), int(highest)
                )
