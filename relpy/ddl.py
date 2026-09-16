from __future__ import annotations

import datetime as dt

from .schema_types import ColumnDef, ForeignKeyDef


# =============================================================================
# DDL mixin
# =============================================================================

class DDLMixin:
    """
    Converts a RelPy schema into executable SQL DDL.

    Currently supports the PostgreSQL dialect:
    - to_ddl(): the whole schema as a single script (tables, primary keys,
      foreign keys, and optional metadata comments).
    - table_to_ddl(): a single CREATE TABLE statement.
    """

    # -------------------------------------------------------------------------
    # Public DDL API
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
