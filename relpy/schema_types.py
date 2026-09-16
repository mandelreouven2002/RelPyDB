from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


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


# =============================================================================
# View definition
# =============================================================================

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
