from __future__ import annotations
import datetime as dt
import base64
import json
from dataclasses import dataclass, field
from typing import Any, Callable
import copy
import re
from .queries import Condition
from .queries import OrderSpec
from .exceptions import (
    TableNotFoundError,
    ColumnNotFoundError,
    SchemaError,
    ConstraintError,
    RowNotFoundError,
    ViewError,
    EncryptionError,
)
from .indexes import IndexMixin, IndexDef, INTERNAL_ROW_ID
from .persistence import PersistenceMixin
from .encryption import EncryptionMixin, ENCRYPTED_DISPLAY_VALUE

# =============================================================================
# RelPy
# =============================================================================
"""
RelPy

An open-source Python library for defining and working with relational
information models in memory.

RelPy is not a SQL database and is not intended to replace production database
engines such as PostgreSQL, SQLite, or DuckDB.

Instead, RelPy provides a Python-native way to define tables, columns,
relationships, constraints, and metadata directly inside Python code.

The project is developed gradually in implementation phases. The current
implementation focuses on the schema layer:

- Create tables.
- Add columns.
- Define primary keys.
- Define simple single-column foreign keys.
- Store metadata such as PII and encryption flags.
- Keep schema separate from actual data.

Planned future layers may include:

- insert()
- update()
- delete()
- query()
- views
- validation over actual data
- exports to DDL, Pandas, JSON
- relationship analysis
- privacy and information mapping reports
"""


# =============================================================================
# Sentinel values
# =============================================================================

DEFAULT_NOT_SET = object()
"""
A sentinel object used to distinguish between two different states:

1. No default value was provided.
2. A default value was explicitly provided as None.

This matters because:

    default=None

can be a valid default only if nullable=True.

But if we used None as the Python function parameter default, we could not know
whether the user really provided None, or simply did not provide a default at all.
"""


# =============================================================================
# Special data types
# =============================================================================

class AutoNumber:
    """
    Special column type used to mark an auto-incrementing integer column.

    Important:
    - AutoNumber is not a real runtime value type.
    - Internally, values of AutoNumber columns will be stored as int.
    - AutoNumber columns must be primary keys.
    - AutoNumber columns cannot also be foreign keys.
    """

    pass


# =============================================================================
# Schema definition classes
# =============================================================================

@dataclass
class ColumnDef:
    """
    Metadata definition of a single column.

    This class does not store actual row values.
    It only describes what a column is supposed to be.

    Attributes:
        name:
            Column name.

        data_type:
            The logical type provided by the user.

            Examples:
                int
                str
                float
                bool
                AutoNumber

        storage_type:
            The actual Python type expected in stored rows.

            Example:
                If data_type is AutoNumber, storage_type is int.

        nullable:
            Whether this column may contain None.

            RelPy uses the following default behavior:
            - regular columns are nullable by default
            - primary key columns are never nullable

        default:
            The default value for the column.

            If default is DEFAULT_NOT_SET, then no default was provided.

        is_primary_key:
            Whether this column is part of the primary key.

        is_pii:
            Whether this column contains personally identifiable information.

        is_encrypted:
            Whether this column should be encrypted or hashed in future data
            operations.

            This class only stores the flag.
            Actual encryption is not implemented here.
    """

    name: str
    data_type: type
    storage_type: type
    nullable: bool = True
    default: Any = DEFAULT_NOT_SET
    is_primary_key: bool = False
    is_pii: bool = False
    is_encrypted: bool = False

    @property
    def is_auto_number(self) -> bool:
        """
        Returns True if this column was defined as AutoNumber.
        """

        return self.data_type is AutoNumber

    @property
    def has_default(self) -> bool:
        """
        Returns True if a default value was explicitly provided.

        Notice:
            default=None counts as a real default value,
            but it is valid only if nullable=True.
        """

        return self.default is not DEFAULT_NOT_SET


@dataclass
class ForeignKeyDef:
    """
    Metadata definition of a single-column foreign key.

    Example:
        orders.user_id -> users.id

    Attributes:
        local_column:
            The column in the current table.

        target_table:
            The referenced table.

        target_column:
            The referenced column in the target table.

        on_delete:
            The deletion behavior.

            Supported values:
                RESTRICT
                CASCADE
                SET NULL
    """

    local_column: str
    target_table: str
    target_column: str
    on_delete: str = "RESTRICT"


@dataclass
class TableDef:
    """
    Metadata definition of a table.

    This class stores schema information only.
    Actual row data is stored separately in RelPy.data.

    Attributes:
        name:
            Table name.

        columns:
            Dictionary mapping column names to ColumnDef objects.

        primary_key:
            List of column names that form the primary key.

            A list is used because relational databases can have composite
            primary keys.

        foreign_keys:
            Dictionary mapping local column names to ForeignKeyDef objects.

        auto_sequences:
            Dictionary mapping AutoNumber column names to their current counter.

            This will be used later by insert().
    """

    name: str
    columns: dict[str, ColumnDef] = field(default_factory=dict)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: dict[str, ForeignKeyDef] = field(default_factory=dict)
    auto_sequences: dict[str, int] = field(default_factory=dict)



@dataclass
class ViewDef:
    """
    Represents a logical view.

    A view is a named query.
    It does not store rows permanently.
    Every call to db.view(...) recalculates the query.
    """

    name: str
    query_builder: Callable[[Any], Any]
    description: str | None = None


# =============================================================================
# Main RelPy class
# =============================================================================

