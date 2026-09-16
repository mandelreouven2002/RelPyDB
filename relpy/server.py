"""
RelPy server
============

Turn any RelPy database -- in-memory or backed by SQLite/PostgreSQL/MySQL/
Oracle/Azure/AWS -- into a small HTTP service so other processes and machines
can use it through the ordinary RelPy API.

Think of it as the "runserver" for RelPy, in the spirit of a Django-style
management command. You define your database in Python, then serve it:

    from relpy import RelPy, AutoNumber

    db = RelPy(backend="sqlite", path="app.db")
    db.create_table("users")
    db.add_column("users", "id", AutoNumber, is_primary_key=True)
    db.add_column("users", "name", str)

    db.serve(host="0.0.0.0", port=8000)          # blocking

A client connects with the same front-facing API:

    from relpy import RelPy, col
    remote = RelPy.connect("remote", url="http://localhost:8000")
    remote.insert("users", {"name": "Ada"})
    remote.query("users").where(col("name") == "Ada").to_list()

You can also run a server straight from the command line::

    python -m relpy.server --backend sqlite --path app.db --host 0.0.0.0 --port 8000

or mount RelPy inside an existing WSGI stack (e.g. behind Django/gunicorn)
using :func:`make_wsgi_app`.

The wire protocol is a single ``POST /rpc`` endpoint taking
``{"op": ..., "args": ...}`` and returning ``{"ok": ..., "result"|"error": ...}``,
plus ``GET /health`` for load balancers. Values are transported with RelPy's
standard type-preserving encoding, so datetimes and bytes survive the trip.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .exceptions import RelPyError
from .indexes import INTERNAL_ROW_ID
from .schema_types import ForeignKeyDef
from .typemap import decode_row, encode_row, name_to_type

# ---------------------------------------------------------------------------
# Shared schema/data helpers (also used by the HTTP backend's reflection)
# ---------------------------------------------------------------------------


def apply_schema_payload(db: Any, schema_payload: dict[str, Any]) -> None:
    """
    Rebuild one or more tables on ``db`` from a serialized-schema payload
    (the format produced by ``RelPy._serialize_schema``).

    Foreign keys are attached as metadata directly rather than through
    ``add_column(references=...)`` so table order and cross-references never
    cause validation failures. Tables that already exist are left untouched.
    """
    # Pass 1: create tables + non-key columns; mark single-column primary keys.
    for table_name, table_payload in schema_payload.items():
        if table_name in db.schema:
            continue

        db.create_table(table_name)
        primary_key = list(table_payload.get("primary_key", []))
        single_pk = primary_key[0] if len(primary_key) == 1 else None

        for column_name, column_payload in table_payload["columns"].items():
            data_type = name_to_type(column_payload["data_type"])
            kwargs: dict[str, Any] = {
                "is_pii": column_payload.get("is_pii", False),
                "is_encrypted": column_payload.get("is_encrypted", False),
            }
            if column_payload.get("has_default", False):
                from .typemap import decode_value

                kwargs["default"] = decode_value(column_payload["default"])

            if column_name == single_pk:
                kwargs["is_primary_key"] = True
            else:
                kwargs["nullable"] = column_payload.get("nullable", True)

            db.add_column(table_name, column_name, data_type, **kwargs)

    # Pass 2: composite primary keys.
    for table_name, table_payload in schema_payload.items():
        primary_key = list(table_payload.get("primary_key", []))
        if len(primary_key) > 1 and db.schema[table_name].primary_key != primary_key:
            db.set_primary_key(table_name, primary_key)

    # Pass 3: foreign keys (metadata only).
    for table_name, table_payload in schema_payload.items():
        for local_column, fk_payload in table_payload.get("foreign_keys", {}).items():
            db.schema[table_name].foreign_keys[local_column] = ForeignKeyDef(
                local_column=fk_payload["local_column"],
                target_table=fk_payload["target_table"],
                target_column=fk_payload["target_column"],
                on_delete=fk_payload.get("on_delete", "RESTRICT"),
            )

    # Pass 4: auto-sequence counters.
    for table_name, table_payload in schema_payload.items():
        sequences = table_payload.get("auto_sequences", {})
        if sequences:
            db.schema[table_name].auto_sequences = {
                name: int(value) for name, value in sequences.items()
            }


def load_rows_into(db: Any, table_name: str, encoded_rows: list[dict[str, Any]]) -> None:
    """Replace a table's rows with decoded rows (bypassing insert validation)."""
    rows = [decode_row(row) for row in encoded_rows]
    db.data[table_name] = rows
    db._ensure_table_row_ids(table_name)
    db._refresh_row_positions(table_name)

    # Keep AutoNumber counters ahead of the data we just loaded.
    table_def = db.schema[table_name]
    for column_name, column_def in table_def.columns.items():
        if column_def.is_auto_number and rows:
            highest = max((row.get(column_name) or 0) for row in rows)
            table_def.auto_sequences[column_name] = max(
                table_def.auto_sequences.get(column_name, 0), int(highest)
            )


