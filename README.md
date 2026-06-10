# RelPyDB

RelPyDB is a Python-native in-memory relational data modeling and query library.

It lets you define relational tables, columns, primary keys, foreign keys, indexes, joins, grouped queries, exports, persistence, and encrypted columns directly inside Python code.

RelPyDB is not a replacement for PostgreSQL, SQLite, DuckDB, or any production database engine.  
It is designed for developers who want a lightweight relational object model inside Python.

For the library's website, [click here!](https://mandelreouven2002.github.io/RelPyDB/)

> Status: Alpha  
> The API may still change before version 1.0.0.

---

## Why RelPyDB?

Python has dictionaries, lists, pandas, SQLAlchemy, SQLite, and DuckDB.  
RelPyDB sits in a different place:

- More structured than plain dict / list
- More relational than pandas
- Lighter than SQLAlchemy
- Python-native, without writing SQL
- Useful for prototypes, local data workflows, teaching, testing, JSON normalization, and relational modeling

RelPyDB is especially useful when you want to model data relationally but still stay inside normal Python code.

---

## Installation

RelPyDB is currently installed directly from GitHub.

bash python -m pip install git+https://github.com/mandelreouven2002/RelPyDB.git 

For development:

bash git clone https://github.com/mandelreouven2002/RelPyDB.git cd RelPyDB python -m pip install -e ".[dev,bench]" 

---

## Quickstart

python from relpy import RelPy, AutoNumber, col, count  db = RelPy()  db.create_table("users") db.add_column("users", "id", AutoNumber, is_primary_key=True) db.add_column("users", "name", str, nullable=False)  db.create_table("orders") db.add_column("orders", "id", AutoNumber, is_primary_key=True) db.add_column("orders", "user_id", int, nullable=False, references="users.id") db.add_column("orders", "status", str, nullable=False)  alice = db.insert("users", {"name": "Alice"})  db.insert_many("orders", [     {"user_id": alice["id"], "status": "paid"},     {"user_id": alice["id"], "status": "paid"}, ])  result = (     db.query("orders")       .join("users")       .where(col("orders.status") == "paid")       .group_by("users.name")       .aggregate(order_count=count())       .to_list() )  print(result) 

Expected result:

python [     {         "users.name": "Alice",         "order_count": 2,     } ] 

---

## Core Features

RelPyDB currently supports:

- In-memory relational tables
- Typed columns
- Primary keys
- Composite primary keys
- Foreign keys
- RESTRICT, CASCADE, and SET NULL delete behavior
- insert, insert_many, update, and delete
- Query builder API
- where, select, order_by, limit, offset, and distinct
- Column conditions with col
- Logical conditions with AND, OR, NOT, &, |, and ~
- Joins
- Join shortcuts: inner_join, left_join, right_join, full_join, cross_join, natural_join
- Grouping and aggregation
- Views
- Indexes
- Encrypted columns
- Blind indexes for encrypted equality lookup
- Export to list, JSON, pandas, NumPy, SQL, and DDL
- Pretty table previews with print_table
- Save/load persistence with .relpy.json files
- Benchmark and compatibility examples

---

## Query Example

python from relpy import col  active_users = (     db.query("users")       .where(col("status") == "active")       .select("id", "name")       .order_by("name")       .to_list() ) 

SQL equivalent:

sql SELECT id, name FROM users WHERE status = 'active' ORDER BY name; 

---

## Join Example

python result = (     db.query("orders")       .join("users")       .select("orders.id", "users.name", "orders.status")       .to_list() ) 

RelPyDB can infer joins automatically from declared foreign keys.  
You can also provide explicit join columns:

python db.query("orders").join("users", on=("user_id", "id")) 

---

## Grouping Example

python from relpy import count, sum_  result = (     db.query("orders")       .group_by("status")       .aggregate(           order_count=count(),           total_amount=sum_("amount"),       )       .having(col("order_count") >= 2)       .to_list() ) 

---

## Persistence

RelPyDB databases can be saved and loaded as local JSON files.

python db.save("database.relpy.json")  loaded_db = RelPy.load("database.relpy.json") 

After loading, the database remains usable:

python loaded_db.insert("users", {"name": "Bob"}) 

Persistence stores schema, data, primary keys, foreign keys, indexes, AutoNumber sequences, and encrypted ciphertext.

Views are currently not persisted because they may contain Python callables or lambdas.

---

## Encrypted Columns

RelPyDB supports encrypted columns.

python key = RelPy.generate_encryption_key()  db = RelPy(encryption_key=key)  db.create_table("users") db.add_column("users", "id", AutoNumber, is_primary_key=True) db.add_column("users", "email", str, nullable=False, is_encrypted=True)  db.insert("users", {"email": "alice@example.com"}) 

Normal exports mask encrypted values:

python db.to_list("users") 

python [     {"id": 1, "email": "[ENCRYPTED]"} ] 

Use decrypt=True to explicitly decrypt:

python db.to_list("users", decrypt=True) 

Encryption keys are never saved inside .relpy.json files.

---

## Exports

RelPyDB supports several export formats:

python db.to_list("users") db.to_json("users") db.to_pandas("users") db.to_numpy("users") db.to_sql("users") db.to_ddl() db.print_table("users") 

Most table exports support:

- Whole table export
- Single column export
- Single row export with where_key
- Encrypted export with decrypt=True

Example:

python db.to_list("users", column_name="email") db.to_list("users", where_key={"id": 1}) 

---

## Documentation

Full documentation is available in the [documentation site](https://mandelreouven2002.github.io/RelPyDB/).

Recommended reading order:

1. Quickstart
2. Full Tutorial
3. Querying
4. Joins
5. Grouping
6. Exports
7. Persistence
8. Encryption
9. API Reference
10. Full Capability Checklist

Markdown docs:

text docs/ ├── QUICKSTART.md ├── FULL_TUTORIAL.md ├── API_REFERENCE.md ├── FUNCTIONS_REFERENCE.md ├── FULL_CAPABILITY_CHECKLIST.md └── EXAMPLES.md 

---

## Benchmarks

RelPyDB includes benchmark scripts that compare it with:

- SQLAlchemy / SQLite
- DuckDB
- pandas
- NumPy
- Pure Python loops

Benchmarks currently test:

- Build time
- Query time
- Joins
- Grouping
- Persistence
- JSON ingestion
- Multi-database logical workflows
- General public API smoke tests

Important note:

RelPyDB is a Python-native in-memory relational object library.  
It is not intended to outperform analytical engines like DuckDB.

---

## Current Limitations

RelPyDB is currently in alpha.

Known limitations:

- Not a production database engine
- Not a multi-user database server
- Not a full SQL engine
- Not designed for large-scale analytical workloads
- Views are not persisted
- Some encrypted operations are limited to equality lookup
- API may change before version 1.0.0

---
