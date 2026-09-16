from __future__ import annotations

import copy
from typing import Any, Callable

from .exceptions import SchemaError, TableNotFoundError
from .indexes import INTERNAL_ROW_ID
from .schema_types import (
    AutoNumber,
    ColumnDef,
    DEFAULT_NOT_SET,
    ForeignKeyDef,
    TableDef,
    ViewDef,
)


# =============================================================================
# Schema mixin
# =============================================================================

class SchemaMixin:
    """
    Provides RelPy's schema definition and introspection API.

    This mixin is responsible for:
    - Defining tables, columns, primary keys, and foreign keys.
    - Describing the schema (describe_table / describe_schema).
    - Defining and querying logical views.
    - Validating names, types, defaults, and foreign key targets.
    - Maintaining the primary-key lookup cache used by other mixins.
    """

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
