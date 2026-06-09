# RelPyDB

RelPyDB is an in-memory relational data modeling and query library for Python.

It supports:

- Tables and columns
- Primary keys
- Foreign keys
- Insert, update, delete
- Query API
- Where conditions
- Joins
- Group by
- Having
- Views
- Indexes
- Persistence with save/load
- JSON / pandas / NumPy exports
- SQL DDL export

## Installation

Download the library, and then:

```bash
pip install relpydb
```

For local development:

```bash
pip install -e .
```

## Example

```python
from relpy import RelPy, AutoNumber, col, count, sum_

db = RelPy()

db.create_table("users")
db.add_column("users", "id", AutoNumber, is_primary_key=True)
db.add_column("users", "name", str, nullable=False)

db.create_table("orders")
db.add_column("orders", "id", AutoNumber, is_primary_key=True)
db.add_column("orders", "user_id", int, nullable=False, references="users.id")
db.add_column("orders", "amount", float, nullable=False)
db.add_column("orders", "status", str, nullable=False)

alice = db.insert("users", {"name": "Alice"})

db.insert("orders", {
    "user_id": alice["id"],
    "amount": 120.0,
    "status": "paid",
})

result = (
    db.query("orders")
      .join("users")
      .where(col("orders.status") == "paid")
      .group_by("users.name")
      .aggregate(
          order_count=count(),
          total_amount=sum_("orders.amount"),
      )
      .to_list()
)

print(result)

db.save("example.relpy.json")

loaded_db = RelPy.load("example.relpy.json")
print(loaded_db.query("users").to_list())
```