def server_drop_table(db: Any, table_name: str) -> None:
    """Fully remove a table (and its indexes) from a server-side RelPy db."""
    if table_name not in db.schema:
        return

    # Drop indexes on the table first.
    for index_name in [
        name for name, idx in db.indexes.items() if idx.table_name == table_name
    ]:
        try:
            db.drop_index(index_name)
        except Exception:
            db.indexes.pop(index_name, None)

    db.schema.pop(table_name, None)
    db.data.pop(table_name, None)
    db._next_row_id.pop(table_name, None)
    db._row_positions.pop(table_name, None)
    db._primary_key_lookup.pop(table_name, None)

    backend = getattr(db, "_backend", None)
    if backend is not None and not backend.is_in_memory:
        try:
            backend.drop_table(table_name)
        except Exception:
            pass
        db._materialized.discard(table_name)


# ---------------------------------------------------------------------------
# RPC operations
# ---------------------------------------------------------------------------


def _op_ping(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {"pong": True, "tables": list(db.schema.keys())}


def _op_get_schema(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    return db._serialize_schema()


def _op_snapshot(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    table = args.get("table")
    tables = [table] if table else list(db.schema.keys())
    result: dict[str, Any] = {}
    for name in tables:
        if name not in db.schema:
            continue
        rows = db.query(name).to_list()
        result[name] = [encode_row(row) for row in rows]
    return result


def _op_replace_table(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    table = args["table"]
    schema_payload = args["schema"]
    rows = args.get("rows", [])

    # Authoritatively replace the table: drop any existing version, recreate
    # from the incoming schema, load the rows, then persist to this server's
    # own durable backend (if any).
    server_drop_table(db, table)
    apply_schema_payload(db, schema_payload)
    load_rows_into(db, table, rows)
    db._rebuild_all_indexes()
    db._refresh_all_primary_key_lookups()

    backend = getattr(db, "_backend", None)
    if backend is not None and not backend.is_in_memory:
        db._rebuild_backend_table(table)

    return {"table": table, "rows": len(db.data.get(table, []))}


def _op_append_rows(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    table = args["table"]
    encoded_rows = args.get("rows", [])
    if table not in db.schema:
        raise RelPyError(f"Cannot append to unknown table {table!r}.")

    decoded = [decode_row(row) for row in encoded_rows]
    for row in decoded:
        stored = dict(row)
        db._attach_row_id(table, stored)
        db.data[table].append(stored)

    db._refresh_row_positions(table)
    db._rebuild_table_indexes(table)
    db._refresh_primary_key_lookup(table)

    table_def = db.schema[table]
    for column_name, column_def in table_def.columns.items():
        if column_def.is_auto_number and db.data[table]:
            highest = max((r.get(column_name) or 0) for r in db.data[table])
            table_def.auto_sequences[column_name] = max(
                table_def.auto_sequences.get(column_name, 0), int(highest)
            )

    backend = getattr(db, "_backend", None)
    if backend is not None and not backend.is_in_memory:
        clean = [{k: v for k, v in r.items() if k != INTERNAL_ROW_ID} for r in decoded]
        if table not in db._materialized:
            db._rebuild_backend_table(table)
        else:
            backend.insert_rows(db, table, clean)

    return {"table": table, "appended": len(decoded)}


def _op_drop_table(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    table = args["table"]
    server_drop_table(db, table)
    return {"dropped": table}


def _op_execute_sql(db: Any, args: dict[str, Any]) -> Any:
    sql = args["sql"]
    params = args.get("params")
    return db.execute_sql(sql, params)


OPERATIONS: dict[str, Callable[[Any, dict[str, Any]], Any]] = {
    "ping": _op_ping,
    "get_schema": _op_get_schema,
    "snapshot": _op_snapshot,
    "replace_table": _op_replace_table,
    "append_rows": _op_append_rows,
    "drop_table": _op_drop_table,
    "execute_sql": _op_execute_sql,
}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _dispatch(db: Any, lock: threading.Lock, message: dict[str, Any]) -> dict[str, Any]:
    """Run one RPC message against the database and return a response dict."""
    op = message.get("op")
    args = message.get("args") or {}

    handler = OPERATIONS.get(op)
    if handler is None:
        return {
            "ok": False,
            "error": {"type": "ServerError", "message": f"Unknown operation {op!r}."},
        }

    try:
        # Serialize mutations so concurrent clients cannot corrupt state.
        with lock:
            result = handler(db, args)
        return {"ok": True, "result": result}
    except RelPyError as exc:
        return {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}


def make_wsgi_app(db: Any, *, token: str | None = None):
    """
    Build a WSGI application serving ``db``.

    This lets RelPy ride inside an existing WSGI server (gunicorn, uWSGI, a
    Django deployment, ...). Mount it at any prefix; it handles ``/rpc`` and
    ``/health`` relative to that mount point.
    """
    lock = threading.Lock()

    def application(environ, start_response):
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET")

        def respond(status: str, payload: dict[str, Any]):
            body = json.dumps(payload).encode("utf-8")
            start_response(
                status,
                [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
            )
            return [body]

        if path.endswith("/health") and method == "GET":
            return respond("200 OK", {"ok": True, "status": "healthy"})

        if not path.endswith("/rpc") or method != "POST":
            return respond("404 Not Found", {"ok": False, "error": {"message": "Not found."}})

        if token is not None:
            auth = environ.get("HTTP_AUTHORIZATION", "")
            if auth != f"Bearer {token}":
                return respond(
                    "401 Unauthorized",
                    {"ok": False, "error": {"message": "Invalid or missing token."}},
                )

        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            raw = environ["wsgi.input"].read(length) if length else b"{}"
            message = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return respond(
                "400 Bad Request",
                {"ok": False, "error": {"message": f"Malformed request: {exc}"}},
            )

        return respond("200 OK", _dispatch(db, lock, message))

    return application


class _Handler(BaseHTTPRequestHandler):
    server_version = "RelPyServer/1.0"

    # Silence default request logging unless the server wants it.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("health") or self.path == "/":
            self._send(200, {"ok": True, "status": "healthy"})
        else:
            self._send(404, {"ok": False, "error": {"message": "Not found."}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("rpc"):
            self._send(404, {"ok": False, "error": {"message": "Not found."}})
            return

        token = getattr(self.server, "token", None)
        if token is not None:
            if self.headers.get("Authorization", "") != f"Bearer {token}":
                self._send(
                    401, {"ok": False, "error": {"message": "Invalid or missing token."}}
                )
                return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            message = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._send(
                400, {"ok": False, "error": {"message": f"Malformed request: {exc}"}}
            )
            return

        response = _dispatch(self.server.db, self.server.lock, message)
        self._send(200, response)


class RelPyServer:
    """
    An HTTP server wrapping a RelPy database.

    Example:
        db = RelPy(backend="sqlite", path="app.db")
        server = RelPyServer(db, host="0.0.0.0", port=8000, token="secret")
        server.start()            # blocking
        # ... or, in tests / background use:
        server.start_in_thread()
        ...
        server.stop()
    """

    def __init__(
        self,
        db: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        token: str | None = None,
        verbose: bool = False,
    ) -> None:
        self.db = db
        self.host = host
        self.port = port
        self.token = token
        self.verbose = verbose

        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        # Attach shared state onto the server object for the handler to use.
        self._httpd.db = db
        self._httpd.token = token
        self._httpd.verbose = verbose
        self._httpd.lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> None:
        """Run the server and block until interrupted."""
        banner = f"RelPy server listening on http://{self.host}:{self.port}  (Ctrl+C to stop)"
        print(banner)
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down RelPy server.")
        finally:
            self.stop()

    def start_in_thread(self) -> "RelPyServer":
        """Run the server in a background daemon thread and return immediately."""
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass


def serve(
    db: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    token: str | None = None,
    verbose: bool = False,
) -> RelPyServer:
    """Create and start (blocking) a :class:`RelPyServer` for ``db``."""
    server = RelPyServer(db, host=host, port=port, token=token, verbose=verbose)
    server.start()
    return server


# ---------------------------------------------------------------------------
# Command-line runner (a small "runserver")
# ---------------------------------------------------------------------------


def _build_cli_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m relpy.server",
        description="Serve a RelPy database over HTTP.",
    )
    parser.add_argument(
        "--backend", default="memory",
        help="Backend: memory, sqlite, postgres, mysql, oracle, azure, aws.",
    )
    parser.add_argument("--path", default=None, help="File path for the sqlite backend.")
    parser.add_argument("--url", default=None, help="SQLAlchemy URL for SQL backends.")
    parser.add_argument("--host", default="127.0.0.1", help="Host/interface to bind.")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on.")
    parser.add_argument("--token", default=None, help="Optional bearer token for auth.")
    parser.add_argument("--verbose", action="store_true", help="Log every request.")
    return parser


def main(argv: list[str] | None = None) -> int:
    from .tables import RelPy

    args = _build_cli_parser().parse_args(argv)

    try:
        db = RelPy(backend=args.backend, url=args.url, path=args.path)
    except RelPyError as exc:
        print(f"Failed to open database: {exc}")
        return 2

    serve(db, host=args.host, port=args.port, token=args.token, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
