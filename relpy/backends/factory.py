"""
Backend factory.

Turns friendly arguments such as ``backend="postgres", url="postgresql://..."``
or ``backend="sqlite", path="data.db"`` into a concrete storage backend. This
is the single place that knows the alias names, so ``RelPy(...)`` and
``RelPy.connect(...)`` stay tiny.

Recognized backend aliases:

    memory                      -> in-memory only (the default)
    sqlite                      -> SQLite (needs `path=` or a sqlite `url=`)
    postgres / postgresql       -> PostgreSQL       (needs `url=`)
    mysql / mariadb             -> MySQL / MariaDB   (needs `url=`)
    oracle                      -> Oracle            (needs `url=`)
    azure / mssql / sqlserver   -> Azure SQL / SQL Server (needs `url=`)
    aws                         -> Amazon RDS/Aurora (needs `url=`; the URL's
                                   own scheme -- postgresql/mysql -- selects the
                                   engine, since RDS just hosts those engines)

A raw SQLAlchemy ``url=`` alone (no ``backend=``) also works; the scheme
selects the dialect.
"""

from __future__ import annotations

from typing import Any

from ..exceptions import BackendConfigurationError
from .base import MemoryBackend, StorageBackend

# Backend alias -> default URL scheme prefix used when the user gives a URL
# without a scheme, or to sanity-check a provided URL.
_ALIASES = {
    "memory": "memory",
    "": "memory",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "pg": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "azure": "mssql",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "aws": "aws",
    "remote": "remote",
}


def normalize_backend_name(backend: str | None) -> str:
    """Return the canonical backend key for a user-supplied name."""
    key = (backend or "memory").strip().lower()
    if key not in _ALIASES:
        supported = ", ".join(sorted(set(_ALIASES.values()) - {"memory", "aws", "remote"}))
        raise BackendConfigurationError(
            f"Unknown backend {backend!r}. Supported backends: memory, sqlite, "
            f"{supported}, azure, aws, remote."
        )
    return _ALIASES[key]


def create_backend(
    backend: str | None = None,
    *,
    url: str | None = None,
    path: str | None = None,
    connect_args: dict[str, Any] | None = None,
    echo: bool = False,
    **options: Any,
) -> StorageBackend:
    """
    Build a storage backend from friendly arguments.

    Raises:
        BackendConfigurationError: for unknown backends or missing url/path.
        BackendNotAvailableError:  if a required driver/library is missing.
        BackendConnectionError:    if the database cannot be reached.
    """
    canonical = normalize_backend_name(backend)

    if canonical == "memory":
        if url or path:
            raise BackendConfigurationError(
                "The in-memory backend does not accept a url or path."
            )
        return MemoryBackend()

    if canonical == "remote":
        # Handled one level up (RelPy.connect) because a remote connection
        # replaces the whole RelPy object with a client.
        raise BackendConfigurationError(
            "The 'remote' backend must be created via RelPy.connect('remote', "
            "url=...). It cannot be used as an in-process storage backend."
        )

    resolved_url = _resolve_url(canonical, url=url, path=path)

    # Import here so `import relpy` never requires SQLAlchemy.
    from .sql import SQLBackend

    return SQLBackend(
        resolved_url,
        connect_args=connect_args,
        echo=echo,
        **options,
    )


def _resolve_url(canonical: str, *, url: str | None, path: str | None) -> str:
    """Compute the final SQLAlchemy URL for a SQL backend."""
    if canonical == "sqlite":
        if url:
            return url
        if not path:
            raise BackendConfigurationError(
                "The sqlite backend needs a file path, e.g. "
                "RelPy(backend='sqlite', path='data.db'), or a sqlite url."
            )
        if path == ":memory:":
            # Marker consumed by SQLBackend via a shared static pool would be
            # ideal, but a plain shared-cache file-less DB is simplest here.
            return "sqlite://"
        return f"sqlite:///{path}"

    if path and not url:
        raise BackendConfigurationError(
            f"The {canonical} backend expects a connection url, not a path."
        )

    if not url:
        raise BackendConfigurationError(
            f"The {canonical} backend requires a url, e.g. "
            f"RelPy.connect('{canonical}', url='...')."
        )

    if canonical == "aws":
        # RDS/Aurora just hosts standard engines; the URL scheme decides which.
        if "://" not in url:
            raise BackendConfigurationError(
                "For backend='aws', pass a full url whose scheme selects the "
                "engine, e.g. 'postgresql+psycopg://user:pass@my-rds-host/db' "
                "or 'mysql+pymysql://user:pass@my-rds-host/db'."
            )
        return url

    return url
