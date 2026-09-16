from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any

from .exceptions import ColumnNotFoundError, EncryptionError
from .hashing import default_json_encoder


# =============================================================================
# Export mixin
# =============================================================================

class ExportMixin:
    """
    Provides RelPy's data export API.

    This mixin covers:
    - Python-ecosystem exports: to_list(), to_json(), to_pandas(), to_numpy()
    - to_sql(): SQL INSERT statement generation
    - print_table(): a human-readable table preview

    All exports share the same row-level conversion helper,
    _stored_row_to_python_dict(), so masking/decryption and hidden internal
    fields behave consistently everywhere.
    """

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

        from ._bootstrap import ensure
        pd = ensure("pandas")

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
            default=self._json_default,
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

    def _json_default(self, value: Any) -> Any:
        """
        Handles values that json.dumps() cannot serialize by default.

        Delegates to the shared default JSON encoder, which supports bytes
        (encoded as base64) and datetime/date/time values (encoded as ISO
        8601 strings).

        Args:
            value:
                Value passed by json.dumps() when it does not know how to serialize it.

        Returns:
            JSON-serializable representation.

        Raises:
            TypeError:
                If the value cannot be converted to JSON.
        """

        return default_json_encoder(value)

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

        from ._bootstrap import ensure
        np = ensure("numpy")

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
    # Public export API - SQL and table preview
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
