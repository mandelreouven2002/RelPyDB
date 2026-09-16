"""
main.py — example usage of RelPyDB.

Just `import relpy`. Everything is available, and RelPyDB always runs on its
native C engine — the C columnar core is an integral part of the library, so
ordinary RelPy queries (filters, count / sum / avg / min / max) execute in C
automatically. There is no separate object to use and no pure-Python mode.

Optional dependencies (cryptography, SQLAlchemy, pandas, NumPy) are installed
automatically the first time a feature needs them. Set RELPY_AUTO_INSTALL=0 to
turn that off.
"""

import relpy
from relpy import RelPy, AutoNumber, col, count, sum_, avg, min_, max_


def main():
    print(f"RelPyDB {relpy.__version__}\n")

    db = RelPy()
    db.create_table("users")
    db.add_column("users", "id", AutoNumber, is_primary_key=True)
    db.add_column("users", "name", str)
    db.add_column("users", "age", int, nullable=True)
    db.add_column("users", "city", str)
    db.add_column("users", "score", float)
    db.add_column("users", "active", bool)

    db.insert_many("users", [
        {"name": "Ada", "age": 36, "city": "haifa", "score": 91.5, "active": True},
        {"name": "Bob", "age": None, "city": "eilat", "score": 40.0, "active": False},
        {"name": "Cy", "age": 52, "city": "haifa", "score": 77.0, "active": True},
        {"name": "Dee", "age": 19, "city": "eilat", "score": 12.0, "active": True},
    ])

    # These aggregates run in the C engine automatically.
    print("count active:", db.query("users").where(col("active") == True).count())  # noqa: E712
    print("avg(age)    :", round(db.query("users").average("age"), 2))
    print("sum(score)  :", db.query("users").sum("score"))
    print("min/max age :", db.query("users").min("age"), db.query("users").max("age"))

    # Filters, grouping, joins, views, encryption, persistence, SQL backends and
    # the server all work as before — this is one library.
    print("over 40     :", db.query("users").where(col("age") > 40).pluck("name"))
    print("by city     :", db.query("users").group_by("city")
          .aggregate(n=count(), a=avg("age")).to_list())


if __name__ == "__main__":
    main()
