from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .exceptions import EncryptionError


ENCRYPTED_MARKER = "__relpy_encrypted__"
ENCRYPTED_DISPLAY_VALUE = "[ENCRYPTED]"


class EncryptionMixin:
    """
    Adds encrypted column support.

    Encrypted values are stored as encrypted payload dictionaries.
    Query equality and indexes are supported through blind indexes.
    """

    @staticmethod
    def generate_encryption_key() -> bytes:
        """
        Generates a Fernet-compatible encryption key.

        Store this key outside the .relpy.json file.
        """
        return Fernet.generate_key()

    def set_encryption_key(self, encryption_key: bytes | str) -> None:
        """
        Sets the encryption key used for encryption, decryption and blind indexes.
        """

        if isinstance(encryption_key, str):
            encryption_key = encryption_key.encode("utf-8")

        # Validate key early.
        Fernet(encryption_key)

        self._encryption_key = encryption_key
        self._fernet = Fernet(encryption_key)

        decoded_key = base64.urlsafe_b64decode(encryption_key)
        self._blind_index_key = hashlib.sha256(
            b"relpy-blind-index-v1:" + decoded_key
        ).digest()

    def has_encryption_key(self) -> bool:
        return getattr(self, "_encryption_key", None) is not None

    def _require_encryption_key(self) -> None:
        if not self.has_encryption_key():
            raise EncryptionError(
                "Encryption key is required for this operation. "
                "Use db.set_encryption_key(key) or RelPy(encryption_key=key)."
            )

    def _is_encrypted_payload(self, value: Any) -> bool:
        return isinstance(value, dict) and value.get(ENCRYPTED_MARKER) is True

    def _serialize_plaintext_for_encryption(
        self,
        value: Any,
        data_type: type,
    ) -> str:
        """
        Converts a typed Python value into a stable JSON string before encryption.
        """

        type_name = self._encryption_type_name(data_type)

        if isinstance(value, bytes):
            json_value = {
                "type": "bytes",
                "value": base64.b64encode(value).decode("ascii"),
            }
        else:
            json_value = {
                "type": type_name,
                "value": value,
            }

        return json.dumps(
            json_value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _deserialize_plaintext_after_decryption(self, text: str) -> Any:
        payload = json.loads(text)

        value_type = payload["type"]
        value = payload["value"]

        if value_type == "bytes":
            return base64.b64decode(value.encode("ascii"))

        return value

    def _encryption_type_name(self, data_type: type) -> str:
        from .tables import AutoNumber

        if data_type is AutoNumber:
            return "AutoNumber"

        return getattr(data_type, "__name__", str(data_type))

    def _blind_index_for_value(
        self,
        *,
        table_name: str,
        column_name: str,
        value: Any,
        data_type: type,
    ) -> str:
        """
        Computes a deterministic keyed blind index for equality lookups.

        This allows:
            col("email") == "alice@example.com"

        without indexing the plaintext or ciphertext directly.
        """

        self._require_encryption_key()

        canonical_plaintext = self._serialize_plaintext_for_encryption(
            value=value,
            data_type=data_type,
        )

        scoped_value = json.dumps(
            {
                "table": table_name,
                "column": column_name,
                "value": canonical_plaintext,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        return hmac.new(
            self._blind_index_key,
            scoped_value,
            hashlib.sha256,
        ).hexdigest()

    def _encrypt_value(
        self,
        *,
        table_name: str,
        column_name: str,
        value: Any,
        data_type: type,
    ) -> dict[str, Any]:
        """
        Encrypts one value and returns a JSON-serializable encrypted payload.
        """

        self._require_encryption_key()

        plaintext = self._serialize_plaintext_for_encryption(
            value=value,
            data_type=data_type,
        ).encode("utf-8")

        ciphertext = self._fernet.encrypt(plaintext).decode("utf-8")

        blind_index = self._blind_index_for_value(
            table_name=table_name,
            column_name=column_name,
            value=value,
            data_type=data_type,
        )

        return {
            ENCRYPTED_MARKER: True,
            "algorithm": "Fernet",
            "data_type": self._encryption_type_name(data_type),
            "ciphertext": ciphertext,
            "blind_index": blind_index,
        }

    def _decrypt_value(self, encrypted_payload: dict[str, Any]) -> Any:
        """
        Decrypts one encrypted payload.
        """

        self._require_encryption_key()

        if not self._is_encrypted_payload(encrypted_payload):
            return encrypted_payload

        try:
            plaintext = self._fernet.decrypt(
                encrypted_payload["ciphertext"].encode("utf-8")
            ).decode("utf-8")
        except InvalidToken as error:
            raise EncryptionError(
                "Could not decrypt value. The encryption key may be wrong "
                "or the encrypted payload may be corrupted."
            ) from error

        return self._deserialize_plaintext_after_decryption(plaintext)

    def _encrypt_row_for_storage(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Encrypts encrypted columns after validation and before storage.
        """

        encrypted_row = dict(row)

        for column_name, column_def in self.schema[table_name].columns.items():
            if not column_def.is_encrypted:
                continue

            value = encrypted_row.get(column_name)

            if value is None:
                continue

            if self._is_encrypted_payload(value):
                continue

            encrypted_row[column_name] = self._encrypt_value(
                table_name=table_name,
                column_name=column_name,
                value=value,
                data_type=column_def.data_type,
            )

        return encrypted_row

    def _stored_row_to_export_dict(
        self,
        row: dict[str, Any],
        *,
        table_name: str | None = None,
        decrypt: bool = False,
    ) -> dict[str, Any]:
        """
        Converts a stored row to a public/export row.

        Internal fields are removed.
        Encrypted values are masked by default.
        """

        from .indexes import INTERNAL_ROW_ID

        public_row: dict[str, Any] = {}

        for column_name, value in row.items():
            if column_name == INTERNAL_ROW_ID:
                continue

            if self._is_encrypted_payload(value):
                if decrypt:
                    public_row[column_name] = self._decrypt_value(value)
                else:
                    public_row[column_name] = ENCRYPTED_DISPLAY_VALUE
            else:
                public_row[column_name] = value

        return public_row

    def _row_for_query_execution(self, row: dict[str, Any]) -> dict[str, Any]:
        """
        Returns a row suitable for query predicates/order/group calculations.

        Fast path:
            If the row has no encrypted payloads, returns the row itself.

        Encrypted path:
            If encrypted payloads are present, returns a decrypted copy.
        """

        has_encrypted_value = any(
            self._is_encrypted_payload(value)
            for value in row.values()
        )

        if not has_encrypted_value:
            return row

        query_row = {}

        for column_name, value in row.items():
            if self._is_encrypted_payload(value):
                query_row[column_name] = self._decrypt_value(value)
            else:
                query_row[column_name] = value

        return query_row

    def _encrypted_index_lookup_value(
        self,
        *,
        table_name: str,
        column_name: str,
        value: Any,
    ) -> str:
        """
        Converts a plaintext lookup value into a blind-index lookup key.
        """

        column_def = self.schema[table_name].columns[column_name]

        return self._blind_index_for_value(
            table_name=table_name,
            column_name=column_name,
            value=value,
            data_type=column_def.data_type,
        )