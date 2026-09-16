from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path
from typing import Any

from .exceptions import (
    SchemaError,
    ViewError,
)
from .indexes import INTERNAL_ROW_ID


REL_PY_FILE_FORMAT = "relpy"
REL_PY_FILE_FORMAT_VERSION = 1


class PersistenceMixin:
    """
    Adds save/load support to RelPy.

    The persistence format is JSON-based and stores:
        - schema
        - data
        - auto sequences
        - internal stable row ids
        - index definitions

    It does not store index maps directly.
    Indexes are rebuilt on load.
    """

    def save(
        self,
        file_path: str | Path,
        *,
        indent: int | None = 2,
    ) -> None:
        """
        Saves the current RelPy database to a JSON file.

        Example:
            db.save("my_database.relpy.json")
        """

        if getattr(self, "views", None):
            if len(self.views) > 0:
                raise ViewError(
                    "RelPy cannot persist views yet because views may contain "
                    "Python callables/lambdas. Drop views before saving, or recreate "
                    "them manually after loading."
                )

        payload = {
            "format": REL_PY_FILE_FORMAT,
            "format_version": REL_PY_FILE_FORMAT_VERSION,
            "schema": self._serialize_schema(),
            "data": self._serialize_data(),
            "indexes": self._serialize_indexes(),
            "internal": {
                "next_row_id": dict(self._next_row_id),
            },
        }

        path = Path(file_path)

        with path.open("w", encoding="utf-8") as file:
            json.dump(
                payload,
                file,
                indent=indent,
                ensure_ascii=False,
            )

    @classmethod
    def load(
        cls,
        file_path: str | Path,
        *,
        encryption_key: bytes | str | None = None,
    ):
        """
        Loads a RelPy database from a JSON file.

        The encryption key is never stored in the file. Pass encryption_key if you
        want decrypted exports or queries over encrypted columns after loading.
        """

        path = Path(file_path)

        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)

        if payload.get("format") != REL_PY_FILE_FORMAT:
            raise SchemaError("File is not a RelPy persistence file.")

        if payload.get("format_version") != REL_PY_FILE_FORMAT_VERSION:
            raise SchemaError(
                f"Unsupported RelPy file format version: "
                f"{payload.get('format_version')!r}."
            )

        db = cls(encryption_key=encryption_key)

        schema_payload = payload["schema"]
        data_payload = payload["data"]
        indexes_payload = payload.get("indexes", {})
        internal_payload = payload.get("internal", {})

        db._load_schema_from_payload(schema_payload)
        db._load_data_from_payload(data_payload)

        db._next_row_id = {
            table_name: int(next_row_id)
            for table_name, next_row_id in internal_payload.get("next_row_id", {}).items()
        }

        for table_name in db.data.keys():
            db._ensure_table_row_ids(table_name)

            if table_name not in db._next_row_id:
                row_ids = [
                    row[INTERNAL_ROW_ID]
                    for row in db.data[table_name]
                    if INTERNAL_ROW_ID in row
                ]

                db._next_row_id[table_name] = (max(row_ids) + 1) if row_ids else 1

            db._refresh_row_positions(table_name)

        db._load_indexes_from_payload(indexes_payload)
        db._rebuild_all_indexes()
        db._refresh_all_primary_key_lookups()

        return db

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def _serialize_schema(self) -> dict[str, Any]:
        """
        Serializes database schema.
        """

        schema_payload: dict[str, Any] = {}

        for table_name, table_def in self.schema.items():
            columns_payload: dict[str, Any] = {}

            for column_name, column_def in table_def.columns.items():
                columns_payload[column_name] = {
                    "name": column_def.name,
                    "data_type": self._type_to_name(column_def.data_type),
                    "storage_type": self._type_to_name(column_def.storage_type),
                    "nullable": column_def.nullable,
                    "has_default": column_def.has_default,
                    "default": (
                        self._serialize_value(column_def.default)
                        if column_def.has_default
                        else None
                    ),
                    "is_primary_key": column_def.is_primary_key,
                    "is_pii": column_def.is_pii,
                    "is_encrypted": column_def.is_encrypted,
                }

            foreign_keys_payload: dict[str, Any] = {}

            for local_column, foreign_key in table_def.foreign_keys.items():
                foreign_keys_payload[local_column] = {
                    "local_column": foreign_key.local_column,
                    "target_table": foreign_key.target_table,
                    "target_column": foreign_key.target_column,
                    "on_delete": foreign_key.on_delete,
                }

            schema_payload[table_name] = {
                "name": table_def.name,
                "columns": columns_payload,
                "primary_key": list(table_def.primary_key),
                "foreign_keys": foreign_keys_payload,
                "auto_sequences": dict(table_def.auto_sequences),
            }

        return schema_payload

    def _serialize_data(self) -> dict[str, list[dict[str, Any]]]:
        """
        Serializes table data.

        Internal row ids are intentionally saved because indexes use stable row ids.
        """

        data_payload: dict[str, list[dict[str, Any]]] = {}

        for table_name, rows in self.data.items():
            data_payload[table_name] = [
                {
                    column_name: self._serialize_value(value)
                    for column_name, value in row.items()
                }
                for row in rows
            ]

        return data_payload

    def _serialize_indexes(self) -> dict[str, Any]:
        """
        Serializes index definitions.

        Does not serialize index_map because it is derived data.
        """

        indexes_payload: dict[str, Any] = {}

        for index_name, index_def in self.indexes.items():
            indexes_payload[index_name] = {
                "name": index_def.name,
                "table_name": index_def.table_name,
                "columns": list(index_def.columns),
                "unique": index_def.unique,
                "nulls_distinct": index_def.nulls_distinct,
            }

        return indexes_payload

    # -------------------------------------------------------------------------
    # Loading
    # -------------------------------------------------------------------------

    def _load_schema_from_payload(self, schema_payload: dict[str, Any]) -> None:
        """
        Recreates schema from serialized payload.
        """

        from .tables import ForeignKeyDef

        # First pass - create all tables.
        for table_name in schema_payload.keys():
            self.create_table(table_name)

        # Second pass - add all columns without foreign keys.
        for table_name, table_payload in schema_payload.items():
            primary_key = list(table_payload.get("primary_key", []))

            for column_name, column_payload in table_payload["columns"].items():
                data_type = self._name_to_type(column_payload["data_type"])

                kwargs = {
                    "nullable": column_payload["nullable"],
                    "is_pii": column_payload.get("is_pii", False),
                    "is_encrypted": column_payload.get("is_encrypted", False),
                }

                if column_payload.get("has_default", False):
                    kwargs["default"] = self._deserialize_value(column_payload["default"])

                # AutoNumber must be added as primary key immediately.
                # For composite primary keys, we add regular columns first
                # and call set_primary_key afterwards.
                is_single_column_primary_key = (
                    len(primary_key) == 1
                    and primary_key[0] == column_name
                )

                if is_single_column_primary_key:
                    kwargs["is_primary_key"] = True

                self.add_column(
                    table_name,
                    column_name,
                    data_type,
                    **kwargs,
                )

        # Third pass - apply composite primary keys if needed.
        for table_name, table_payload in schema_payload.items():
            primary_key = list(table_payload.get("primary_key", []))

            if len(primary_key) > 1:
                self.set_primary_key(table_name, primary_key)

        # Fourth pass - restore foreign keys.
        # We set them directly because columns already exist.
        for table_name, table_payload in schema_payload.items():
            for local_column, foreign_key_payload in table_payload.get("foreign_keys", {}).items():
                self.schema[table_name].foreign_keys[local_column] = ForeignKeyDef(
                    local_column=foreign_key_payload["local_column"],
                    target_table=foreign_key_payload["target_table"],
                    target_column=foreign_key_payload["target_column"],
                    on_delete=foreign_key_payload.get("on_delete", "RESTRICT"),
                )

        # Fifth pass - restore auto sequences.
        for table_name, table_payload in schema_payload.items():
            self.schema[table_name].auto_sequences = {
                column_name: int(next_value)
                for column_name, next_value in table_payload.get("auto_sequences", {}).items()
            }

    def _load_data_from_payload(self, data_payload: dict[str, list[dict[str, Any]]]) -> None:
        """
        Restores raw table data from payload.

        This bypasses insert() because insert() would create new AutoNumber values.
        """

        for table_name, rows_payload in data_payload.items():
            self._validate_existing_table(table_name)

            restored_rows = []

            for row_payload in rows_payload:
                row = {
                    column_name: self._deserialize_value(value)
                    for column_name, value in row_payload.items()
                }

                restored_rows.append(row)

            self.data[table_name] = restored_rows

            self._ensure_table_row_ids(table_name)
            self._refresh_row_positions(table_name)

    def _load_indexes_from_payload(self, indexes_payload: dict[str, Any]) -> None:
        """
        Recreates index definitions and rebuilds them.
        """

        for index_name, index_payload in indexes_payload.items():
            self.create_index(
                table_name=index_payload["table_name"],
                columns=index_payload["columns"],
                name=index_payload.get("name", index_name),
                unique=index_payload.get("unique", False),
                nulls_distinct=index_payload.get("nulls_distinct", True),
            )

    # -------------------------------------------------------------------------
    # Value encoding
    # -------------------------------------------------------------------------

    def _serialize_value(self, value: Any) -> Any:
        """
        Serializes values into JSON-compatible structures.
        """

        if isinstance(value, bytes):
            return {
                "__relpy_encoded__": True,
                "type": "bytes",
                "value": base64.b64encode(value).decode("ascii"),
            }

        if isinstance(value, dt.datetime):
            return {
                "__relpy_encoded__": True,
                "type": "datetime",
                "value": value.isoformat(),
            }

        if isinstance(value, dt.date):
            return {
                "__relpy_encoded__": True,
                "type": "date",
                "value": value.isoformat(),
            }

        if isinstance(value, dt.time):
            return {
                "__relpy_encoded__": True,
                "type": "time",
                "value": value.isoformat(),
            }

        if isinstance(value, dict):
            return {
                key: self._serialize_value(inner_value)
                for key, inner_value in value.items()
            }

        if isinstance(value, list):
            return [
                self._serialize_value(item)
                for item in value
            ]

        return value

    def _deserialize_value(self, value: Any) -> Any:
        """
        Deserializes values from JSON-compatible structures.
        """

        if isinstance(value, dict):
            if value.get("__relpy_encoded__") is True:
                value_type = value["type"]

                if value_type == "bytes":
                    return base64.b64decode(value["value"].encode("ascii"))

                if value_type == "datetime":
                    return dt.datetime.fromisoformat(value["value"])

                if value_type == "date":
                    return dt.date.fromisoformat(value["value"])

                if value_type == "time":
                    return dt.time.fromisoformat(value["value"])

                raise SchemaError(f"Unknown encoded RelPy value type: {value_type!r}")

            return {
                key: self._deserialize_value(inner_value)
                for key, inner_value in value.items()
            }

        if isinstance(value, list):
            return [
                self._deserialize_value(item)
                for item in value
            ]

        return value

    # -------------------------------------------------------------------------
    # Type encoding
    # -------------------------------------------------------------------------

    def _type_to_name(self, data_type: type) -> str:
        """
        Converts Python/RelPy types to stable names.
        """

        from .tables import AutoNumber

        if data_type is AutoNumber:
            return "AutoNumber"

        if data_type is int:
            return "int"

        if data_type is float:
            return "float"

        if data_type is str:
            return "str"

        if data_type is bool:
            return "bool"

        if data_type is bytes:
            return "bytes"

        if data_type is dict:
            return "dict"

        if data_type is list:
            return "list"

        if data_type is dt.datetime:
            return "datetime"

        if data_type is dt.date:
            return "date"

        if data_type is dt.time:
            return "time"

        raise SchemaError(
            f"Cannot persist unsupported column type: {data_type!r}"
        )

    def _name_to_type(self, type_name: str) -> type:
        """
        Converts persisted type names back to Python/RelPy types.
        """

        from .tables import AutoNumber

        mapping: dict[str, type] = {
            "AutoNumber": AutoNumber,
            "int": int,
            "float": float,
            "str": str,
            "bool": bool,
            "bytes": bytes,
            "dict": dict,
            "list": list,
            "datetime": dt.datetime,
            "date": dt.date,
            "time": dt.time,
        }

        if type_name not in mapping:
            raise SchemaError(f"Unknown persisted type name: {type_name!r}")

        return mapping[type_name]