class RelPy(IndexMixin, PersistenceMixin, EncryptionMixin):
    """
    A lightweight, strictly typed, in-memory relational data modeling library.

    Current implementation:
    - create_table()
    - add_column()
    - set_primary_key()
    - describe_table()
    - describe_schema()

    This class currently focuses on schema definition and schema integrity.
    It prepares a solid foundation for future data operations.
    """

    VALID_ON_DELETE_RULES = {"CASCADE", "SET NULL", "RESTRICT"}

    # Names are intentionally strict.
    #
    # Valid examples:
    #   users
    #   user_id
    #   orders_2026
    #
    # Invalid examples:
    #   ""
    #   "user id"
    #   "123users"
    #   "users.email"
    #
    # This makes future export easier:
    # - SQL DDL
    # - Mermaid ERD
    # - JSON Schema
    # - Pydantic models
    NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(self, encryption_key: bytes | str | None = None):
        """
        Initializes an empty RelPy model.
        """

        self.schema: dict[str, TableDef] = {}
        self.data: dict[str, list[dict[str, Any]]] = {}
        self.views: dict[str, ViewDef] = {}

        self.indexes: dict[str, IndexDef] = {}
        self._next_row_id: dict[str, int] = {}
        self._row_positions: dict[str, dict[int, int]] = {}
        self._primary_key_lookup: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}

        self._encryption_key = None
        self._fernet = None
        self._blind_index_key = None

        if encryption_key is not None:
            self.set_encryption_key(encryption_key)

    # -------------------------------------------------------------------------
    # Public schema API
    # -------------------------------------------------------------------------

    def create_table(self, table_name: str) -> RelPy:
        """
        Creates a new empty table.

        Args:
            table_name:
                Name of the table to create.

        Returns:
            self, so calls can be chained if desired.

        Raises:
            ValueError:
                If the table name is invalid or already exists.
        """

        self._validate_name(table_name, "Table")

        if table_name in self.views:
            raise ValueError(
                f"Cannot create table '{table_name}'. A view with this name already exists."
            )

        if table_name in self.schema:
            raise ValueError(f"Table '{table_name}' already exists.")

        self.schema[table_name] = TableDef(name=table_name)
        self.data[table_name] = []
        self._initialize_index_storage_for_table(table_name)
        self._primary_key_lookup[table_name] = {}

        return self

    def add_column(
        self,
        table_name: str,
        column_name: str,
        data_type: type,
        is_primary_key: bool = False,
        is_pii: bool = False,
        is_encrypted: bool = False,
        references: str | None = None,
        on_delete: str | None = None,
        nullable: bool | None = None,
        default: Any = DEFAULT_NOT_SET,
    ) -> RelPy:
        """
        Adds a column to an existing table.

        Args:
            table_name:
                Name of the table.

            column_name:
                Name of the new column.

            data_type:
                A Python type, such as int, str, float, bool,
                or the special AutoNumber marker.

            is_primary_key:
                If True, this column becomes the table's primary key.

                This shorthand only supports a single-column primary key.
                For composite keys, use set_primary_key().

            is_pii:
                Marks the column as personally identifiable information.

            is_encrypted:
                Marks the column as requiring encryption/hashing in future
                data operations.

                This method only stores the flag.
                Actual encryption is not implemented here.

            references:
                Optional foreign key reference in the format:

                    "target_table.target_column"

                Example:

                    "users.id"

            on_delete:
                Deletion rule for foreign keys.

                Supported values:
                    "RESTRICT"
                    "CASCADE"
                    "SET NULL"

                May only be used when references is provided.

            nullable:
                Whether the column may contain None.

                If nullable is not provided:
                - regular columns default to nullable=True
                - primary key columns default to nullable=False

                If nullable=True is explicitly provided together with
                is_primary_key=True, RelPy raises an error.

            default:
                Optional default value.

                If provided, it must match the column's storage type.

                Important:
                    DEFAULT_NOT_SET means no default was provided.
                    None is a real default value, but only valid if nullable=True.

        Returns:
            self, so calls can be chained if desired.

        Raises:
            ValueError:
                If table/column/primary-key/foreign-key/default rules are invalid.

            TypeError:
                If types are invalid.
        """

        self._validate_existing_table(table_name)
        self._validate_name(column_name, "Column")

        self._validate_bool(is_primary_key, "is_primary_key")
        self._validate_bool(is_pii, "is_pii")
        self._validate_bool(is_encrypted, "is_encrypted")

        if nullable is not None:
            self._validate_bool(nullable, "nullable")

        table_ref = self.schema[table_name]
        existing_rows = self.data[table_name]

        if column_name in table_ref.columns:
            raise ValueError(
                f"Column '{column_name}' already exists in table '{table_name}'."
            )

        if not isinstance(data_type, type):
            raise TypeError(
                "data_type must be a valid Python type, "
                "for example int, str, float, bool, or AutoNumber."
            )

        if is_encrypted and is_primary_key:
            raise SchemaError("Encrypted columns cannot be primary keys.")

        if is_encrypted and references is not None:
            raise SchemaError("Encrypted columns cannot be foreign keys.")

        # ---------------------------------------------------------------------
        # Resolve nullable behavior
        # ---------------------------------------------------------------------
        #
        # Rules:
        # - Regular columns are nullable by default.
        # - Primary key columns are not nullable by default.
        # - Primary key columns cannot be nullable.
        #

        if nullable is None:
            resolved_nullable = not is_primary_key
        else:
            resolved_nullable = nullable

        if is_primary_key and resolved_nullable:
            raise ValueError("Primary key columns cannot be nullable.")

        # ---------------------------------------------------------------------
        # Resolve logical type vs storage type
        # ---------------------------------------------------------------------

        if data_type is AutoNumber:
            storage_type = int

            if not is_primary_key:
                raise ValueError("AutoNumber columns must be primary keys.")

            if references is not None:
                raise ValueError("AutoNumber columns cannot also be foreign keys.")

            if default is not DEFAULT_NOT_SET:
                raise ValueError(
                    "AutoNumber columns cannot have a manual default value."
                )

            if existing_rows:
                raise ValueError(
                    f"Adding AutoNumber column '{column_name}' to non-empty table "
                    f"'{table_name}' is not currently supported."
                )

            resolved_nullable = False

        else:
            storage_type = data_type

        # ---------------------------------------------------------------------
        # Primary key rules
        # ---------------------------------------------------------------------

        if is_primary_key:
            if table_ref.primary_key:
                raise ValueError(
                    f"Table '{table_name}' already has a primary key: "
                    f"{table_ref.primary_key}. "
                    "Use set_primary_key(..., replace=True) if you really want "
                    "to replace it."
                )

            if existing_rows:
                raise ValueError(
                    f"Adding a new primary key column '{column_name}' to "
                    f"non-empty table '{table_name}' is not currently supported."
                )

        # ---------------------------------------------------------------------
        # Default validation
        # ---------------------------------------------------------------------

        self._validate_default_value(
            column_name=column_name,
            storage_type=storage_type,
            nullable=resolved_nullable,
            default=default,
        )

        # ---------------------------------------------------------------------
        # Adding a new column to a table that already has rows
        # ---------------------------------------------------------------------
        #
        # If the table already contains rows, we need to know what value should
        # be placed in the new column for existing rows.
        #
        # This is allowed only if:
        # - The column is nullable, so existing rows can receive None.
        # - Or a default was provided, so existing rows can receive that default.
        #
        # If the column is non-nullable and has no default, existing rows would
        # immediately become invalid.
        #

        if existing_rows and not resolved_nullable and default is DEFAULT_NOT_SET:
            raise ValueError(
                f"Cannot add non-nullable column '{column_name}' without a "
                f"default to non-empty table '{table_name}'."
            )

        # ---------------------------------------------------------------------
        # Foreign key rules
        # ---------------------------------------------------------------------

        foreign_key_def = None

        if references is not None:
            target_table, target_column = self._parse_reference(references)

            self._validate_foreign_key_target(
                local_table=table_name,
                local_column=column_name,
                local_storage_type=storage_type,
                target_table=target_table,
                target_column=target_column,
            )

            rule = self._normalize_on_delete_rule(on_delete)

            if rule == "SET NULL" and not resolved_nullable:
                raise ValueError(
                    f"Column '{table_name}.{column_name}' cannot use "
                    "ON DELETE SET NULL because the column is not nullable."
                )

            foreign_key_def = ForeignKeyDef(
                local_column=column_name,
                target_table=target_table,
                target_column=target_column,
                on_delete=rule,
            )

            # If this column is being added to existing rows with a default
            # value, and that default is not None, verify that the referenced
            # value already exists in the target table.
            if existing_rows and default is not DEFAULT_NOT_SET and default is not None:
                if not self._foreign_key_value_exists(
                    target_table=target_table,
                    target_column=target_column,
                    value=default,
                ):
                    raise ValueError(
                        f"Default value for foreign key column "
                        f"'{table_name}.{column_name}' does not reference an "
                        f"existing value in '{target_table}.{target_column}'."
                    )

        else:
            if on_delete is not None:
                raise ValueError(
                    f"on_delete cannot be set for column "
                    f"'{table_name}.{column_name}' because it does not reference "
                    "another table."
                )

        # ---------------------------------------------------------------------
        # Save column metadata
        # ---------------------------------------------------------------------

        table_ref.columns[column_name] = ColumnDef(
            name=column_name,
            data_type=data_type,
            storage_type=storage_type,
            nullable=resolved_nullable,
            default=default,
            is_primary_key=is_primary_key,
            is_pii=is_pii,
            is_encrypted=is_encrypted,
        )

        # ---------------------------------------------------------------------
        # Save primary key metadata
        # ---------------------------------------------------------------------

        if is_primary_key:
            table_ref.primary_key = [column_name]
            self._refresh_primary_key_lookup(table_name)

        # ---------------------------------------------------------------------
        # Save AutoNumber sequence metadata
        # ---------------------------------------------------------------------

        if data_type is AutoNumber:
            table_ref.auto_sequences[column_name] = 0

        # ---------------------------------------------------------------------
        # Save foreign key metadata
        # ---------------------------------------------------------------------

        if foreign_key_def is not None:
            table_ref.foreign_keys[column_name] = foreign_key_def

        # ---------------------------------------------------------------------
        # Apply the new column to existing rows, if any
        # ---------------------------------------------------------------------
        #
        # This prepares the data layer for future insert()/alter behavior.
        # Since there is no public insert() yet, this usually does nothing now.
        #

        if existing_rows:
            if default is not DEFAULT_NOT_SET:
                for row in existing_rows:
                    row[column_name] = self._clone_default(default)
            else:
                for row in existing_rows:
                    row[column_name] = None

        return self

    def set_primary_key(
        self,
        table_name: str,
        columns: list[str],
        replace: bool = False,
    ) -> RelPy:
        """
        Sets a primary key using one or more existing columns.

        This method is mainly intended for composite primary keys.

        Important:
            Composite primary keys require all participating columns
            to already be nullable=False.

            RelPy does not silently change nullable=True columns into
            nullable=False columns when setting a composite primary key.

            This keeps schema changes explicit.

        Args:
            table_name:
                Name of the table.

            columns:
                Non-empty list of existing column names.

            replace:
                If False, this method refuses to replace an existing primary key.
                If True, it replaces the existing primary key after safety checks.

        Returns:
            self, so calls can be chained if desired.

        Raises:
            ValueError:
                If the table does not exist, columns are invalid,
                columns are nullable, existing data violates the primary key,
                or replacing the key would break existing foreign key definitions.
        """

        self._validate_existing_table(table_name)
        self._validate_bool(replace, "replace")

        if not isinstance(columns, list) or not columns:
            raise ValueError("columns must be a non-empty list of column names.")

        for column in columns:
            self._validate_name(column, "Primary key column")

        if len(columns) != len(set(columns)):
            raise ValueError("Primary key columns cannot contain duplicates.")

        table_ref = self.schema[table_name]

        for column in columns:
            if column not in table_ref.columns:
                raise ValueError(
                    f"Cannot set primary key. Column '{column}' does not exist "
                    f"in table '{table_name}'."
                )

        if table_ref.primary_key and not replace:
            raise ValueError(
                f"Table '{table_name}' already has a primary key: "
                f"{table_ref.primary_key}. "
                "Use replace=True if you really want to replace it."
            )

        # Composite primary key rule:
        # every participating column must already be nullable=False.
        #
        # We do not silently modify nullable=True columns here.
        for column in columns:
            if table_ref.columns[column].nullable:
                raise ValueError(
                    f"Cannot set primary key on table '{table_name}'. "
                    f"Column '{column}' is nullable. "
                    "Primary key columns must be defined with nullable=False."
                )

        # AutoNumber is designed to be the only primary key column.
        auto_number_columns = [
            column_name
            for column_name, column_def in table_ref.columns.items()
            if column_def.is_auto_number
        ]

        if auto_number_columns and columns != auto_number_columns:
            raise ValueError(
                "AutoNumber columns must remain the only primary key. "
                f"AutoNumber columns found: {auto_number_columns}."
            )

        # If replacing a primary key, make sure existing foreign keys will not
        # become invalid.
        if table_ref.primary_key and replace:
            self._validate_primary_key_replacement(
                target_table=table_name,
                new_primary_key_columns=columns,
            )

        # Existing data must satisfy the primary key rules:
        # - no None values in key columns
        # - no duplicate key combinations
        self._validate_existing_data_for_primary_key(
            table_name=table_name,
            columns=columns,
        )

        # Remove old primary key flags.
        for column_def in table_ref.columns.values():
            column_def.is_primary_key = False

        # Set new primary key flags.
        for column in columns:
            table_ref.columns[column].is_primary_key = True

        table_ref.primary_key = columns
        self._refresh_primary_key_lookup(table_name)

        return self

    def describe_table(self, table_name: str) -> dict[str, Any]:
        """
        Returns a dictionary representation of a table definition.

        This is useful for debugging, testing, and printing the current schema.

        Args:
            table_name:
                Name of the table.

        Returns:
            A dictionary containing table metadata.
        """

        self._validate_existing_table(table_name)

        table_ref = self.schema[table_name]

        return {
            "name": table_ref.name,
            "columns": {
                column_name: {
                    "type": column_def.data_type.__name__,
                    "storage_type": column_def.storage_type.__name__,
                    "nullable": column_def.nullable,
                    "has_default": column_def.has_default,
                    "default": self._describe_default(column_def.default),
                    "is_primary_key": column_def.is_primary_key,
                    "is_pii": column_def.is_pii,
                    "is_encrypted": column_def.is_encrypted,
                    "is_auto_number": column_def.is_auto_number,
                }
                for column_name, column_def in table_ref.columns.items()
            },
            "primary_key": list(table_ref.primary_key),
            "foreign_keys": {
                local_column: {
                    "local_column": foreign_key.local_column,
                    "target_table": foreign_key.target_table,
                    "target_column": foreign_key.target_column,
                    "on_delete": foreign_key.on_delete,
                }
                for local_column, foreign_key in table_ref.foreign_keys.items()
            },
            "auto_sequences": dict(table_ref.auto_sequences),
            "row_count": len(self.data[table_name]),
        }

    def describe_schema(self) -> dict[str, Any]:
        """
        Returns a dictionary representation of the entire schema.

        Returns:
            Dictionary mapping table names to their table descriptions.
        """

        return {
            table_name: self.describe_table(table_name)
            for table_name in self.schema
        }

    def create_view(
            self,
            view_name: str,
            query_builder: Callable[[Any], Any],
            description: str | None = None,
    ) -> "RelPy":
        """
        Creates a logical view.

        A view is a named query.

        Example:
            db.create_view(
                "paid_orders",
                lambda db: (
                    db.query("orders")
                      .where(col("status") == "paid")
                      .select("id", "user_id", "amount")
                )
            )
        """

        self._validate_name(view_name, "View")

        if view_name in self.schema:
            raise ValueError(
                f"Cannot create view '{view_name}'. A table with this name already exists."
            )

        if view_name in self.views:
            raise ValueError(f"View '{view_name}' already exists.")

        if not callable(query_builder):
            raise TypeError("query_builder must be callable.")

        from .queries import Query

        query = query_builder(self)

        if not isinstance(query, Query):
            raise TypeError("query_builder must return a Query object.")

        # Validate that the view can expose columns.
        # This does not permanently store the result.
        query._result_columns()

        self.views[view_name] = ViewDef(
            name=view_name,
            query_builder=query_builder,
            description=description,
        )

        return self

    def query(self, table_name: str):
        """
        Starts a query over a table.
        """

        self._validate_existing_table(table_name)

        from .queries import Query

        return Query(
            db=self,
            table_name=table_name,
        )


    def view(self, view_name: str):
        """
        Returns a Query over a logical view.

        The view query is recalculated every time this method is called.

        Example:
            db.view("paid_orders")
              .where(col("amount") > 200)
              .to_list()
        """

        self._validate_name(view_name, "View")

        if view_name not in self.views:
            raise ValueError(f"View '{view_name}' does not exist.")

        from .queries import Query

        view_def = self.views[view_name]

        base_query = view_def.query_builder(self)

        if not isinstance(base_query, Query):
            raise TypeError(
                f"View '{view_name}' query_builder must return a Query object."
            )

        columns = base_query._result_columns()
        rows = base_query.to_list()

        return Query.from_rows(
            db=self,
            relation_name=view_name,
            rows=rows,
            columns=columns,
        )

    def drop_view(self, view_name: str) -> "RelPy":
        """
        Drops a logical view.
        """

        self._validate_name(view_name, "View")

        if view_name not in self.views:
            raise ValueError(f"View '{view_name}' does not exist.")

        del self.views[view_name]

        return self

    def list_views(self) -> list[str]:
        """
        Returns the names of all defined views.
        """

        return list(self.views.keys())

    def describe_view(self, view_name: str) -> dict[str, Any]:
        """
        Describes a logical view.

        The view is evaluated to calculate its current row count.
        """

        self._validate_name(view_name, "View")

        if view_name not in self.views:
            raise ValueError(f"View '{view_name}' does not exist.")

        view_def = self.views[view_name]
        query = self.view(view_name)

        return {
            "name": view_def.name,
            "description": view_def.description,
            "columns": query._result_columns(),
            "row_count": query.count(),
        }

    def describe_views(self) -> dict[str, dict[str, Any]]:
        """
        Describes all logical views.
        """

        return {
            view_name: self.describe_view(view_name)
            for view_name in self.views
        }

    # -------------------------------------------------------------------------
    # Internal validation helpers
    # -------------------------------------------------------------------------

    def _validate_name(self, name: str, kind: str) -> None:
        """
        Validates table and column names.

        For the current implementation, names are intentionally strict.
        This makes future export to SQL, JSON Schema, Mermaid ERD, and
        Pydantic easier.

        Args:
            name:
                The name to validate.

            kind:
                Human-readable name kind, such as "Table" or "Column".
        """

        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{kind} name must be a non-empty string.")

        if name == INTERNAL_ROW_ID:
            raise SchemaError(
                f"{kind} name '{name}' is reserved for RelPy internal storage."
            )

        if not self.NAME_PATTERN.match(name):
            raise ValueError(
                f"{kind} name '{name}' is invalid. "
                "Use only letters, numbers, and underscores, and do not start "
                "with a number."
            )

    def _validate_bool(self, value: bool, field_name: str) -> None:
        """
        Validates that a given flag is actually a bool.

        This prevents accidental values such as:
        - "yes"
        - 1
        - None

        Note:
            In Python, bool is a subclass of int, so strict checking is useful.
        """

        if type(value) is not bool:
            raise TypeError(f"{field_name} must be a bool.")

    def _validate_existing_table(self, table_name: str) -> None:
        """
        Validates that a table exists.

        Args:
            table_name:
                Name of the table to check.
        """

        self._validate_name(table_name, "Table")

        if table_name not in self.schema:
            raise TableNotFoundError(f"Table '{table_name}' does not exist. Create it first.")

    def _parse_reference(self, references: str) -> tuple[str, str]:
        """
        Parses a foreign key reference string.

        Expected format:
            "target_table.target_column"

        Example:
            "users.id"

        Args:
            references:
                The reference string.

        Returns:
            A tuple:
                (target_table, target_column)
        """

        if not isinstance(references, str) or not references.strip():
            raise ValueError(
                "references must be a non-empty string in the format "
                "'table.column'."
            )

        parts = references.split(".")

        if len(parts) != 2:
            raise ValueError(
                "references must be in the format 'table.column', "
                "for example 'users.id'."
            )

        target_table, target_column = parts

        self._validate_name(target_table, "Referenced table")
        self._validate_name(target_column, "Referenced column")

        return target_table, target_column

    def _normalize_on_delete_rule(self, on_delete: str | None) -> str:
        """
        Normalizes and validates an ON DELETE rule.

        Args:
            on_delete:
                The user-provided deletion rule.

        Returns:
            A normalized uppercase rule.
        """

        if on_delete is None:
            return "RESTRICT"

        if not isinstance(on_delete, str):
            raise TypeError("on_delete must be a string.")

        rule = on_delete.strip().upper()

        if rule not in self.VALID_ON_DELETE_RULES:
            raise ValueError(
                f"on_delete must be one of {sorted(self.VALID_ON_DELETE_RULES)}."
            )

        return rule

    def _validate_default_value(
        self,
        column_name: str,
        storage_type: type,
        nullable: bool,
        default: Any,
    ) -> None:
        """
        Validates a column default value.

        Rules:
        - DEFAULT_NOT_SET means no default was provided, so there is nothing to check.
        - default=None is allowed only when nullable=True.
        - Any non-None default must match the column storage type.

        Args:
            column_name:
                Name of the column.

            storage_type:
                The actual type used to store values.

            nullable:
                Whether the column allows None.

            default:
                The default value or DEFAULT_NOT_SET.
        """

        if default is DEFAULT_NOT_SET:
            return

        if default is None:
            if not nullable:
                raise ValueError(
                    f"Default value for non-nullable column '{column_name}' "
                    "cannot be None."
                )
            return

        if not self._is_value_compatible(default, storage_type):
            raise TypeError(
                f"Default value for column '{column_name}' must be of type "
                f"{storage_type.__name__}, got {type(default).__name__}."
            )

    def _validate_foreign_key_target(
        self,
        local_table: str,
        local_column: str,
        local_storage_type: type,
        target_table: str,
        target_column: str,
    ) -> None:
        """
        Validates that a foreign key points to a valid target.

        Current implementation rule:
        - Foreign keys are single-column only.
        - The target table must exist.
        - The target column must exist.
        - The target column must be the single-column primary key of the target table.
        - The local column storage type must match the target column storage type.

        Args:
            local_table:
                Table containing the foreign key.

            local_column:
                Column containing the foreign key.

            local_storage_type:
                Storage type of the local column.

            target_table:
                Referenced table.

            target_column:
                Referenced column.
        """

        if target_table not in self.schema:
            raise ValueError(
                f"Foreign key error in '{local_table}.{local_column}': "
                f"target table '{target_table}' does not exist."
            )

        target_table_ref = self.schema[target_table]

        if target_column not in target_table_ref.columns:
            raise ValueError(
                f"Foreign key error in '{local_table}.{local_column}': "
                f"target column '{target_table}.{target_column}' does not exist."
            )

        # Avoid pretending to support composite foreign keys before actually
        # implementing them.
        #
        # A single local column can correctly reference only a single-column
        # primary key.
        if target_table_ref.primary_key != [target_column]:
            raise ValueError(
                f"Foreign key error in '{local_table}.{local_column}': "
                f"target column '{target_table}.{target_column}' must be the "
                f"single-column primary key of table '{target_table}'. "
                f"Current primary key: {target_table_ref.primary_key}."
            )

        target_column_def = target_table_ref.columns[target_column]

        if local_storage_type is not target_column_def.storage_type:
            raise TypeError(
                f"Foreign key type mismatch in '{local_table}.{local_column}': "
                f"local column type is {local_storage_type.__name__}, but target "
                f"column '{target_table}.{target_column}' type is "
                f"{target_column_def.storage_type.__name__}."
            )

    def _validate_primary_key_replacement(
        self,
        target_table: str,
        new_primary_key_columns: list[str],
    ) -> None:
        """
        Validates that replacing a primary key will not break existing foreign keys.

        Since the current implementation supports only single-column foreign keys
        that reference a single-column primary key, any existing foreign key to
        this table requires the new primary key to remain exactly the referenced
        column.

        Args:
            target_table:
                The table whose primary key is being replaced.

            new_primary_key_columns:
                The new primary key columns.
        """

        for source_table_name, source_table in self.schema.items():
            for foreign_key in source_table.foreign_keys.values():
                if foreign_key.target_table != target_table:
                    continue

                required_primary_key = [foreign_key.target_column]

                if new_primary_key_columns != required_primary_key:
                    raise ValueError(
                        f"Cannot replace primary key of table '{target_table}' "
                        f"with {new_primary_key_columns} because foreign key "
                        f"'{source_table_name}.{foreign_key.local_column}' "
                        f"references '{target_table}.{foreign_key.target_column}'. "
                        f"With the current single-column foreign key support, "
                        f"the target primary key must remain {required_primary_key}."
                    )

    def _validate_existing_data_for_primary_key(
        self,
        table_name: str,
        columns: list[str],
    ) -> None:
        """
        Validates that existing rows satisfy a proposed primary key.

        Rules:
        - Every key value must be non-None.
        - Every key combination must be unique.

        Args:
            table_name:
                Table to validate.

            columns:
                Proposed primary key columns.
        """

        seen_keys = set()

        for row_index, row in enumerate(self.data[table_name]):
            key = tuple(row.get(column) for column in columns)

            if any(value is None for value in key):
                raise ValueError(
                    f"Cannot set primary key {columns} on table '{table_name}'. "
                    f"Row at index {row_index} contains None in the key: {key}."
                )

            if key in seen_keys:
                raise ValueError(
                    f"Cannot set primary key {columns} on table '{table_name}'. "
                    f"Duplicate key found: {key}."
                )

            seen_keys.add(key)

    def _foreign_key_value_exists(
        self,
        target_table: str,
        target_column: str,
        value: Any,
    ) -> bool:
        """
        Checks whether a value exists in the target side of a foreign key.

        Fast path:
            If the foreign key references the target table's single-column
            primary key, this method uses RelPy's primary-key lookup cache.

        Fallback:
            If the target column is not the primary key, this method scans the
            target table.
        """

        target_primary_key = self.schema[target_table].primary_key

        if target_primary_key == [target_column]:
            lookup = self._primary_key_lookup_for_table(target_table)
            return (value,) in lookup

        for row in self.data[target_table]:
            if row.get(target_column) == value:
                return True

        return False

    def _primary_key_lookup_for_table(
        self,
        table_name: str,
    ) -> dict[tuple[Any, ...], dict[str, Any]]:
        """
        Returns the primary-key lookup cache for a table.

        The cache maps:
            primary-key tuple -> stored row

        It is rebuilt lazily when missing.
        """

        if not hasattr(self, "_primary_key_lookup"):
            self._primary_key_lookup = {}

        if table_name not in self._primary_key_lookup:
            self._refresh_primary_key_lookup(table_name)

        return self._primary_key_lookup[table_name]

    def _refresh_primary_key_lookup(self, table_name: str) -> None:
        """
        Rebuilds the primary-key lookup cache for one table.
        """

        if not hasattr(self, "_primary_key_lookup"):
            self._primary_key_lookup = {}

        primary_key = self.schema[table_name].primary_key

        if not primary_key:
            self._primary_key_lookup[table_name] = {}
            return

        lookup: dict[tuple[Any, ...], dict[str, Any]] = {}

        for row in self.data[table_name]:
            key = self._primary_key_tuple(table_name, row)

            if key is None:
                continue

            lookup[key] = row

        self._primary_key_lookup[table_name] = lookup

    def _refresh_all_primary_key_lookups(self) -> None:
        """
        Rebuilds primary-key lookup caches for all tables.
        """

        if not hasattr(self, "_primary_key_lookup"):
            self._primary_key_lookup = {}

        for table_name in self.schema.keys():
            self._refresh_primary_key_lookup(table_name)

    def _add_row_to_primary_key_lookup(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> None:
        """
        Adds one stored row to the primary-key lookup cache.
        """

        primary_key = self.schema[table_name].primary_key

        if not primary_key:
            return

        lookup = self._primary_key_lookup_for_table(table_name)
        key = self._primary_key_tuple(table_name, row)

        if key is not None:
            lookup[key] = row

    def _is_value_compatible(self, value: Any, expected_type: type) -> bool:
        """
        Checks whether a value matches an expected Python type.

        This is intentionally a bit stricter than isinstance() for bool/int.

        In Python:
            isinstance(True, int) == True

        But for data modeling, bool should usually not be accepted as int.

        Args:
            value:
                The value to check.

            expected_type:
                The expected Python type.

        Returns:
            True if compatible, False otherwise.
        """

        if expected_type is int and type(value) is bool:
            return False

        return isinstance(value, expected_type)

    def _clone_default(self, default: Any) -> Any:
        """
        Returns a safe copy of a default value.

        This matters for mutable defaults such as list, dict, and set.

        Example problem:
            If every row received the exact same default list object,
            changing the list in one row would affect other rows.

        Args:
            default:
                Default value to clone.

        Returns:
            A deep copy of the default value.
        """

        return copy.deepcopy(default)

    def _describe_default(self, default: Any) -> Any:
        """
        Converts the internal default representation into something readable.

        Args:
            default:
                Either DEFAULT_NOT_SET or a real default value.

        Returns:
            A readable representation.
        """

        if default is DEFAULT_NOT_SET:
            return "<DEFAULT_NOT_SET>"

        return default

    # -------------------------------------------------------------------------
    # Public export API
    # -------------------------------------------------------------------------

    def to_ddl(
        self,
        dialect: str = "postgresql",
        include_transaction: bool = True,
        drop_existing: bool = False,
        create_if_not_exists: bool = False,
        include_metadata_comments: bool = False,
    ) -> str:
        """
        Converts the RelPy schema into an executable SQL DDL script.

        The goal of this method is to produce SQL that can be copied and
        executed directly in a SQL client.

        Currently supported dialect:
            - PostgreSQL

        Args:
            dialect:
                SQL dialect to generate.
                Currently supports "postgresql" and "postgres".

            include_transaction:
                If True, wraps the generated script with:

                    BEGIN;
                    ...
                    COMMIT;

            drop_existing:
                If True, adds DROP TABLE IF EXISTS statements before creating
                the tables.

                Drops are generated in reverse table creation order and use
                CASCADE, so dependent foreign keys will not block the drop.

            create_if_not_exists:
                If True, generates:

                    CREATE TABLE IF NOT EXISTS ...

                Otherwise generates:

                    CREATE TABLE ...

            include_metadata_comments:
                If True, exports RelPy metadata such as PII and encryption flags
                as PostgreSQL COMMENT statements.

                Default is False because the main goal is executable structural DDL.

        Returns:
            A complete SQL DDL script as a string.
        """

        normalized_dialect = self._normalize_ddl_dialect(dialect)

        if not self.schema:
            return "-- RelPy schema is empty. No DDL generated."

        statements: list[str] = []

        if include_transaction:
            statements.append("BEGIN;")

        if drop_existing:
            statements.extend(
                self._drop_tables_to_ddl(dialect=normalized_dialect)
            )

        # Create all tables first, without foreign keys.
        # Foreign keys are added later with ALTER TABLE statements.
        for table_name in self.schema:
            statements.append(
                self.table_to_ddl(
                    table_name=table_name,
                    dialect=normalized_dialect,
                    create_if_not_exists=create_if_not_exists,
                )
            )

        # Add foreign keys only after all tables exist.
        statements.extend(
            self._foreign_keys_to_ddl(dialect=normalized_dialect)
        )

        if include_metadata_comments:
            statements.extend(
                self._metadata_comments_to_ddl(dialect=normalized_dialect)
            )

        if include_transaction:
            statements.append("COMMIT;")

        return "\n\n".join(statements)

    def table_to_ddl(
        self,
        table_name: str,
        dialect: str = "postgresql",
        create_if_not_exists: bool = False,
    ) -> str:
        """
        Converts a single table definition into an executable CREATE TABLE statement.

        Foreign keys are intentionally not included here.
        They are generated later as ALTER TABLE statements by to_ddl().

        Args:
            table_name:
                Name of the table to export.

            dialect:
                SQL dialect.
                Currently supports PostgreSQL only.

            create_if_not_exists:
                If True, generates CREATE TABLE IF NOT EXISTS.

        Returns:
            A CREATE TABLE SQL statement ending with a semicolon.
        """

        self._validate_existing_table(table_name)
        normalized_dialect = self._normalize_ddl_dialect(dialect)

        table_ref = self.schema[table_name]

        if not table_ref.columns:
            raise ValueError(
                f"Cannot generate DDL for table '{table_name}' because it has no columns."
            )

        lines: list[str] = []

        # Column definitions.
        for column_def in table_ref.columns.values():
            lines.append(
                "    " + self._column_to_ddl(
                    column_def=column_def,
                    dialect=normalized_dialect,
                )
            )

        # Primary key constraint.
        if table_ref.primary_key:
            pk_name = self._primary_key_constraint_name(table_name)
            pk_columns = ", ".join(
                self._quote_identifier(column_name)
                for column_name in table_ref.primary_key
            )

            lines.append(
                f"    CONSTRAINT {self._quote_identifier(pk_name)} "
                f"PRIMARY KEY ({pk_columns})"
            )

        create_clause = "CREATE TABLE"

        if create_if_not_exists:
            create_clause += " IF NOT EXISTS"

        return (
            f"{create_clause} {self._quote_identifier(table_name)} (\n"
            + ",\n".join(lines)
            + "\n);"
        )

    # -------------------------------------------------------------------------
    # Internal DDL helpers
    # -------------------------------------------------------------------------

    def _normalize_ddl_dialect(self, dialect: str) -> str:
        """
        Normalizes a user-provided SQL dialect name.

        Currently supported:
            - postgresql
            - postgres
        """

        if not isinstance(dialect, str) or not dialect.strip():
            raise ValueError("dialect must be a non-empty string.")

        normalized = dialect.strip().lower()

        aliases = {
            "postgres": "postgresql",
            "postgresql": "postgresql",
        }

        if normalized not in aliases:
            raise ValueError(
                f"Unsupported DDL dialect '{dialect}'. "
                "Currently supported dialect: postgresql."
            )

        return aliases[normalized]

    def _quote_identifier(self, identifier: str) -> str:
        """
        Quotes a PostgreSQL identifier.

        Even though RelPy validates names strictly, quoting is still safer
        because a valid identifier may still be a reserved SQL keyword.
        """

        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    def _drop_tables_to_ddl(self, dialect: str) -> list[str]:
        """
        Generates DROP TABLE IF EXISTS statements.

        Tables are dropped in reverse creation order.
        CASCADE is used because foreign keys may exist between tables.
        """

        if dialect != "postgresql":
            raise ValueError(f"Unsupported DDL dialect '{dialect}'.")

        statements: list[str] = []

        for table_name in reversed(list(self.schema.keys())):
            statements.append(
                f"DROP TABLE IF EXISTS {self._quote_identifier(table_name)} CASCADE;"
            )

        return statements

    def _column_to_ddl(
        self,
        column_def: ColumnDef,
        dialect: str,
    ) -> str:
        """
        Converts a ColumnDef object into a PostgreSQL column definition.
        """

        column_parts = [
            self._quote_identifier(column_def.name),
            self._python_type_to_sql_type(column_def, dialect),
        ]

        if not column_def.nullable:
            column_parts.append("NOT NULL")

        default_clause = self._default_to_ddl(column_def, dialect)

        if default_clause:
            column_parts.append(default_clause)

        return " ".join(column_parts)

    def _python_type_to_sql_type(
        self,
        column_def: ColumnDef,
        dialect: str,
    ) -> str:
        """
        Maps RelPy/Python types to PostgreSQL types.
        """

        if dialect != "postgresql":
            raise ValueError(f"Unsupported DDL dialect '{dialect}'.")

        if column_def.is_auto_number:
            return "INTEGER GENERATED BY DEFAULT AS IDENTITY"

        type_mapping = {
            int: "INTEGER",
            float: "DOUBLE PRECISION",
            str: "TEXT",
            bool: "BOOLEAN",
            bytes: "BYTEA",
            dict: "JSONB",
            list: "JSONB",

            dt.datetime: "TIMESTAMP",
            dt.date: "DATE",
            dt.time: "TIME",
        }

        if column_def.storage_type not in type_mapping:
            raise TypeError(
                f"Cannot convert Python type "
                f"{column_def.storage_type.__name__} to PostgreSQL DDL type."
            )

        return type_mapping[column_def.storage_type]

    def _default_to_ddl(
        self,
        column_def: ColumnDef,
        dialect: str,
    ) -> str:
        """
        Converts a column default value into a PostgreSQL DEFAULT clause.
        """

        if not column_def.has_default:
            return ""

        sql_literal = self._value_to_sql_literal(
            value=column_def.default,
            dialect=dialect,
            target_storage_type=column_def.storage_type,
        )

        return f"DEFAULT {sql_literal}"

    def _value_to_sql_literal(
        self,
        value: Any,
        dialect: str,
        target_storage_type: type | None = None,
    ) -> str:
        """
        Converts a Python value into a PostgreSQL SQL literal.

        Supports:
            - None
            - str
            - bool
            - int
            - float
            - dict as JSONB
            - list as JSONB
        """

        if dialect != "postgresql":
            raise ValueError(f"Unsupported DDL dialect '{dialect}'.")

        if value is None:
            return "NULL"

        if type(value) is bool:
            return "TRUE" if value else "FALSE"

        if type(value) is int:
            return str(value)

        if type(value) is float:
            return repr(value)

        if isinstance(value, dt.datetime):
            escaped = value.isoformat().replace("'", "''")
            return f"TIMESTAMP '{escaped}'"

        if isinstance(value, dt.date):
            escaped = value.isoformat().replace("'", "''")
            return f"DATE '{escaped}'"

        if isinstance(value, dt.time):
            escaped = value.isoformat().replace("'", "''")
            return f"TIME '{escaped}'"

        if isinstance(value, str):
            escaped = value.replace("'", "''")
            return f"'{escaped}'"

        if isinstance(value, (dict, list)):
            import json

            json_text = json.dumps(value, ensure_ascii=False)
            escaped = json_text.replace("'", "''")

            if target_storage_type in (dict, list):
                return f"'{escaped}'::jsonb"

            return f"'{escaped}'"

        raise TypeError(
            f"Cannot convert default value of type "
            f"{type(value).__name__} to PostgreSQL SQL literal."
        )

    def _primary_key_constraint_name(self, table_name: str) -> str:
        """
        Builds a primary key constraint name.
        """

        return f"pk_{table_name}"

    def _foreign_key_constraint_name(
        self,
        source_table: str,
        foreign_key: ForeignKeyDef,
    ) -> str:
        """
        Builds a foreign key constraint name.

        Example:
            fk_orders_user_id__users_id
        """

        return (
            f"fk_{source_table}_{foreign_key.local_column}"
            f"__{foreign_key.target_table}_{foreign_key.target_column}"
        )

    def _foreign_keys_to_ddl(
        self,
        dialect: str,
    ) -> list[str]:
        """
        Converts all foreign keys into executable ALTER TABLE statements.
        """

        if dialect != "postgresql":
            raise ValueError(f"Unsupported DDL dialect '{dialect}'.")

        statements: list[str] = []

        for source_table_name, table_ref in self.schema.items():
            for foreign_key in table_ref.foreign_keys.values():
                statements.append(
                    self._foreign_key_constraint_to_ddl(
                        source_table=source_table_name,
                        foreign_key=foreign_key,
                        dialect=dialect,
                    )
                )

        return statements

    def _foreign_key_constraint_to_ddl(
        self,
        source_table: str,
        foreign_key: ForeignKeyDef,
        dialect: str,
    ) -> str:
        """
        Converts a ForeignKeyDef into an executable ALTER TABLE statement.
        """

        if dialect != "postgresql":
            raise ValueError(f"Unsupported DDL dialect '{dialect}'.")

        constraint_name = self._foreign_key_constraint_name(
            source_table=source_table,
            foreign_key=foreign_key,
        )

        return (
            f"ALTER TABLE {self._quote_identifier(source_table)}\n"
            f"ADD CONSTRAINT {self._quote_identifier(constraint_name)}\n"
            f"FOREIGN KEY ({self._quote_identifier(foreign_key.local_column)})\n"
            f"REFERENCES {self._quote_identifier(foreign_key.target_table)} "
            f"({self._quote_identifier(foreign_key.target_column)})\n"
            f"ON DELETE {foreign_key.on_delete};"
        )

    def _metadata_comments_to_ddl(
        self,
        dialect: str,
    ) -> list[str]:
        """
        Converts RelPy metadata into PostgreSQL COMMENT statements.

        These statements are executable in PostgreSQL, but they are optional
        because they are not part of the core relational structure.
        """

        if dialect != "postgresql":
            raise ValueError(f"Unsupported DDL dialect '{dialect}'.")

        statements: list[str] = []

        for table_name, table_ref in self.schema.items():
            for column_name, column_def in table_ref.columns.items():
                metadata: list[str] = []

                if column_def.is_pii:
                    metadata.append("pii=true")

                if column_def.is_encrypted:
                    metadata.append("encrypted=true")

                if column_def.is_auto_number:
                    metadata.append("relpy_type=AutoNumber")

                if not metadata:
                    continue

                comment_text = "; ".join(metadata)
                comment_literal = self._sql_string_literal(comment_text)

                statements.append(
                    f"COMMENT ON COLUMN "
                    f"{self._quote_identifier(table_name)}."
                    f"{self._quote_identifier(column_name)} "
                    f"IS {comment_literal};"
                )

        return statements

    def _sql_string_literal(self, text: str) -> str:
        """
        Converts text into a safe PostgreSQL string literal.
        """

        escaped = text.replace("'", "''")
        return f"'{escaped}'"

    # -------------------------------------------------------------------------
    # Public data API
    # -------------------------------------------------------------------------

    def insert(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Inserts a new row into a table.

        Optimized behavior:
        - Validates the plaintext row before storage.
        - Encrypts encrypted columns only after validation.
        - Updates existing indexes incrementally instead of rebuilding all table
          indexes after every insert.
        - Updates the primary-key lookup cache incrementally.

        For loading many rows, prefer insert_many().
        """

        self._validate_existing_table(table_name)
        self._validate_row_dict(row, "row")

        normalized_row, pending_auto_sequences = self._build_insert_row(
            table_name=table_name,
            input_row=row,
        )

        self._validate_complete_row_against_schema(
            table_name=table_name,
            row=normalized_row,
        )

        self._validate_primary_key_for_candidate(
            table_name=table_name,
            candidate_row=normalized_row,
        )

        self._validate_foreign_keys_for_row(
            table_name=table_name,
            row=normalized_row,
        )

        storage_row = self._encrypt_row_for_storage(
            table_name=table_name,
            row=normalized_row,
        )

        old_next_row_id = self._next_row_id[table_name]
        old_auto_sequences = dict(self.schema[table_name].auto_sequences)
        row_was_appended = False

        try:
            for column_name, sequence_value in pending_auto_sequences.items():
                self.schema[table_name].auto_sequences[column_name] = sequence_value

            self._attach_row_id(table_name, storage_row)
            self.data[table_name].append(storage_row)
            row_was_appended = True

            row_id = storage_row[INTERNAL_ROW_ID]
            self._row_positions[table_name][row_id] = len(self.data[table_name]) - 1

            self._add_row_to_table_indexes(table_name, storage_row)
            self._add_row_to_primary_key_lookup(table_name, storage_row)

        except Exception:
            if row_was_appended:
                self.data[table_name].pop()

            self._next_row_id[table_name] = old_next_row_id
            self.schema[table_name].auto_sequences = old_auto_sequences
            self._refresh_row_positions(table_name)
            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)
            raise

        return self._stored_row_to_python_dict(
            storage_row,
            table_name=table_name,
            decrypt=True,
        )

    def insert_many(
        self,
        table_name: str,
        rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        """
        Inserts many rows into a table in one batch.

        This is much faster than calling insert() in a loop because it:
        - validates all rows first,
        - commits AutoNumber sequences once,
        - appends all rows in one batch,
        - rebuilds indexes once,
        - refreshes row positions once,
        - refreshes the primary-key lookup once.

        Example:
            db.insert_many("registrations", registration_rows)
        """

        self._validate_existing_table(table_name)

        if not isinstance(rows, (list, tuple)):
            raise TypeError("rows must be a list or tuple of dictionaries.")

        if not rows:
            return []

        for index, row in enumerate(rows):
            self._validate_row_dict(row, f"rows[{index}]")

        table_ref = self.schema[table_name]
        working_auto_sequences = dict(table_ref.auto_sequences)
        normalized_rows: list[dict[str, Any]] = []

        for input_row in rows:
            self._validate_known_columns(
                table_name=table_name,
                values=input_row,
                context="insert_many",
            )

            complete_row: dict[str, Any] = {}

            for column_name, column_def in table_ref.columns.items():
                if column_name in input_row:
                    value = input_row[column_name]

                    if column_def.is_auto_number and value is not None:
                        if not self._is_value_compatible(value, int):
                            raise TypeError(
                                f"AutoNumber column '{table_name}.{column_name}' "
                                f"must receive an int value, got {type(value).__name__}."
                            )

                        working_auto_sequences[column_name] = max(
                            working_auto_sequences.get(column_name, 0),
                            value,
                        )

                    complete_row[column_name] = self._clone_default(value)
                    continue

                if column_def.is_auto_number:
                    next_value = working_auto_sequences.get(column_name, 0) + 1
                    working_auto_sequences[column_name] = next_value
                    complete_row[column_name] = next_value
                    continue

                if column_def.has_default:
                    complete_row[column_name] = self._clone_default(column_def.default)
                    continue

                if column_def.nullable:
                    complete_row[column_name] = None
                    continue

                raise ValueError(
                    f"Missing required value for non-nullable column "
                    f"'{table_name}.{column_name}'."
                )

            self._validate_complete_row_against_schema(
                table_name=table_name,
                row=complete_row,
            )

            self._validate_foreign_keys_for_row(
                table_name=table_name,
                row=complete_row,
            )

            normalized_rows.append(complete_row)

        self._validate_primary_keys_for_batch(
            table_name=table_name,
            candidate_rows=normalized_rows,
        )

        storage_rows = [
            self._encrypt_row_for_storage(
                table_name=table_name,
                row=row,
            )
            for row in normalized_rows
        ]

        old_rows = list(self.data[table_name])
        old_next_row_id = self._next_row_id[table_name]
        old_auto_sequences = dict(table_ref.auto_sequences)

        try:
            table_ref.auto_sequences = working_auto_sequences

            for storage_row in storage_rows:
                self._attach_row_id(table_name, storage_row)

            self.data[table_name].extend(storage_rows)
            self._refresh_row_positions(table_name)
            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)

        except Exception:
            self.data[table_name] = old_rows
            self._next_row_id[table_name] = old_next_row_id
            table_ref.auto_sequences = old_auto_sequences
            self._refresh_row_positions(table_name)
            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)
            raise

        return [
            self._stored_row_to_python_dict(
                storage_row,
                table_name=table_name,
                decrypt=True,
            )
            for storage_row in storage_rows
        ]

    def update(
        self,
        table_name: str,
        values: dict[str, Any],
        where: Callable[[dict[str, Any]], bool] | None = None,
        allow_all: bool = False,
    ) -> int:
        """
        Updates rows in a table.

        Encrypted columns are decrypted for validation, then encrypted again before
        the updated rows are stored.
        """

        self._validate_existing_table(table_name)
        self._validate_row_dict(values, "values")
        self._validate_where_or_allow_all(where, allow_all, "update")

        if not values:
            raise ValueError("values cannot be empty.")

        self._validate_known_columns(
            table_name=table_name,
            values=values,
            context="update",
        )

        self._validate_update_columns_are_mutable(
            table_name=table_name,
            values=values,
        )

        matching_indices = self._matching_row_indices(
            table_name=table_name,
            where=where,
        )

        if not matching_indices:
            return 0

        updated_rows_by_index: dict[int, dict[str, Any]] = {}

        for row_index in matching_indices:
            current_row = self.data[table_name][row_index]

            candidate_public_row = self._stored_row_to_python_dict(
                current_row,
                table_name=table_name,
                decrypt=True,
            )

            for column_name, value in values.items():
                candidate_public_row[column_name] = self._clone_default(value)

            self._validate_complete_row_against_schema(
                table_name=table_name,
                row=candidate_public_row,
            )

            self._validate_primary_key_for_candidate(
                table_name=table_name,
                candidate_row=candidate_public_row,
                ignore_row_index=row_index,
            )

            self._validate_foreign_keys_for_row(
                table_name=table_name,
                row=candidate_public_row,
            )

            candidate_storage_row = self._encrypt_row_for_storage(
                table_name=table_name,
                row=candidate_public_row,
            )

            candidate_storage_row[INTERNAL_ROW_ID] = current_row[INTERNAL_ROW_ID]
            updated_rows_by_index[row_index] = candidate_storage_row

        old_rows = copy.deepcopy(self.data[table_name])
        old_next_row_id = self._next_row_id.get(table_name)
        old_row_positions = copy.deepcopy(self._row_positions.get(table_name, {}))

        try:
            for row_index, candidate_storage_row in updated_rows_by_index.items():
                self.data[table_name][row_index] = candidate_storage_row

            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)

        except Exception:
            self.data[table_name] = old_rows

            if old_next_row_id is not None:
                self._next_row_id[table_name] = old_next_row_id

            self._row_positions[table_name] = old_row_positions
            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)
            raise

        return len(updated_rows_by_index)

    def delete(
            self,
            table_name: str,
            where: Callable[[dict[str, Any]], bool] | None = None,
            allow_all: bool = False,
    ) -> int:
        """
        Deletes rows from a table.

        Behavior:
        - If other tables reference deleted rows, on_delete rules are applied:
            - RESTRICT: deletion is blocked.
            - CASCADE: referencing rows are also deleted.
            - SET NULL: referencing foreign key values are set to None.
        - Indexes are rebuilt after successful deletion.
        - If index rebuild fails, the deletion is rolled back.

        Safety:
            If where is None, delete() refuses to delete all rows unless
            allow_all=True is explicitly provided.

        Returns:
            Total number of rows deleted, including rows deleted by CASCADE.
        """

        self._validate_existing_table(table_name)
        self._validate_where_or_allow_all(where, allow_all, "delete")

        matching_indices = set(
            self._matching_row_indices(
                table_name=table_name,
                where=where,
            )
        )

        if not matching_indices:
            return 0

        delete_plan: dict[str, set[int]] = {}
        set_null_actions: set[tuple[str, int, str]] = set()

        # Important:
        # This stage only builds the plan.
        # If RESTRICT blocks deletion, it should fail here before any data changes.
        self._collect_delete_plan(
            table_name=table_name,
            row_indices=matching_indices,
            delete_plan=delete_plan,
            set_null_actions=set_null_actions,
        )

        # From here we are about to mutate actual storage.
        # Save full DB state because CASCADE / SET NULL may affect multiple tables.
        old_data = copy.deepcopy(self.data)
        old_next_row_id = copy.deepcopy(self._next_row_id)
        old_row_positions = copy.deepcopy(self._row_positions)

        try:
            # Apply SET NULL actions first, but only for rows that are not also
            # being deleted.
            for source_table, row_index, local_column in set_null_actions:
                if row_index in delete_plan.get(source_table, set()):
                    continue

                self.data[source_table][row_index][local_column] = None

            deleted_count = 0

            # Delete rows from each table.
            # Delete by descending index so list positions remain valid.
            for target_table, row_indices in delete_plan.items():
                for row_index in sorted(row_indices, reverse=True):
                    del self.data[target_table][row_index]
                    deleted_count += 1

            # Rebuild all indexes because delete may affect multiple tables.
            # This also refreshes row_id -> position mappings.
            self._rebuild_all_indexes()
            self._refresh_all_primary_key_lookups()

        except Exception:
            # Rollback all affected data and index storage metadata.
            self.data = old_data
            self._next_row_id = old_next_row_id
            self._row_positions = old_row_positions

            # Restore index maps to match the restored data.
            self._rebuild_all_indexes()
            self._refresh_all_primary_key_lookups()

            raise

        return deleted_count


    # -------------------------------------------------------------------------
    # Internal data helpers
    # -------------------------------------------------------------------------

    def _validate_row_dict(
        self,
        value: Any,
        name: str,
    ) -> None:
        """
        Validates that an input row/update object is a dictionary.

        Args:
            value:
                Value to validate.

            name:
                Human-readable name for error messages.
        """

        if not isinstance(value, dict):
            raise TypeError(f"{name} must be a dictionary.")

        for key in value:
            if not isinstance(key, str):
                raise TypeError(f"All keys in {name} must be strings.")

    def _validate_known_columns(
        self,
        table_name: str,
        values: dict[str, Any],
        context: str,
    ) -> None:
        """
        Validates that all provided columns exist in the table.

        Args:
            table_name:
                Target table.

            values:
                Dictionary of provided column values.

            context:
                Human-readable operation name, such as "insert" or "update".
        """

        table_ref = self.schema[table_name]

        unknown_columns = [
            column_name
            for column_name in values
            if column_name not in table_ref.columns
        ]

        if unknown_columns:
            raise ValueError(
                f"Unknown columns in {context} for table '{table_name}': "
                f"{unknown_columns}."
            )

    def _build_insert_row(
        self,
        table_name: str,
        input_row: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """
        Builds a complete row for insertion.

        This method fills missing values using:
        - AutoNumber generation.
        - Column defaults.
        - None for nullable columns.

        It does not commit AutoNumber sequence changes.
        Instead, it returns pending sequence updates so insert() can commit them
        only after all validation succeeds.

        Args:
            table_name:
                Target table.

            input_row:
                User-provided row.

        Returns:
            A tuple:
                (complete_row, pending_auto_sequences)
        """

        self._validate_known_columns(
            table_name=table_name,
            values=input_row,
            context="insert",
        )

        table_ref = self.schema[table_name]

        complete_row: dict[str, Any] = {}
        pending_auto_sequences: dict[str, int] = {}

        for column_name, column_def in table_ref.columns.items():
            if column_name in input_row:
                value = input_row[column_name]

                # If the user manually provides an AutoNumber value, accept it
                # as long as it is valid, and advance the internal sequence if
                # needed.
                if column_def.is_auto_number and value is not None:
                    if not self._is_value_compatible(value, int):
                        raise TypeError(
                            f"AutoNumber column '{table_name}.{column_name}' "
                            f"must receive an int value, got {type(value).__name__}."
                        )

                    current_sequence = table_ref.auto_sequences.get(column_name, 0)
                    pending_auto_sequences[column_name] = max(
                        current_sequence,
                        value,
                    )

                complete_row[column_name] = self._clone_default(value)
                continue

            if column_def.is_auto_number:
                current_sequence = table_ref.auto_sequences.get(column_name, 0)
                next_value = current_sequence + 1

                complete_row[column_name] = next_value
                pending_auto_sequences[column_name] = next_value
                continue

            if column_def.has_default:
                complete_row[column_name] = self._clone_default(column_def.default)
                continue

            if column_def.nullable:
                complete_row[column_name] = None
                continue

            raise ValueError(
                f"Missing required value for non-nullable column "
                f"'{table_name}.{column_name}'."
            )

        return complete_row, pending_auto_sequences

    def _validate_complete_row_against_schema(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> None:
        """
        Validates a complete row against the table schema.

        A complete row is expected to contain every column in the table.

        Args:
            table_name:
                Target table.

            row:
                Complete row to validate.
        """

        table_ref = self.schema[table_name]

        expected_columns = set(table_ref.columns)
        actual_columns = set(row)

        if expected_columns != actual_columns:
            missing = sorted(expected_columns - actual_columns)
            extra = sorted(actual_columns - expected_columns)

            raise ValueError(
                f"Row for table '{table_name}' does not match the table schema. "
                f"Missing columns: {missing}. Extra columns: {extra}."
            )

        for column_name, column_def in table_ref.columns.items():
            self._validate_value_for_column(
                table_name=table_name,
                column_def=column_def,
                value=row[column_name],
            )

    def _validate_value_for_column(
        self,
        table_name: str,
        column_def: ColumnDef,
        value: Any,
    ) -> None:
        """
        Validates a single value against a column definition.

        Rules:
        - None is allowed only if the column is nullable.
        - Non-None values must match the column storage type.
        """

        if value is None:
            if not column_def.nullable:
                raise ValueError(
                    f"Column '{table_name}.{column_def.name}' cannot be None."
                )
            return

        if not self._is_value_compatible(value, column_def.storage_type):
            raise TypeError(
                f"Column '{table_name}.{column_def.name}' expects "
                f"{column_def.storage_type.__name__}, got {type(value).__name__}."
            )

    def _primary_key_tuple(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> tuple[Any, ...] | None:
        """
        Returns the primary key tuple for a row.

        Args:
            table_name:
                Target table.

            row:
                Row from which to extract the primary key.

        Returns:
            Primary key tuple, or None if the table has no primary key.
        """

        primary_key = self.schema[table_name].primary_key

        if not primary_key:
            return None

        return tuple(row.get(column_name) for column_name in primary_key)

    def _validate_primary_key_for_candidate(
        self,
        table_name: str,
        candidate_row: dict[str, Any],
        ignore_row_index: int | None = None,
    ) -> None:
        """
        Validates that a candidate row does not violate primary key rules.

        Fast path:
            For insert-like operations, this uses the primary-key lookup cache.

        Fallback:
            For update() with ignore_row_index, this scans so the row does not
            conflict with itself.
        """

        key = self._primary_key_tuple(table_name, candidate_row)

        if key is None:
            return

        if any(value is None for value in key):
            raise ValueError(
                f"Primary key for table '{table_name}' cannot contain None. "
                f"Candidate key: {key}."
            )

        if ignore_row_index is None:
            lookup = self._primary_key_lookup_for_table(table_name)

            if key in lookup:
                raise ValueError(
                    f"Duplicate primary key in table '{table_name}': {key}."
                )

            return

        for row_index, existing_row in enumerate(self.data[table_name]):
            if row_index == ignore_row_index:
                continue

            existing_key = self._primary_key_tuple(table_name, existing_row)

            if existing_key == key:
                raise ValueError(
                    f"Duplicate primary key in table '{table_name}': {key}."
                )

    def _validate_primary_keys_for_batch(
        self,
        table_name: str,
        candidate_rows: list[dict[str, Any]],
    ) -> None:
        """
        Validates primary-key uniqueness for a batch before committing it.
        """

        primary_key = self.schema[table_name].primary_key

        if not primary_key:
            return

        existing_lookup = self._primary_key_lookup_for_table(table_name)
        seen_keys: set[tuple[Any, ...]] = set()

        for candidate_row in candidate_rows:
            key = self._primary_key_tuple(table_name, candidate_row)

            if key is None:
                continue

            if any(value is None for value in key):
                raise ValueError(
                    f"Primary key for table '{table_name}' cannot contain None. "
                    f"Candidate key: {key}."
                )

            if key in existing_lookup or key in seen_keys:
                raise ValueError(
                    f"Duplicate primary key in table '{table_name}': {key}."
                )

            seen_keys.add(key)

    def _validate_foreign_keys_for_row(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> None:
        """
        Validates all foreign keys for a row.

        Rules:
        - None is allowed for nullable foreign keys.
        - Non-None foreign key values must exist in the referenced table.
        """

        table_ref = self.schema[table_name]

        for local_column, foreign_key in table_ref.foreign_keys.items():
            value = row[local_column]

            if value is None:
                continue

            if not self._foreign_key_value_exists(
                target_table=foreign_key.target_table,
                target_column=foreign_key.target_column,
                value=value,
            ):
                raise ValueError(
                    f"Foreign key violation in '{table_name}.{local_column}': "
                    f"value {value!r} does not exist in "
                    f"'{foreign_key.target_table}.{foreign_key.target_column}'."
                )

    def _validate_update_columns_are_mutable(
        self,
        table_name: str,
        values: dict[str, Any],
    ) -> None:
        """
        Validates that the requested update does not modify protected columns.

        Current implementation:
        - AutoNumber columns cannot be updated.
        - Primary key columns cannot be updated.

        This keeps update() simple and prevents broken references.
        """

        table_ref = self.schema[table_name]

        for column_name in values:
            column_def = table_ref.columns[column_name]

            if column_def.is_auto_number:
                raise ValueError(
                    f"AutoNumber column '{table_name}.{column_name}' cannot be updated."
                )

            if column_def.is_primary_key:
                raise ValueError(
                    f"Primary key column '{table_name}.{column_name}' cannot be updated "
                    "in the current implementation."
                )

    def _validate_where_or_allow_all(
        self,
        where: Callable[[dict[str, Any]], bool] | None,
        allow_all: bool,
        operation: str,
    ) -> None:
        """
        Validates mass update/delete safety.

        Args:
            where:
                Optional predicate.

            allow_all:
                Whether operating on all rows is explicitly allowed.

            operation:
                Operation name for error messages.
        """

        self._validate_bool(allow_all, "allow_all")

        if where is None and not allow_all:
            raise ValueError(
                f"{operation}() without a where predicate would affect all rows. "
                "Pass allow_all=True if this is intentional."
            )

        if where is not None and not callable(where):
            raise TypeError("where must be callable or None.")

    def _matching_row_indices(
        self,
        table_name: str,
        where: Callable[[dict[str, Any]], bool] | None,
    ) -> list[int]:
        """
        Returns row indices matching a predicate.

        The predicate receives a public/decrypted row so encrypted payloads and
        RelPy internal metadata are never exposed to user callbacks.
        """

        matching_indices: list[int] = []

        for row_index, row in enumerate(self.data[table_name]):
            if where is None:
                matching_indices.append(row_index)
                continue

            public_row = self._stored_row_to_python_dict(
                row,
                table_name=table_name,
                decrypt=True,
            )

            result = where(copy.deepcopy(public_row))

            if type(result) is not bool:
                raise TypeError("where predicate must return a bool for every row.")

            if result:
                matching_indices.append(row_index)

        return matching_indices

    def _collect_delete_plan(
        self,
        table_name: str,
        row_indices: set[int],
        delete_plan: dict[str, set[int]],
        set_null_actions: set[tuple[str, int, str]],
    ) -> None:
        """
        Recursively builds a deletion plan according to foreign key rules.

        Args:
            table_name:
                Table from which rows are being deleted.

            row_indices:
                Indices of rows to delete from table_name.

            delete_plan:
                Mutable dictionary mapping table names to row indices that
                should be deleted.

            set_null_actions:
                Mutable set of actions:
                    (source_table, row_index, local_column)

                These actions are applied before deletion.
        """

        if not row_indices:
            return

        planned_for_table = delete_plan.setdefault(table_name, set())
        new_indices = row_indices - planned_for_table

        if not new_indices:
            return

        planned_for_table.update(new_indices)

        # For every foreign key in the schema, check whether it references
        # rows that are now planned for deletion.
        for source_table_name, source_table in self.schema.items():
            for foreign_key in source_table.foreign_keys.values():
                if foreign_key.target_table != table_name:
                    continue

                target_values = {
                    self.data[table_name][row_index].get(foreign_key.target_column)
                    for row_index in new_indices
                }

                dependent_indices = {
                    row_index
                    for row_index, row in enumerate(self.data[source_table_name])
                    if row.get(foreign_key.local_column) in target_values
                }

                already_planned = delete_plan.get(source_table_name, set())
                dependent_indices = dependent_indices - already_planned

                if not dependent_indices:
                    continue

                if foreign_key.on_delete == "RESTRICT":
                    raise ValueError(
                        f"Cannot delete from '{table_name}' because rows in "
                        f"'{source_table_name}' reference it through foreign key "
                        f"'{source_table_name}.{foreign_key.local_column}' -> "
                        f"'{foreign_key.target_table}.{foreign_key.target_column}' "
                        "with ON DELETE RESTRICT."
                    )

                if foreign_key.on_delete == "CASCADE":
                    self._collect_delete_plan(
                        table_name=source_table_name,
                        row_indices=dependent_indices,
                        delete_plan=delete_plan,
                        set_null_actions=set_null_actions,
                    )
                    continue

                if foreign_key.on_delete == "SET NULL":
                    for dependent_index in dependent_indices:
                        set_null_actions.add(
                            (
                                source_table_name,
                                dependent_index,
                                foreign_key.local_column,
                            )
                        )
                    continue

                raise ValueError(
                    f"Unsupported on_delete rule: {foreign_key.on_delete}."
                )

    # -------------------------------------------------------------------------
    # Public export API - Python ecosystem
    # -------------------------------------------------------------------------

    def to_pandas(
            self,
            table_name: str,
            column_name: str | None = None,
            where_key: dict[str, Any] | None = None,
            *,
            decrypt: bool = False,
    ):
        """
        Exports table data to a pandas DataFrame.

        Behavior:
        - Uses the same export semantics as to_list().
        - Internal RelPy metadata fields are hidden.
        - Encrypted values are masked by default.
        - If decrypt=True, encrypted values are decrypted using the loaded encryption key.

        Examples:
            db.to_pandas("users")
            db.to_pandas("users", decrypt=True)
            db.to_pandas("users", column_name="email")
            db.to_pandas("users", where_key={"id": 1})
        """

        try:
            import pandas as pd
        except ImportError as error:
            raise ImportError(
                "pandas is required for to_pandas(). "
                "Install it with: pip install pandas"
            ) from error

        exported_data = self.to_list(
            table_name=table_name,
            column_name=column_name,
            where_key=where_key,
            decrypt=decrypt,
        )

        if column_name is not None:
            return pd.DataFrame({
                column_name: exported_data,
            })

        return pd.DataFrame(exported_data)

    def to_json(
            self,
            table_name: str,
            column_name: str | None = None,
            where_key: dict[str, Any] | None = None,
            *,
            decrypt: bool = False,
            indent: int | None = 2,
            ensure_ascii: bool = False,
    ) -> str:
        """
        Exports table data as a JSON string.

        Behavior:
        - Uses the same export semantics as to_list().
        - Internal RelPy metadata fields are hidden.
        - Encrypted values are masked by default.
        - If decrypt=True, encrypted values are decrypted using the loaded encryption key.

        Examples:
            db.to_json("users")
            db.to_json("users", decrypt=True)
            db.to_json("users", column_name="email")
            db.to_json("users", where_key={"id": 1}, decrypt=True)
        """

        self._validate_export_selection(
            table_name=table_name,
            column_name=column_name,
            where_key=where_key,
        )

        exported_data = self.to_list(
            table_name=table_name,
            column_name=column_name,
            where_key=where_key,
            decrypt=decrypt,
        )

        return json.dumps(
            exported_data,
            ensure_ascii=ensure_ascii,
            indent=indent,
            default=str,
        )


    def to_list(
        self,
        table_name: str,
        column_name: str | None = None,
        where_key: dict[str, Any] | None = None,
        decrypt: bool = False,
    ) -> list[dict[str, Any]] | list[Any]:
        """
        Exports table data as native Python lists.

        Encrypted values are masked by default. Pass decrypt=True to decrypt them.
        """

        self._validate_export_selection(
            table_name=table_name,
            column_name=column_name,
            where_key=where_key,
        )

        if where_key is not None:
            row = self._row_by_primary_key(table_name, where_key)
            return [
                self._stored_row_to_python_dict(
                    row,
                    table_name=table_name,
                    decrypt=decrypt,
                )
            ]

        if column_name is not None:
            return self._column_to_python_list(
                table_name,
                column_name,
                decrypt=decrypt,
            )

        return self._table_to_python_list(
            table_name,
            decrypt=decrypt,
        )

    # -------------------------------------------------------------------------
    # Internal export helpers
    # -------------------------------------------------------------------------

    def _validate_export_selection(
            self,
            table_name: str,
            column_name: str | None,
            where_key: dict[str, Any] | None,
    ) -> None:
        """
        Validates the target of an export operation.

        Export can target exactly one of:
        - full table
        - one column
        - one row by primary key

        Args:
            table_name:
                Name of the table.

            column_name:
                Optional column name.

            where_key:
                Optional primary key selector.
        """

        self._validate_existing_table(table_name)

        if column_name is not None and where_key is not None:
            raise ValueError(
                "Export target is ambiguous. Use either column_name or where_key, not both."
            )

        if column_name is not None:
            self._validate_existing_column(table_name, column_name)

        if where_key is not None:
            self._validate_where_key(table_name, where_key)

    def _validate_existing_column(
            self,
            table_name: str,
            column_name: str,
    ) -> None:
        """
        Validates that a column exists in a table.

        Args:
            table_name:
                Name of the table.

            column_name:
                Name of the column.
        """

        self._validate_name(column_name, "Column")

        if column_name not in self.schema[table_name].columns:
            raise ColumnNotFoundError(
                f"Column '{column_name}' does not exist in table '{table_name}'."
            )

    def _validate_where_key(
            self,
            table_name: str,
            where_key: dict[str, Any],
    ) -> None:
        """
        Validates a primary key selector.

        Rules:
        - The table must have a primary key.
        - where_key must be a dictionary.
        - where_key must contain exactly the primary key columns.
        - Each value must be valid for its primary key column.

        Args:
            table_name:
                Name of the table.

            where_key:
                Primary key selector.

                Example:
                    {"id": 1}

                Composite key example:
                    {"order_id": 10, "item_id": 3}
        """

        self._validate_row_dict(where_key, "where_key")

        table_ref = self.schema[table_name]

        if not table_ref.primary_key:
            raise ValueError(
                f"Table '{table_name}' has no primary key, so where_key cannot be used."
            )

        expected_columns = set(table_ref.primary_key)
        actual_columns = set(where_key.keys())

        if actual_columns != expected_columns:
            raise ValueError(
                f"where_key for table '{table_name}' must contain exactly "
                f"the primary key columns {table_ref.primary_key}. "
                f"Got: {list(where_key.keys())}."
            )

        for column_name, value in where_key.items():
            column_def = table_ref.columns[column_name]

            self._validate_value_for_column(
                table_name=table_name,
                column_def=column_def,
                value=value,
            )

    def _row_to_python_dict(
        self,
        table_name: str,
        where_key: dict[str, Any],
        *,
        decrypt: bool = False,
    ) -> dict[str, Any]:
        """
        Converts a single RelPy row into a plain Python dictionary.
        """

        row = self._row_by_primary_key(
            table_name=table_name,
            where_key=where_key,
        )

        return self._stored_row_to_python_dict(
            row,
            table_name=table_name,
            decrypt=decrypt,
        )

    def _stored_row_to_python_dict(
            self,
            row: dict[str, Any],
            *,
            table_name: str | None = None,
            decrypt: bool = False,
    ) -> dict[str, Any]:
        """
        Converts a stored internal row into a plain Python dictionary.

        This is the lowest-level row export function.

        Table-level export calls this function for every row.
        Row-level export also calls this function after finding the row
        by primary key.

        Args:
            row:
                Stored RelPy row.

        Returns:
            A deep copy of the row.
        """

        return self._stored_row_to_export_dict(
            row,
            table_name=table_name,
            decrypt=decrypt,
        )

    def _column_to_python_list(
            self,
            table_name: str,
            column_name: str,
            *,
            decrypt: bool = False,
    ) -> list[Any]:
        """
        Converts a single RelPy column into a plain Python list.

        Args:
            table_name:
                Name of the table.

            column_name:
                Name of the column.

        Returns:
            A list containing all values in the column.
        """

        rows = self._table_to_python_list(
            table_name,
            decrypt=decrypt,
        )

        return [
            row[column_name]
            for row in rows
        ]

    def _table_to_python_list(
            self,
            table_name: str,
            *,
            decrypt: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Converts a full RelPy table into a list of plain Python dictionaries.

        This function intentionally calls the row-level export helper for
        every stored row.

        That keeps row export behavior consistent everywhere:
        - to_json(table)
        - to_pandas(table)
        - future exports

        Args:
            table_name:
                Name of the table.

        Returns:
            A list of row dictionaries.
        """

        return [
            self._stored_row_to_python_dict(
                row,
                table_name=table_name,
                decrypt=decrypt,
            )
            for row in self.data[table_name]
        ]

    def _row_by_primary_key(
            self,
            table_name: str,
            where_key: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Finds a stored row by primary key using the primary-key lookup cache.
        """

        self._validate_existing_table(table_name)
        self._validate_where_key(table_name, where_key)

        table_ref = self.schema[table_name]
        key = tuple(
            where_key[column_name]
            for column_name in table_ref.primary_key
        )

        lookup = self._primary_key_lookup_for_table(table_name)
        row = lookup.get(key)

        if row is None:
            raise KeyError(
                f"No row found in table '{table_name}' for primary key {where_key}."
            )

        return row

    def _primary_key_label(
            self,
            table_name: str,
            where_key: dict[str, Any],
    ) -> Any:
        """
        Builds a readable label for pandas.Series when exporting one row.

        Args:
            table_name:
                Name of the table.

            where_key:
                Primary key selector.

        Returns:
            If the primary key has one column:
                the single key value.

            If the primary key is composite:
                a tuple of key values in primary key order.
        """

        table_ref = self.schema[table_name]

        values = tuple(
            where_key[column_name]
            for column_name in table_ref.primary_key
        )

        if len(values) == 1:
            return values[0]

        return values

    def _json_default(self, value: Any) -> Any:
        """
        Handles values that json.dumps() cannot serialize by default.

        Currently supported special case:
        - bytes are encoded as base64.

        Args:
            value:
                Value passed by json.dumps() when it does not know how to serialize it.

        Returns:
            JSON-serializable representation.

        Raises:
            TypeError:
                If the value cannot be converted to JSON.
        """

        if isinstance(value, bytes):
            return {
                "__type__": "bytes",
                "encoding": "base64",
                "value": base64.b64encode(value).decode("ascii"),
            }

        if isinstance(value, (dt.datetime, dt.date, dt.time)):
            return value.isoformat()

        raise TypeError(
            f"Object of type {type(value).__name__} is not JSON serializable."
        )

    def to_numpy(
            self,
            table_name: str,
            column_name: str | None = None,
            where_key: dict[str, Any] | None = None,
            *,
            decrypt: bool = False,
            dtype: Any | None = None,
    ):
        """
        Exports table data to a NumPy array.

        Behavior:
        - Uses the same export semantics as to_list().
        - Internal RelPy metadata fields are hidden.
        - Encrypted values are masked by default.
        - If decrypt=True, encrypted values are decrypted using the loaded encryption key.
        - If column_name is provided, returns a 1D array.
        - Otherwise, returns a 2D array using schema column order.

        Examples:
            db.to_numpy("users")
            db.to_numpy("users", decrypt=True)
            db.to_numpy("users", column_name="email")
            db.to_numpy("users", where_key={"id": 1})
        """

        try:
            import numpy as np
        except ImportError as error:
            raise ImportError(
                "numpy is required for to_numpy(). "
                "Install it with: pip install numpy"
            ) from error

        exported_data = self.to_list(
            table_name=table_name,
            column_name=column_name,
            where_key=where_key,
            decrypt=decrypt,
        )

        if column_name is not None:
            return np.array(
                exported_data,
                dtype=dtype,
            )

        column_names = list(self.schema[table_name].columns.keys())

        if not exported_data:
            return np.empty(
                (0, len(column_names)),
                dtype=dtype if dtype is not None else object,
            )

        rows_as_lists = [
            [
                row.get(column_name)
                for column_name in column_names
            ]
            for row in exported_data
        ]

        return np.array(
            rows_as_lists,
            dtype=dtype,
        )
    
    # -------------------------------------------------------------------------
    # Internal NumPy export helpers
    # -------------------------------------------------------------------------

    def _table_to_numpy_array(
        self,
        table_name: str,
        dtype: Any | None,
        np_module: Any,
    ) -> tuple[Any, list[str]]:
        """
        Converts a full RelPy table into a 2D NumPy array.

        Args:
            table_name:
                Name of the table.

            dtype:
                Optional NumPy dtype.

            np_module:
                Imported NumPy module.

        Returns:
            A tuple:
                (array, column_names)

            array shape:
                (number_of_rows, number_of_columns)
        """

        self._validate_existing_table(table_name)

        columns = list(self.schema[table_name].columns.keys())
        rows = self._table_to_python_list(table_name)

        if not rows:
            array_dtype = dtype if dtype is not None else object
            return np_module.empty((0, len(columns)), dtype=array_dtype), columns

        matrix = []

        for row in rows:
            matrix.append([
                row[column_name]
                for column_name in columns
            ])

        # Full relational tables often contain mixed types.
        # Using dtype=object by default prevents NumPy from converting everything
        # into strings or another unwanted common type.
        array_dtype = dtype if dtype is not None else object

        return np_module.array(matrix, dtype=array_dtype), columns

    def _row_to_numpy_array(
        self,
        table_name: str,
        where_key: dict[str, Any],
        dtype: Any | None,
        np_module: Any,
    ) -> tuple[Any, list[str]]:
        """
        Converts a single RelPy row into a 1D NumPy array.

        The row is selected by primary key.

        Args:
            table_name:
                Name of the table.

            where_key:
                Primary key selector.

            dtype:
                Optional NumPy dtype.

            np_module:
                Imported NumPy module.

        Returns:
            A tuple:
                (array, column_names)

            array shape:
                (number_of_columns,)
        """

        self._validate_existing_table(table_name)
        self._validate_where_key(table_name, where_key)

        columns = list(self.schema[table_name].columns.keys())

        row = self._row_to_python_dict(
            table_name=table_name,
            where_key=where_key,
        )

        values = [
            row[column_name]
            for column_name in columns
        ]

        # Rows often contain mixed types, so object is safer by default.
        array_dtype = dtype if dtype is not None else object

        return np_module.array(values, dtype=array_dtype), columns

    def _column_to_numpy_array(
        self,
        table_name: str,
        column_name: str,
        dtype: Any | None,
        np_module: Any,
    ) -> Any:
        """
        Converts a single RelPy column into a 1D NumPy array.

        Args:
            table_name:
                Name of the table.

            column_name:
                Name of the column.

            dtype:
                Optional NumPy dtype.

            np_module:
                Imported NumPy module.

        Returns:
            NumPy array with shape:
                (number_of_rows,)
        """

        self._validate_existing_table(table_name)
        self._validate_existing_column(table_name, column_name)

        values = self._column_to_python_list(
            table_name=table_name,
            column_name=column_name,
        )

        if dtype is not None:
            return np_module.array(values, dtype=dtype)

        # For a single column, NumPy inference is usually useful.
        # Example:
        #   [1, 2, 3] -> int array
        #   [1.5, 2.0] -> float array
        #   ["a", "b"] -> string array
        return np_module.array(values)


    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    @dataclass(frozen=True)
    class Query:
        """
        Represents a query over one RelPy relation.

        A relation can be:
            - a real table
            - calculated rows from a view

        Query objects are immutable:
        each method returns a new Query object instead of changing the existing one.
        """

        db: Any

        table_name: str | None = None
        relation_name: str | None = None

        source_rows: tuple[dict[str, Any], ...] | None = None
        source_columns: tuple[str, ...] | None = None

        conditions: tuple[Condition | Callable[[dict[str, Any]], bool], ...] = field(default_factory=tuple)
        selected_columns: tuple[str, ...] | None = None
        order_specs: tuple[OrderSpec, ...] = field(default_factory=tuple)

        @classmethod
        def from_rows(
                cls,
                db: Any,
                relation_name: str,
                rows: list[dict[str, Any]],
                columns: list[str] | tuple[str, ...],
        ) -> "Query":
            """
            Creates a Query from already-calculated rows.

            Used mainly for views.

            This allows:

                db.view("paid_orders")
                  .where(col("amount") > 200)
                  .to_list()

            The view behaves like a relation of its own.
            """

            if not isinstance(relation_name, str) or not relation_name.strip():
                raise ValueError("relation_name must be a non-empty string.")

            if not isinstance(columns, (list, tuple)):
                raise TypeError("columns must be a list or tuple of column names.")

            normalized_columns = list(columns)

            for column_name in normalized_columns:
                if not isinstance(column_name, str):
                    raise TypeError("source column names must be strings.")

            if len(normalized_columns) != len(set(normalized_columns)):
                raise ValueError("source columns cannot contain duplicates.")

            column_set = set(normalized_columns)

            normalized_rows: list[dict[str, Any]] = []

            for row in rows:
                if not isinstance(row, dict):
                    raise TypeError("source rows must be dictionaries.")

                if set(row.keys()) != column_set:
                    raise ValueError(
                        "All source rows must contain exactly the declared source columns."
                    )

                normalized_rows.append(copy.deepcopy(row))

            return cls(
                db=db,
                table_name=None,
                relation_name=relation_name,
                source_rows=tuple(normalized_rows),
                source_columns=tuple(normalized_columns),
            )


    # -------------------------------------------------------------------------
    # Table Printing
    # -------------------------------------------------------------------------

    def to_sql(
        self,
        table_name: str,
        *,
        row: dict[str, Any] | None = None,
        where_key: dict[str, Any] | None = None,
        decrypt: bool = False,
    ) -> str:
        """
        Exports table rows as SQL INSERT statements.

        The target SQL table is assumed to already exist.

        Examples:
            db.to_sql("users")
            db.to_sql("users", where_key={"id": 1})
            db.to_sql("users", decrypt=True)

        If the table contains encrypted columns, decrypt=True is required so the
        generated SQL contains runnable plaintext values instead of masked values.
        """

        self._validate_existing_table(table_name)

        if row is not None and where_key is not None:
            raise ValueError("row and where_key cannot both be provided.")

        if self._table_has_encrypted_columns(table_name) and not decrypt:
            raise EncryptionError(
                f"Table '{table_name}' contains encrypted columns. "
                "Use decrypt=True to export runnable plaintext SQL."
            )

        if row is not None:
            rows = [copy.deepcopy(row)]
        elif where_key is not None:
            stored_row = self._row_by_primary_key(table_name, where_key)
            rows = [
                self._stored_row_to_python_dict(
                    stored_row,
                    table_name=table_name,
                    decrypt=decrypt,
                )
            ]
        else:
            rows = self.to_list(
                table_name,
                decrypt=decrypt,
            )

        column_names = list(self.schema[table_name].columns.keys())
        statements: list[str] = []

        for export_row in rows:
            values_sql = [
                self._sql_literal(export_row.get(column_name))
                for column_name in column_names
            ]

            columns_sql = ", ".join(
                self._quote_sql_identifier(column_name)
                for column_name in column_names
            )

            values_sql_text = ", ".join(values_sql)

            statements.append(
                f"INSERT INTO {self._quote_sql_identifier(table_name)} "
                f"({columns_sql}) VALUES ({values_sql_text});"
            )

        return "\n".join(statements)


    def _table_has_encrypted_columns(self, table_name: str) -> bool:
        return any(
            column_def.is_encrypted
            for column_def in self.schema[table_name].columns.values()
        )


    def _quote_sql_identifier(self, identifier: str) -> str:
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'


    def _sql_literal(self, value: Any) -> str:
        if value is None:
            return "NULL"

        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"

        if type(value) is int or type(value) is float:
            return str(value)

        if isinstance(value, bytes):
            return "X'" + value.hex() + "'"

        if isinstance(value, (dt.datetime, dt.date, dt.time)):
            text = value.isoformat()
        else:
            text = str(value)

        escaped = text.replace("'", "''")
        return f"'{escaped}'"

    def print_table(
        self,
        table_name: str,
        *,
        limit: int = 3,
        max_width: int = 24,
        decrypt: bool = False,
    ) -> None:
        """
        Prints a visual preview of a table.

        Encrypted values are shown as [ENCRYPTED] by default.
        Pass decrypt=True to display decrypted values when an encryption key is loaded.
        """

        print(
            self._format_table_preview(
                table_name=table_name,
                limit=limit,
                max_width=max_width,
                decrypt=decrypt,
            )
        )

    def _format_table_preview(
        self,
        table_name: str,
        *,
        limit: int = 3,
        max_width: int = 24,
        decrypt: bool = False,
    ) -> str:
        """
        Returns a visual table preview as a string.
        """

        self._validate_existing_table(table_name)

        if not isinstance(limit, int):
            raise TypeError("limit must be an integer.")

        if limit < 0:
            raise ValueError("limit cannot be negative.")

        if not isinstance(max_width, int):
            raise TypeError("max_width must be an integer.")

        if max_width < 8:
            raise ValueError("max_width must be at least 8.")

        table_def = self.schema[table_name]
        column_names = list(table_def.columns.keys())
        total_rows = len(self.data[table_name])

        if not column_names:
            return f"Table: {table_name} ({total_rows} rows)\n(no columns)"

        preview_rows = [
            self._stored_row_to_python_dict(
                row,
                table_name=table_name,
                decrypt=decrypt,
            )
            for row in self.data[table_name][:limit]
        ]

        raw_headers = [
            f"{column_name} ({self._column_type_display_name(table_name, column_name)})"
            for column_name in column_names
        ]

        headers = [
            self._format_table_cell(header, max_width=max_width)
            for header in raw_headers
        ]

        formatted_rows = [
            [
                self._format_table_cell(row.get(column_name), max_width=max_width)
                for column_name in column_names
            ]
            for row in preview_rows
        ]

        widths = []

        for column_index, header in enumerate(headers):
            values = [row[column_index] for row in formatted_rows]
            width = max([len(header), *(len(value) for value in values)])
            widths.append(width)

        top_border = self._table_border("┌", "┬", "┐", widths)
        middle_border = self._table_border("├", "┼", "┤", widths)
        bottom_border = self._table_border("└", "┴", "┘", widths)

        lines = [
            f"Table: {table_name} (showing {len(preview_rows)} of {total_rows} rows)",
            top_border,
            self._table_row(headers, widths),
            middle_border,
        ]

        for row in formatted_rows:
            lines.append(self._table_row(row, widths))

        lines.append(bottom_border)

        return "\n".join(lines)

    def _column_type_display_name(
        self,
        table_name: str,
        column_name: str,
    ) -> str:
        """
        Returns a user-friendly column type name.
        """

        column_def = self.schema[table_name].columns[column_name]

        if column_def.is_auto_number:
            type_name = "AutoNumber"
        else:
            type_name = getattr(
                column_def.data_type,
                "__name__",
                str(column_def.data_type),
            )

        if column_def.is_encrypted:
            return f"{type_name}, encrypted"

        return type_name

    def _format_table_cell(
            self,
            value: Any,
            *,
            max_width: int,
    ) -> str:
        """
        Converts a value into a printable table cell.
        """

        if value is None:
            text = "NULL"
        else:
            text = str(value)

        text = text.replace("\n", "\\n")

        if len(text) <= max_width:
            return text

        return text[: max_width - 1] + "…"

    def _table_border(
            self,
            left: str,
            middle: str,
            right: str,
            widths: list[int],
    ) -> str:
        """
        Builds a unicode table border.
        """

        return (
                left
                + middle.join("─" * (width + 2) for width in widths)
                + right
        )

    def _table_row(
            self,
            values: list[str],
            widths: list[int],
    ) -> str:
        """
        Builds a unicode table row.
        """

        cells = [
            f" {value.ljust(width)} "
            for value, width in zip(values, widths)
        ]

        return "│" + "│".join(cells) + "│"
