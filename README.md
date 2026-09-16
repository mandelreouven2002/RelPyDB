# RelPyDB

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Engine: C](https://img.shields.io/badge/engine-C-informational.svg)](#the-native-c-engine)
![Version: 2.0.0](https://img.shields.io/badge/RelPyDB's_Version-2.0.0-blue.svg)


**RelPyDB is a relational database you build and query in pure Python — backed by
a native C engine.** You define tables, columns, keys and relationships as Python
objects, then insert, update and query them with a fluent, SQL-like API — no SQL
strings, no ORM boilerplate, no external database process. The same data can also
be mirrored to a real SQL database or served over HTTP.

For the library's website, [click here!](https://mandelreouven2002.github.io/RelPyDB/)

```python
from relpy import RelPy, AutoNumber, col, count, avg

db = RelPy()
db.create_table("users")
db.add_column("users", "id", AutoNumber, is_primary_key=True)
db.add_column("users", "name", str)
db.add_column("users", "age", int, nullable=True)
db.add_column("users", "city", str)

db.insert_many("users", [
    {"name": "Ada", "age": 36, "city": "haifa"},
    {"name": "Bob", "age": None, "city": "eilat"},
    {"name": "Cy",  "age": 52, "city": "haifa"},
])

db.query("users").where(col("age") > 40).pluck("name")            # ['Cy']
db.query("users").average("age")                                  # 44.0
db.query("users").group_by("city").aggregate(n=count()).to_list()
```

## What you get

RelPyDB is a full relational toolkit, not just an in-memory dict wrapper:

- **Schema with real types & constraints** — tables and typed columns
  (`int`, `float`, `bool`, `str`, `datetime`, `date`, `time`, `bytes`, `dict`,
  `list`), primary keys (including auto-incrementing `AutoNumber`), foreign keys
  with `ON DELETE CASCADE / SET NULL / RESTRICT`, and `NOT NULL` / `UNIQUE`
  constraints — all enforced.
- **A fluent query language** — `where`, `select`, `order_by`, `limit`/`offset`,
  `distinct`, built from `col(...)` expressions (`==`, `!=`, `<`, `>`, `in_`,
  `between`, `is_null`, `like`) combined with `AND` / `OR` / `NOT`.
- **Joins** — inner, left, right, full, cross and natural joins, on explicit
  keys or inferred from foreign keys.
- **Grouping & aggregation** — `group_by(...).aggregate(count(), sum_(), avg(),
  min_(), max_())` with `HAVING`.
- **Views** — named, reusable virtual queries.
- **Indexes** — single-column, composite and unique indexes for fast equality
  lookups, plus blind indexes over encrypted columns.
- **Column-level encryption** — mark columns encrypted and RelPyDB encrypts them
  at rest, while still allowing equality lookups via blind indexes.
- **Exports** — `to_list`, `to_json`, `to_pandas`, `to_numpy`, `to_sql`, a
  human-readable `print_table`, and SQL DDL generation.
- **Persistence** — `save` / `load` your database to a JSON file.
- **Real SQL backends** — mirror the *same* API to SQLite, PostgreSQL,
  MySQL/MariaDB, Oracle, Azure SQL, or Amazon RDS/Aurora:
  `RelPy(backend="sqlite", path=...)`, `RelPy.connect("postgres", url=...)`.
- **Client / server** — run a database over HTTP (`db.serve(...)`, a CLI runner,
  or a WSGI app) and connect to it remotely with the identical API
  (`RelPy.connect("remote", url=...)`).
- **Clear errors** — a rich exception hierarchy (`TableNotFoundError`,
  `ConstraintError`, `QueryError`, `BackendConnectionError`, …) all under
  `RelPyError`.
- **A native C engine** (below) that runs the heavy work — and installs its own
  optional dependencies on first use.

## The native C engine

RelPyDB runs on a **native C columnar engine** that is part of `RelPy` itself,
not a separate object. Ordinary queries execute in C automatically: `count`,
`sum`, `avg`, `min`, `max`, structured `where` filters, `GROUP BY`, and inner
(equi) joins — including a **fused join + group-by** that runs both in one C
pass. The engine is cached per database and rebuilt per-table only when that
table changes. As a result RelPyDB's aggregates and grouping are competitive
with, or faster than, sqlite and DuckDB.

**There is no pure-Python mode and no switch to turn C off.** The C engine is a
hard requirement: if it has not been built, `import relpy` fails with a clear
instruction to build it. Anything the C core doesn't cover (left/right/full/
cross joins, ordering, `DISTINCT`, richer column types, encryption, persistence,
SQL backends, the server) runs on the Python path — always with identical
results.

## Install

A C compiler and the Python development headers are required.

```bash
# from a source checkout
pip install .

# or directly from GitHub
pip install git+https://github.com/mandelreouven2002/RelPyDB.git
```

<details>
<summary><strong>macOS</strong></summary>

```bash
xcode-select --install            # once: installs the clang compiler
brew install python               # Python with development headers
python3 -m venv .venv && source .venv/bin/activate
pip install .
```
</details>

<details>
<summary><strong>Linux (Debian/Ubuntu)</strong></summary>

```bash
sudo apt-get install build-essential python3-dev
python3 -m venv .venv && source .venv/bin/activate
pip install .
```
</details>

<details>
<summary><strong>Windows</strong></summary>

Install the "Microsoft C++ Build Tools", then:

```bash
py -m venv .venv && .venv\Scripts\activate
pip install .
```
</details>

### Verify the C engine is active

Run this **from a directory other than the source checkout** (otherwise Python
imports the local `relpy/` folder, which has no compiled `.so`):

```bash
python -c "import relpy, os; \
print('C extensions:', [f for f in os.listdir(os.path.dirname(relpy.__file__)) if f.endswith('.so')])"
```

You should see two `.so` files: `_relpy_core...` and `_relpyengine...`.

### Dependencies install themselves

Optional dependencies are installed **on first use**: encrypted columns pull in
`cryptography`, the SQL backends pull in `SQLAlchemy` (and a driver), and
`to_pandas` / `to_numpy` pull in `pandas` / `numpy`. Set `RELPY_AUTO_INSTALL=0`
to disable this.

## A fuller taste

```python
from relpy import RelPy, AutoNumber, col, count, sum_

db = RelPy()
db.create_table("users")
db.add_column("users", "id", AutoNumber, is_primary_key=True)
db.add_column("users", "name", str)
db.add_column("users", "city", str)

db.create_table("orders")
db.add_column("orders", "id", AutoNumber, is_primary_key=True)
db.add_column("orders", "user_id", int, references="users.id", on_delete="CASCADE")
db.add_column("orders", "amount", float)

db.insert_many("users", [{"name": "Ada", "city": "haifa"},
                         {"name": "Bob", "city": "eilat"}])
db.insert_many("orders", [{"user_id": 1, "amount": 9.99},
                          {"user_id": 1, "amount": 4.50},
                          {"user_id": 2, "amount": 7.00}])

# join + group-by (runs as a single C pass)
db.query("orders").join("users").group_by("users.city") \
  .aggregate(total=sum_("orders.amount"), n=count()).to_list()

# an index for fast equality lookups
db.create_index("users", ["city"])

# persistence: snapshot the whole database to a JSON file, and load it back
db.save("mydb.relpy.json")
loaded = RelPy.load("mydb.relpy.json")

# a named view (a reusable virtual query)
db.create_view("haifa_users", lambda d: d.query("users").where(col("city") == "haifa"))
db.view("haifa_users").pluck("name")

# same API, backed by SQLite instead of memory
sql_db = RelPy(backend="sqlite", path="data.db")
```

## Documentation

Full documentation lives in [`website/`](website/) — open `website/index.html`
in a browser, or host it (e.g. GitHub Pages). It covers the quickstart, a full
tutorial, every query feature, joins, grouping, views, indexes, exports,
persistence, encryption, the SQL backends, the server, and the API reference.

## Benchmark

[`benchmarks/relpy_benchmark.py`](benchmarks/relpy_benchmark.py) compares RelPyDB
against pandas, sqlite3, DuckDB, SQLAlchemy and hand-written Python loops on
identical data, with correctness verification and rigorous statistics.

```bash
python benchmarks/relpy_benchmark.py --quick
python benchmarks/relpy_benchmark.py --sizes 1000,100000
```

A sample run is in [`benchmarks/sample_output/`](benchmarks/sample_output/).

## Project layout

```
relpy/            the library (Python API + C engine source in relpy/_core_src/)
examples/         a runnable quickstart
benchmarks/       the benchmark suite and a sample report
website/          the documentation site
```

## License

Licensed under the [Apache License 2.0](LICENSE). Copyright 2026 Reuven Mandelovich.
