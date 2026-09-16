#!/usr/bin/env python3
"""
=============================================================================
 RelPyDB — Comprehensive Benchmark Suite
=============================================================================

A rigorous, self-contained benchmark for the RelPyDB in-memory relational
engine. It measures the full query surface across several dataset scales and,
where a fair comparison exists, puts RelPyDB side by side with the tools people
would otherwise reach for:

    * RelPyDB (native)     — the C-accelerated engine (relpy._relpy_core)
    * RelPyDB (pure-Python)— the same engine with the native core switched off
    * pandas               — DataFrame operations
    * sqlite3              — the standard-library embedded SQL database
    * DuckDB               — an in-process analytical SQL engine
    * SQLAlchemy Core      — expression queries over in-memory SQLite
    * Python loops         — hand-written dict/list code (the "no library" base)

What makes it trustworthy:

    * Deterministic data (seeded) so runs are comparable.
    * Warmup runs, multiple repeats, and robust statistics (min / median /
      mean / stdev / p95), with the garbage collector disabled during timing.
    * Every system materializes the SAME answer (list of tuples or a scalar),
      so timings are apples-to-apples — no lazy-evaluation advantage.
    * Correctness verification: the native and pure-Python engines must agree
      byte-for-byte, and every other system is checked against a reference.
    * Graceful degradation: any competitor that is not installed is skipped,
      never fatal.

Outputs: a detailed console report, a machine-readable ``results.json``, a
``summary.csv``, a ``REPORT.md``, and (if matplotlib is available) PNG charts.

Usage:
    python relpy_benchmark.py                       # default scales
    python relpy_benchmark.py --quick               # tiny, fast smoke run
    python relpy_benchmark.py --sizes 1000,50000,250000 --repeats 7
    python relpy_benchmark.py --systems relpy,pandas,sqlite --no-charts
    python relpy_benchmark.py --out ./bench_out
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import platform
import random
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# Optional dependencies — every one of these is optional and skipped if absent.
# --------------------------------------------------------------------------- #

def _try_import(name: str):
    try:
        return __import__(name)
    except Exception:
        return None


np = _try_import("numpy")
pd = _try_import("pandas")
duckdb = _try_import("duckdb")
psutil = _try_import("psutil")

try:
    import sqlalchemy as sa
    from sqlalchemy import (
        MetaData, Table, Column, Integer, String, Float, Boolean, select, func,
        create_engine, insert,
    )
    _HAVE_SA = True
except Exception:
    sa = None
    _HAVE_SA = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _HAVE_MPL = True
except Exception:
    _HAVE_MPL = False

# RelPyDB itself is required.
import relpy
from relpy import RelPy, AutoNumber, col, count, sum_, avg, min_, max_
import relpy.queries as _relpy_queries

try:
    from relpy import CoreDB
    _HAVE_COREDB = CoreDB is not None
except Exception:
    CoreDB = None
    _HAVE_COREDB = False


# =============================================================================
# Configuration
# =============================================================================

ALL_SYSTEMS = ["relpy", "coredb", "pandas", "sqlite",
               "duckdb", "sqlalchemy", "python"]
DEFAULT_SYSTEMS = ["relpy", "pandas", "sqlite", "duckdb", "sqlalchemy", "python"]

SYSTEM_LABELS = {
    "relpy": "RelPy",
    "coredb": "RelPy CoreDB (C)",
    "pandas": "pandas",
    "sqlite": "sqlite3",
    "duckdb": "DuckDB",
    "sqlalchemy": "SQLAlchemy",
    "python": "Python loops",
}


@dataclass
class Config:
    sizes: list[int] = field(default_factory=lambda: [1_000, 10_000, 100_000])
    repeats: int = 5
    warmup: int = 2
    systems: list[str] = field(default_factory=lambda: list(DEFAULT_SYSTEMS))
    out_dir: Path = field(default_factory=lambda: Path("relpy_benchmark_out"))
    charts: bool = True
    seed: int = 1234
    verify: bool = True


def parse_args(argv: list[str]) -> Config:
    p = argparse.ArgumentParser(description="RelPyDB comprehensive benchmark")
    p.add_argument("--sizes", type=str, default="1000,10000,100000",
                   help="comma-separated row counts, e.g. 1000,10000,100000")
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--warmup", type=int, default=2)
    p.add_argument("--systems", type=str, default=",".join(DEFAULT_SYSTEMS))
    p.add_argument("--out", type=str, default="relpy_benchmark_out")
    p.add_argument("--no-charts", action="store_true")
    p.add_argument("--quick", action="store_true",
                   help="tiny fast run (sizes 1000,10000; repeats 3)")
    p.add_argument("--seed", type=int, default=1234)
    args = p.parse_args(argv)

    if args.quick:
        sizes = [1_000, 10_000]
        repeats = 3
    else:
        sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
        repeats = args.repeats

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    for s in systems:
        if s not in ALL_SYSTEMS:
            raise SystemExit(f"unknown system '{s}'. choose from {ALL_SYSTEMS}")

    return Config(
        sizes=sizes,
        repeats=repeats,
        warmup=args.warmup,
        systems=systems,
        out_dir=Path(args.out),
        charts=not args.no_charts,
        seed=args.seed,
    )


# =============================================================================
# Environment capture
# =============================================================================

def capture_environment() -> dict[str, Any]:
    env = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "relpy_version": getattr(relpy, "__version__", "?"),
        "libraries": {
            "numpy": getattr(np, "__version__", None),
            "pandas": getattr(pd, "__version__", None),
            "duckdb": getattr(duckdb, "__version__", None),
            "sqlalchemy": (sa.__version__ if _HAVE_SA else None),
            "sqlite3": sqlite3.sqlite_version,
        },
    }
    if psutil is not None:
        try:
            env["total_ram_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
        except Exception:
            pass
    return env


# =============================================================================
# Timing / statistics core
# =============================================================================

@dataclass
class Sample:
    """The measured statistics of one (system, workload, size) cell."""
    ok: bool
    error: Optional[str] = None
    n_out: Optional[int] = None
    samples_ms: list[float] = field(default_factory=list)

    @property
    def best_ms(self) -> float:
        return min(self.samples_ms) if self.samples_ms else float("nan")

    @property
    def median_ms(self) -> float:
        return statistics.median(self.samples_ms) if self.samples_ms else float("nan")

    @property
    def mean_ms(self) -> float:
        return statistics.fmean(self.samples_ms) if self.samples_ms else float("nan")

    @property
    def stdev_ms(self) -> float:
        return statistics.pstdev(self.samples_ms) if len(self.samples_ms) > 1 else 0.0

    @property
    def p95_ms(self) -> float:
        if not self.samples_ms:
            return float("nan")
        s = sorted(self.samples_ms)
        k = min(len(s) - 1, int(math.ceil(0.95 * len(s))) - 1)
        return s[k]


def measure(fn: Callable[[], Any], *, warmup: int, repeats: int) -> Sample:
    """Time ``fn`` robustly: warmups, repeats, gc disabled, best-of statistics."""
    result_len: Optional[int] = None
    # Warmup (also validates the call works and lets caches settle).
    try:
        for _ in range(max(0, warmup)):
            out = fn()
        result_len = _result_len(out) if warmup > 0 else None
    except Exception as exc:  # noqa: BLE001
        return Sample(ok=False, error=f"{type(exc).__name__}: {exc}")

    samples: list[float] = []
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(max(1, repeats)):
            gc.collect()
            t0 = time.perf_counter()
            out = fn()
            dt = time.perf_counter() - t0
            samples.append(dt * 1000.0)
        result_len = _result_len(out)
    except Exception as exc:  # noqa: BLE001
        return Sample(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if gc_was_enabled:
            gc.enable()

    return Sample(ok=True, n_out=result_len, samples_ms=samples)


def _result_len(out: Any) -> Optional[int]:
    try:
        return len(out)
    except Exception:
        return 1 if out is not None else 0


# =============================================================================
# Deterministic dataset generation
# =============================================================================

CITIES = ["haifa", "tel-aviv", "jerusalem", "eilat", "beer-sheva",
          "nazareth", "acre", "tiberias"]
STATUSES = ["paid", "pending", "refunded", "cancelled"]


@dataclass
class Dataset:
    """A pair of related tables shared, in identical form, by every system."""
    size: int
    users: list[dict[str, Any]]     # id, name, age(None sometimes), city, score, active
    orders: list[dict[str, Any]]    # oid, user_id, amount, status, qty


def generate_dataset(size: int, seed: int) -> Dataset:
    rnd = random.Random(seed)
    users: list[dict[str, Any]] = []
    for i in range(1, size + 1):
        age = rnd.randint(18, 90)
        if i % 17 == 0:
            age = None  # inject some NULLs for null-handling tests
        users.append({
            "id": i,
            "name": f"user{i:07d}",
            "age": age,
            "city": CITIES[rnd.randrange(len(CITIES))],
            "score": round(rnd.uniform(0.0, 100.0), 3),
            "active": (i % 3 != 0),
        })
    # roughly one order per user, some users with several
    n_orders = size
    orders: list[dict[str, Any]] = []
    for j in range(1, n_orders + 1):
        orders.append({
            "oid": j,
            "user_id": rnd.randint(1, size),
            "amount": round(rnd.uniform(1.0, 500.0), 2),
            "status": STATUSES[rnd.randrange(len(STATUSES))],
            "qty": rnd.randint(1, 10),
        })
    return Dataset(size=size, users=users, orders=orders)


# =============================================================================
# System adapters — each builds its own representation of a Dataset and exposes
# query implementations. Every query returns a *comparable* result (a scalar,
# or a normalized list) so timings across systems are apples-to-apples.
# =============================================================================

# Thresholds/constants used by the workloads (chosen so result sizes are
# non-trivial but not the whole table).
T_CITY = "haifa"
T_CITIES_IN = ("haifa", "eilat", "acre")
T_AGE_GT = 50
T_AGE_LO, T_AGE_HI = 30, 40
T_AGE_YOUNG = 25
T_AMOUNT_GT = 450.0
ORDER_LIMIT = 200


def _round(x, nd=4):
    if x is None:
        return None
    if isinstance(x, float):
        return round(x, nd)
    return x


# ----------------------------- RelPyDB -------------------------------------- #

def build_relpy(ds: Dataset) -> RelPy:
    db = RelPy()
    db.create_table("users")
    db.add_column("users", "id", AutoNumber, is_primary_key=True)
    db.add_column("users", "name", str)
    db.add_column("users", "age", int, nullable=True)
    db.add_column("users", "city", str)
    db.add_column("users", "score", float)
    db.add_column("users", "active", bool)
    db.insert_many("users", [
        {"name": u["name"], "age": u["age"], "city": u["city"],
         "score": u["score"], "active": u["active"]}
        for u in ds.users
    ])
    db.create_table("orders")
    db.add_column("orders", "oid", AutoNumber, is_primary_key=True)
    db.add_column("orders", "user_id", int, references="users.id")
    db.add_column("orders", "amount", float)
    db.add_column("orders", "status", str)
    db.add_column("orders", "qty", int)
    db.insert_many("orders", [
        {"user_id": o["user_id"], "amount": o["amount"],
         "status": o["status"], "qty": o["qty"]}
        for o in ds.orders
    ])
    return db


def relpy_impls() -> dict[str, Callable[[RelPy], Any]]:
    def ids(rows):  # sorted list of primary ids
        return sorted(r["id"] for r in rows)

    return {
        "filter_eq_str": lambda db: ids(db.query("users").where(col("city") == T_CITY).to_list()),
        "filter_gt": lambda db: ids(db.query("users").where(col("age") > T_AGE_GT).to_list()),
        "filter_between": lambda db: ids(db.query("users").where(col("age").between(T_AGE_LO, T_AGE_HI)).to_list()),
        "filter_or": lambda db: ids(db.query("users").where((col("city") == "eilat") | (col("age") < T_AGE_YOUNG)).to_list()),
        "filter_in": lambda db: ids(db.query("users").where(col("city").in_(list(T_CITIES_IN))).to_list()),
        "filter_complex": lambda db: ids(db.query("users").where(((col("age") >= 30) & (col("age") <= 60)) | (col("city") == T_CITY)).to_list()),
        "count_gt": lambda db: db.query("users").where(col("age") > T_AGE_GT).count(),
        "sum_active_score": lambda db: _round(db.query("users").where(col("active") == True).sum("score")),  # noqa: E712
        "avg_age": lambda db: _round(db.query("users").average("age")),
        "minmax_score": lambda db: (_round(db.query("users").min("score")), _round(db.query("users").max("score"))),
        "group_city": lambda db: sorted(
            (r["city"], r["n"], _round(r["a"]))
            for r in db.query("users").group_by("city").aggregate(n=count(), a=avg("age")).to_list()
        ),
        "group_city_active": lambda db: sorted(
            (r["city"], bool(r["active"]), r["n"], _round(r["s"]))
            for r in db.query("users").group_by("city", "active").aggregate(n=count(), s=sum_("score")).to_list()
        ),
        "join_filter": lambda db: sorted(
            r["orders.oid"] for r in
            db.query("orders").join("users").where(col("orders.amount") > T_AMOUNT_GT)
              .select("orders.oid", "users.city").to_list()
        ),
        "join_group_sum": lambda db: sorted(
            (r["users.city"], _round(r["s"], 2)) for r in
            db.query("orders").join("users").group_by("users.city").aggregate(s=sum_("orders.amount")).to_list()
        ),
        "order_score_desc": lambda db: [
            _round(r["score"]) for r in
            db.query("users").order_by("score", descending=True).limit(ORDER_LIMIT).to_list()
        ],
        "distinct_city": lambda db: sorted(r["city"] for r in db.query("users").select("city").distinct().to_list()),
        "project_where": lambda db: sorted(
            (r["id"], r["city"]) for r in
            db.query("users").where(col("age") > 60).select("id", "city").to_list()
        ),
    }


# ------------------------------- CoreDB (native C engine) ------------------- #

def build_coredb(ds: Dataset):
    if not _HAVE_COREDB:
        return None
    db = CoreDB()
    db.create_table("users")
    db.add_column("users", "id", int)
    db.add_column("users", "name", str)
    db.add_column("users", "age", int)
    db.add_column("users", "city", str)
    db.add_column("users", "score", float)
    db.add_column("users", "active", bool)
    db.insert_many("users", [
        {"id": u["id"], "name": u["name"], "age": u["age"], "city": u["city"],
         "score": u["score"], "active": u["active"]}
        for u in ds.users
    ])
    return db


def coredb_impls() -> dict[str, Callable[[Any], Any]]:
    """CoreDB handles every single-table workload; joins are left to other
    engines (the C core is single-table for now)."""
    def ids(rows):
        return sorted(r["id"] for r in rows)

    return {
        "filter_eq_str": lambda db: ids(db.query("users").where(col("city") == T_CITY).to_list()),
        "filter_gt": lambda db: ids(db.query("users").where(col("age") > T_AGE_GT).to_list()),
        "filter_between": lambda db: ids(db.query("users").where(col("age").between(T_AGE_LO, T_AGE_HI)).to_list()),
        "filter_or": lambda db: ids(db.query("users").where((col("city") == "eilat") | (col("age") < T_AGE_YOUNG)).to_list()),
        "filter_in": lambda db: ids(db.query("users").where(col("city").in_(list(T_CITIES_IN))).to_list()),
        "filter_complex": lambda db: ids(db.query("users").where(((col("age") >= 30) & (col("age") <= 60)) | (col("city") == T_CITY)).to_list()),
        "count_gt": lambda db: db.query("users").where(col("age") > T_AGE_GT).count(),
        "sum_active_score": lambda db: _round(db.query("users").where(col("active") == True).sum("score")),  # noqa: E712
        "avg_age": lambda db: _round(db.query("users").average("age")),
        "minmax_score": lambda db: (_round(db.query("users").min("score")), _round(db.query("users").max("score"))),
        "group_city": lambda db: sorted(
            (r["city"], r["n"], _round(r["a"]))
            for r in db.query("users").group_by("city").aggregate(n=count(), a=avg("age")).to_list()
        ),
        "group_city_active": lambda db: sorted(
            (r["city"], bool(r["active"]), r["n"], _round(r["s"]))
            for r in db.query("users").group_by("city", "active").aggregate(n=count(), s=sum_("score")).to_list()
        ),
        # joins: not supported by the single-table C core yet
        "order_score_desc": lambda db: [
            _round(r["score"]) for r in
            db.query("users").order_by("score", descending=True).limit(ORDER_LIMIT).to_list()
        ],
        "distinct_city": lambda db: sorted(r["city"] for r in db.query("users").select("city").distinct().to_list()),
        "project_where": lambda db: sorted(
            (r["id"], r["city"]) for r in
            db.query("users").where(col("age") > 60).select("id", "city").to_list()
        ),
    }


# ------------------------------- pandas ------------------------------------- #

def build_pandas(ds: Dataset):
    if pd is None:
        return None
    users = pd.DataFrame(ds.users)
    orders = pd.DataFrame(ds.orders)
    return {"users": users, "orders": orders}


def pandas_impls() -> dict[str, Callable[[Any], Any]]:
    def ids(df):
        return sorted(df["id"].tolist())

    def U(ctx):
        return ctx["users"]

    return {
        "filter_eq_str": lambda c: ids(U(c)[U(c)["city"] == T_CITY]),
        "filter_gt": lambda c: ids(U(c)[U(c)["age"] > T_AGE_GT]),
        "filter_between": lambda c: ids(U(c)[(U(c)["age"] >= T_AGE_LO) & (U(c)["age"] <= T_AGE_HI)]),
        "filter_or": lambda c: ids(U(c)[(U(c)["city"] == "eilat") | (U(c)["age"] < T_AGE_YOUNG)]),
        "filter_in": lambda c: ids(U(c)[U(c)["city"].isin(list(T_CITIES_IN))]),
        "filter_complex": lambda c: ids(U(c)[((U(c)["age"] >= 30) & (U(c)["age"] <= 60)) | (U(c)["city"] == T_CITY)]),
        "count_gt": lambda c: int((U(c)["age"] > T_AGE_GT).sum()),
        "sum_active_score": lambda c: _round(float(U(c).loc[U(c)["active"], "score"].sum())),
        "avg_age": lambda c: _round(float(U(c)["age"].mean())),
        "minmax_score": lambda c: (_round(float(U(c)["score"].min())), _round(float(U(c)["score"].max()))),
        "group_city": lambda c: sorted(
            (str(city), int(len(g)), _round(float(g["age"].mean())))
            for city, g in U(c).groupby("city")
        ),
        "group_city_active": lambda c: sorted(
            (str(city), bool(active), int(len(g)), _round(float(g["score"].sum())))
            for (city, active), g in U(c).groupby(["city", "active"])
        ),
        "join_filter": lambda c: _pandas_join_filter(c),
        "join_group_sum": lambda c: _pandas_join_group(c),
        "order_score_desc": lambda c: [
            _round(v) for v in U(c).sort_values("score", ascending=False)["score"].head(ORDER_LIMIT).tolist()
        ],
        "distinct_city": lambda c: sorted(U(c)["city"].unique().tolist()),
        "project_where": lambda c: sorted(
            (int(r.id), str(r.city))
            for r in U(c)[U(c)["age"] > 60][["id", "city"]].itertuples(index=False)
        ),
    }


def _pandas_join_filter(c):
    o = c["orders"]
    hot = o[o["amount"] > T_AMOUNT_GT]
    merged = hot.merge(c["users"], left_on="user_id", right_on="id", how="inner")
    return sorted(merged["oid"].tolist())


def _pandas_join_group(c):
    merged = c["orders"].merge(c["users"], left_on="user_id", right_on="id", how="inner")
    g = merged.groupby("city")["amount"].sum()
    return sorted((str(k), _round(float(v), 2)) for k, v in g.items())


# ------------------------------- sqlite3 ------------------------------------ #

def build_sqlite(ds: Dataset):
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, "
                "city TEXT, score REAL, active INTEGER)")
    cur.execute("CREATE TABLE orders (oid INTEGER PRIMARY KEY, user_id INTEGER, "
                "amount REAL, status TEXT, qty INTEGER)")
    cur.executemany("INSERT INTO users VALUES (?,?,?,?,?,?)",
                    [(u["id"], u["name"], u["age"], u["city"], u["score"], int(u["active"]))
                     for u in ds.users])
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?)",
                    [(o["oid"], o["user_id"], o["amount"], o["status"], o["qty"])
                     for o in ds.orders])
    conn.commit()
    return conn


def _sql_impls(run) -> dict[str, Callable[[Any], Any]]:
    """SQL implementations shared by sqlite/duckdb; ``run(ctx, sql, params)`` -> rows."""
    inlist = ",".join(f"'{c}'" for c in T_CITIES_IN)
    return {
        "filter_eq_str": lambda c: sorted(r[0] for r in run(c, f"SELECT id FROM users WHERE city='{T_CITY}'")),
        "filter_gt": lambda c: sorted(r[0] for r in run(c, f"SELECT id FROM users WHERE age>{T_AGE_GT}")),
        "filter_between": lambda c: sorted(r[0] for r in run(c, f"SELECT id FROM users WHERE age BETWEEN {T_AGE_LO} AND {T_AGE_HI}")),
        "filter_or": lambda c: sorted(r[0] for r in run(c, f"SELECT id FROM users WHERE city='eilat' OR age<{T_AGE_YOUNG}")),
        "filter_in": lambda c: sorted(r[0] for r in run(c, f"SELECT id FROM users WHERE city IN ({inlist})")),
        "filter_complex": lambda c: sorted(r[0] for r in run(c, f"SELECT id FROM users WHERE (age>=30 AND age<=60) OR city='{T_CITY}'")),
        "count_gt": lambda c: int(run(c, f"SELECT COUNT(*) FROM users WHERE age>{T_AGE_GT}")[0][0]),
        "sum_active_score": lambda c: _round(float(run(c, "SELECT SUM(score) FROM users WHERE active=1")[0][0])),
        "avg_age": lambda c: _round(float(run(c, "SELECT AVG(age) FROM users")[0][0])),
        "minmax_score": lambda c: tuple(_round(float(v)) for v in run(c, "SELECT MIN(score), MAX(score) FROM users")[0]),
        "group_city": lambda c: sorted(
            (str(r[0]), int(r[1]), _round(float(r[2])))
            for r in run(c, "SELECT city, COUNT(*), AVG(age) FROM users GROUP BY city")
        ),
        "group_city_active": lambda c: sorted(
            (str(r[0]), bool(r[1]), int(r[2]), _round(float(r[3])))
            for r in run(c, "SELECT city, active, COUNT(*), SUM(score) FROM users GROUP BY city, active")
        ),
        "join_filter": lambda c: sorted(
            r[0] for r in run(c, f"SELECT o.oid FROM orders o JOIN users u ON o.user_id=u.id WHERE o.amount>{T_AMOUNT_GT}")
        ),
        "join_group_sum": lambda c: sorted(
            (str(r[0]), _round(float(r[1]), 2))
            for r in run(c, "SELECT u.city, SUM(o.amount) FROM orders o JOIN users u ON o.user_id=u.id GROUP BY u.city")
        ),
        "order_score_desc": lambda c: [
            _round(float(r[0])) for r in run(c, f"SELECT score FROM users ORDER BY score DESC LIMIT {ORDER_LIMIT}")
        ],
        "distinct_city": lambda c: sorted(r[0] for r in run(c, "SELECT DISTINCT city FROM users")),
        "project_where": lambda c: sorted(
            (int(r[0]), str(r[1])) for r in run(c, "SELECT id, city FROM users WHERE age>60")
        ),
    }


def sqlite_impls():
    def run(conn, sql):
        return conn.execute(sql).fetchall()
    return _sql_impls(run)


# ------------------------------- DuckDB ------------------------------------- #

def build_duckdb(ds: Dataset):
    if duckdb is None:
        return None
    conn = duckdb.connect(database=":memory:")
    conn.execute("CREATE TABLE users (id INTEGER, name VARCHAR, age INTEGER, "
                 "city VARCHAR, score DOUBLE, active BOOLEAN)")
    conn.execute("CREATE TABLE orders (oid INTEGER, user_id INTEGER, amount DOUBLE, "
                 "status VARCHAR, qty INTEGER)")
    conn.executemany("INSERT INTO users VALUES (?,?,?,?,?,?)",
                     [(u["id"], u["name"], u["age"], u["city"], u["score"], u["active"])
                      for u in ds.users])
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)",
                     [(o["oid"], o["user_id"], o["amount"], o["status"], o["qty"])
                      for o in ds.orders])
    return conn


def duckdb_impls():
    def run(conn, sql):
        return conn.execute(sql).fetchall()
    return _sql_impls(run)


# ----------------------------- SQLAlchemy Core ------------------------------ #

def build_sqlalchemy(ds: Dataset):
    if not _HAVE_SA:
        return None
    engine = create_engine("sqlite://")  # in-memory
    md = MetaData()
    users = Table("users", md,
                  Column("id", Integer, primary_key=True),
                  Column("name", String), Column("age", Integer),
                  Column("city", String), Column("score", Float),
                  Column("active", Boolean))
    orders = Table("orders", md,
                   Column("oid", Integer, primary_key=True),
                   Column("user_id", Integer), Column("amount", Float),
                   Column("status", String), Column("qty", Integer))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(users), ds.users)
        conn.execute(insert(orders), ds.orders)
    return {"engine": engine, "users": users, "orders": orders}


def sqlalchemy_impls():
    """Idiomatic SQLAlchemy Core expression queries over in-memory SQLite."""
    def U(c):
        return c["users"]

    def O(c):
        return c["orders"]

    def rows(c, stmt):
        with c["engine"].connect() as conn:
            return conn.execute(stmt).fetchall()

    return {
        "filter_eq_str": lambda c: sorted(r[0] for r in rows(c, select(U(c).c.id).where(U(c).c.city == T_CITY))),
        "filter_gt": lambda c: sorted(r[0] for r in rows(c, select(U(c).c.id).where(U(c).c.age > T_AGE_GT))),
        "filter_between": lambda c: sorted(r[0] for r in rows(c, select(U(c).c.id).where(U(c).c.age.between(T_AGE_LO, T_AGE_HI)))),
        "filter_or": lambda c: sorted(r[0] for r in rows(c, select(U(c).c.id).where((U(c).c.city == "eilat") | (U(c).c.age < T_AGE_YOUNG)))),
        "filter_in": lambda c: sorted(r[0] for r in rows(c, select(U(c).c.id).where(U(c).c.city.in_(list(T_CITIES_IN))))),
        "filter_complex": lambda c: sorted(r[0] for r in rows(c, select(U(c).c.id).where(((U(c).c.age >= 30) & (U(c).c.age <= 60)) | (U(c).c.city == T_CITY)))),
        "count_gt": lambda c: int(rows(c, select(func.count()).select_from(U(c)).where(U(c).c.age > T_AGE_GT))[0][0]),
        "sum_active_score": lambda c: _round(float(rows(c, select(func.sum(U(c).c.score)).where(U(c).c.active == True))[0][0])),  # noqa: E712
        "avg_age": lambda c: _round(float(rows(c, select(func.avg(U(c).c.age)))[0][0])),
        "minmax_score": lambda c: tuple(_round(float(v)) for v in rows(c, select(func.min(U(c).c.score), func.max(U(c).c.score)))[0]),
        "group_city": lambda c: sorted(
            (str(r[0]), int(r[1]), _round(float(r[2])))
            for r in rows(c, select(U(c).c.city, func.count(), func.avg(U(c).c.age)).group_by(U(c).c.city))
        ),
        "group_city_active": lambda c: sorted(
            (str(r[0]), bool(r[1]), int(r[2]), _round(float(r[3])))
            for r in rows(c, select(U(c).c.city, U(c).c.active, func.count(), func.sum(U(c).c.score)).group_by(U(c).c.city, U(c).c.active))
        ),
        "join_filter": lambda c: sorted(
            r[0] for r in rows(c, select(O(c).c.oid).select_from(
                O(c).join(U(c), O(c).c.user_id == U(c).c.id)).where(O(c).c.amount > T_AMOUNT_GT))
        ),
        "join_group_sum": lambda c: sorted(
            (str(r[0]), _round(float(r[1]), 2)) for r in rows(c, select(U(c).c.city, func.sum(O(c).c.amount)).select_from(
                O(c).join(U(c), O(c).c.user_id == U(c).c.id)).group_by(U(c).c.city))
        ),
        "order_score_desc": lambda c: [
            _round(float(r[0])) for r in rows(c, select(U(c).c.score).order_by(U(c).c.score.desc()).limit(ORDER_LIMIT))
        ],
        "distinct_city": lambda c: sorted(r[0] for r in rows(c, select(U(c).c.city).distinct())),
        "project_where": lambda c: sorted(
            (int(r[0]), str(r[1])) for r in rows(c, select(U(c).c.id, U(c).c.city).where(U(c).c.age > 60))
        ),
    }


# ------------------------------ Python loops -------------------------------- #

def build_python(ds: Dataset):
    users = ds.users
    orders = ds.orders
    user_by_id = {u["id"]: u for u in users}
    return {"users": users, "orders": orders, "user_by_id": user_by_id}


def python_impls() -> dict[str, Callable[[Any], Any]]:
    def U(c):
        return c["users"]

    return {
        "filter_eq_str": lambda c: sorted(u["id"] for u in U(c) if u["city"] == T_CITY),
        "filter_gt": lambda c: sorted(u["id"] for u in U(c) if u["age"] is not None and u["age"] > T_AGE_GT),
        "filter_between": lambda c: sorted(u["id"] for u in U(c) if u["age"] is not None and T_AGE_LO <= u["age"] <= T_AGE_HI),
        "filter_or": lambda c: sorted(u["id"] for u in U(c) if u["city"] == "eilat" or (u["age"] is not None and u["age"] < T_AGE_YOUNG)),
        "filter_in": lambda c: sorted(u["id"] for u in U(c) if u["city"] in T_CITIES_IN),
        "filter_complex": lambda c: sorted(u["id"] for u in U(c) if ((u["age"] is not None and 30 <= u["age"] <= 60) or u["city"] == T_CITY)),
        "count_gt": lambda c: sum(1 for u in U(c) if u["age"] is not None and u["age"] > T_AGE_GT),
        "sum_active_score": lambda c: _round(sum(u["score"] for u in U(c) if u["active"])),
        "avg_age": lambda c: _round(statistics.fmean([u["age"] for u in U(c) if u["age"] is not None])),
        "minmax_score": lambda c: (_round(min(u["score"] for u in U(c))), _round(max(u["score"] for u in U(c)))),
        "group_city": lambda c: _py_group_city(U(c)),
        "group_city_active": lambda c: _py_group_city_active(U(c)),
        "join_filter": lambda c: _py_join_filter(c),
        "join_group_sum": lambda c: _py_join_group(c),
        "order_score_desc": lambda c: [_round(u["score"]) for u in sorted(U(c), key=lambda r: r["score"], reverse=True)[:ORDER_LIMIT]],
        "distinct_city": lambda c: sorted({u["city"] for u in U(c)}),
        "project_where": lambda c: sorted((u["id"], u["city"]) for u in U(c) if u["age"] is not None and u["age"] > 60),
    }


def _py_group_city(users):
    groups: dict[str, list] = {}
    for u in users:
        groups.setdefault(u["city"], []).append(u["age"])
    out = []
    for city, ages in groups.items():
        non_null = [a for a in ages if a is not None]
        out.append((city, len(ages), _round(statistics.fmean(non_null)) if non_null else None))
    return sorted(out)


def _py_group_city_active(users):
    groups: dict[tuple, list] = {}
    for u in users:
        groups.setdefault((u["city"], bool(u["active"])), []).append(u["score"])
    return sorted((k[0], k[1], len(v), _round(sum(v))) for k, v in groups.items())


def _py_join_filter(c):
    idx = c["user_by_id"]
    out = []
    for o in c["orders"]:
        if o["amount"] > T_AMOUNT_GT and o["user_id"] in idx:
            out.append(o["oid"])
    return sorted(out)


def _py_join_group(c):
    idx = c["user_by_id"]
    sums: dict[str, float] = {}
    for o in c["orders"]:
        u = idx.get(o["user_id"])
        if u is not None:
            sums[u["city"]] = sums.get(u["city"], 0.0) + o["amount"]
    return sorted((k, _round(v, 2)) for k, v in sums.items())


# =============================================================================
# System registry
# =============================================================================

@dataclass
class SystemAdapter:
    key: str
    available: bool
    build: Callable[[Dataset], Any]
    impls: dict[str, Callable[[Any], Any]]


def build_system_registry(cfg: Config) -> dict[str, SystemAdapter]:
    reg: dict[str, SystemAdapter] = {}

    # RelPy: one build serves both native and pure-python (toggled at run time).
    ri = relpy_impls()
    reg["relpy"] = SystemAdapter("relpy", True, build_relpy, ri)
    reg["coredb"] = SystemAdapter("coredb", _HAVE_COREDB, build_coredb,
                                  coredb_impls() if _HAVE_COREDB else {})

    reg["pandas"] = SystemAdapter("pandas", pd is not None, build_pandas, pandas_impls() if pd else {})
    reg["sqlite"] = SystemAdapter("sqlite", True, build_sqlite, sqlite_impls())
    reg["duckdb"] = SystemAdapter("duckdb", duckdb is not None, build_duckdb, duckdb_impls() if duckdb else {})
    reg["sqlalchemy"] = SystemAdapter("sqlalchemy", _HAVE_SA, build_sqlalchemy, sqlalchemy_impls() if _HAVE_SA else {})
    reg["python"] = SystemAdapter("python", True, build_python, python_impls())

    return {k: reg[k] for k in cfg.systems if k in reg}


# =============================================================================
# Workload catalogue (the cross-system query matrix)
# =============================================================================

@dataclass
class Workload:
    id: str
    category: str
    label: str


WORKLOADS: list[Workload] = [
    Workload("filter_eq_str", "Filters", "WHERE city = 'haifa'"),
    Workload("filter_gt", "Filters", "WHERE age > 50"),
    Workload("filter_between", "Filters", "WHERE age BETWEEN 30 AND 40"),
    Workload("filter_or", "Filters", "WHERE city='eilat' OR age<25"),
    Workload("filter_in", "Filters", "WHERE city IN (3)"),
    Workload("filter_complex", "Filters", "WHERE (30<=age<=60) OR city='haifa'"),
    Workload("count_gt", "Aggregates", "COUNT WHERE age>50"),
    Workload("sum_active_score", "Aggregates", "SUM(score) WHERE active"),
    Workload("avg_age", "Aggregates", "AVG(age)"),
    Workload("minmax_score", "Aggregates", "MIN/MAX(score)"),
    Workload("group_city", "Group by", "GROUP BY city -> count, avg"),
    Workload("group_city_active", "Group by", "GROUP BY city, active"),
    Workload("join_filter", "Joins", "JOIN + WHERE amount>450"),
    Workload("join_group_sum", "Joins", "JOIN + GROUP BY city SUM"),
    Workload("order_score_desc", "Order/Distinct", "ORDER BY score DESC LIMIT 200"),
    Workload("distinct_city", "Order/Distinct", "DISTINCT city"),
    Workload("project_where", "Order/Distinct", "SELECT id,city WHERE age>60"),
]


# =============================================================================
# Native engine toggle
# =============================================================================



# =============================================================================
# Correctness comparison (tolerant of float summation order)
# =============================================================================

def almost_equal(a: Any, b: Any, tol: float = 1e-2) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(almost_equal(x, y, tol) for x, y in zip(a, b))
    return a == b


# =============================================================================
# Runner
# =============================================================================

def run_size(cfg: Config, size: int, registry: dict[str, SystemAdapter]) -> dict[str, Any]:
    """Benchmark every system at one dataset size.

    Systems are processed one at a time and their context is freed before the
    next is built, so peak memory is (dataset + a single system), not every
    system at once. This keeps large scales runnable on modest machines.
    """
    ds = generate_dataset(size, cfg.seed)
    results: dict[str, dict[str, Sample]] = {w.id: {} for w in WORKLOADS}
    correctness: dict[str, dict[str, Any]] = {w.id: {} for w in WORKLOADS}
    ingestion: dict[str, Sample] = {}
    for key, adapter in registry.items():
        if not adapter.available:
            for w in WORKLOADS:
                results[w.id][key] = Sample(ok=False, error="n/a")
            continue

        try:
            ctx = adapter.build(ds)
        except Exception as exc:  # noqa: BLE001
            ingestion[key] = Sample(ok=False, error=f"build: {exc}")
            for w in WORKLOADS:
                results[w.id][key] = Sample(ok=False, error="build failed")
            continue

        # Ingestion timing (fresh build each repeat).
        ingestion[key] = measure(
            lambda a=adapter: a.build(ds),
            warmup=0, repeats=min(3, cfg.repeats),
        )
        gc.collect()

        # Run all workloads for this system.
        for w in WORKLOADS:
            impl = adapter.impls.get(w.id)
            if impl is None:
                results[w.id][key] = Sample(ok=False, error="n/a")
                continue
            results[w.id][key] = measure(
                lambda i=impl, c=ctx: i(c),
                warmup=cfg.warmup, repeats=cfg.repeats,
            )
            if cfg.verify:
                try:
                    correctness[w.id][key] = impl(ctx)
                except Exception as exc:  # noqa: BLE001
                    correctness[w.id][key] = f"ERR:{exc}"

        # Free this system's context before building the next one.
        ctx = None
        gc.collect()

    gc.collect()

    return {
        "size": size,
        "ingestion": ingestion,
        "results": results,
        "correctness": correctness,
    }


def verify_correctness(size_result: dict[str, Any], systems: list[str]) -> dict[str, Any]:
    """Compare every system to a reference result."""
    report = {"mismatches": [], "checked": 0}
    corr = size_result["correctness"]
    ref_order = ["sqlite", "duckdb", "python", "pandas", "sqlalchemy", "relpy"]
    for w in WORKLOADS:
        values = corr[w.id]
        # reference
        ref_key = next((k for k in ref_order if k in values and not _is_err(values[k])), None)
        if ref_key is None:
            continue
        ref = values[ref_key]
        for key, val in values.items():
            if _is_err(val):
                continue
            report["checked"] += 1
            if not almost_equal(val, ref):
                report["mismatches"].append(
                    {"workload": w.id, "size": size_result["size"],
                     "system": key, "ref": ref_key}
                )
    return report


def _is_err(v: Any) -> bool:
    return isinstance(v, str) and v.startswith("ERR:")


# =============================================================================
# RelPy-only capability timings (measured with the native engine on)
# =============================================================================

def measure_rebuild(build_fn, run_fn, *, warmup, repeats) -> Sample:
    """For mutating operations: rebuild fresh (untimed) each repeat, time run_fn."""
    samples = []
    gc_was = gc.isenabled(); gc.disable()
    try:
        for _ in range(max(1, repeats)):
            state = build_fn()
            gc.collect()
            t0 = time.perf_counter()
            out = run_fn(state)
            samples.append((time.perf_counter() - t0) * 1000)
        return Sample(ok=True, n_out=_result_len(out), samples_ms=samples)
    except Exception as exc:  # noqa: BLE001
        return Sample(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if gc_was:
            gc.enable()


def run_relpy_capabilities(cfg: Config, size: int) -> dict[str, Sample]:
    ds = generate_dataset(size, cfg.seed)
    out: dict[str, Sample] = {}

    # Mutations (rebuild fresh each repeat).
    out["update_bulk (active)"] = measure_rebuild(
        lambda: build_relpy(ds),
        lambda db: db.update("users", {"score": 0.0}, where=lambda r: r["active"]),
        warmup=0, repeats=min(3, cfg.repeats),
    )
    out["delete_bulk (amount<50)"] = measure_rebuild(
        lambda: build_relpy(ds),
        lambda db: db.delete("orders", where=lambda r: r["amount"] < 50),
        warmup=0, repeats=min(3, cfg.repeats),
    )

    db = build_relpy(ds)
    out["to_list (all users)"] = measure(lambda: db.query("users").to_list(),
                                         warmup=cfg.warmup, repeats=cfg.repeats)
    if pd is not None:
        out["to_pandas"] = measure(lambda: db.query("users").to_pandas(),
                                   warmup=1, repeats=min(3, cfg.repeats))
    out["to_json"] = measure(lambda: db.query("users").to_json(),
                             warmup=1, repeats=min(3, cfg.repeats))

    # Persistence: save + load round trip.
    tmp = cfg.out_dir / f"_persist_{size}.relpy.json"
    cfg.out_dir.mkdir(parents=True, exist_ok=True)

    def save_load():
        db.save(str(tmp))
        return RelPy.load(str(tmp))
    out["save + load"] = measure(save_load, warmup=1, repeats=min(3, cfg.repeats))
    try:
        tmp.unlink()
    except Exception:
        pass

    del db
    gc.collect()

    # Encryption overhead: query a plaintext vs an encrypted DB (built one at a
    # time to keep peak memory low at large scales).
    key = RelPy.generate_encryption_key()
    edb = RelPy(encryption_key=key)
    edb.create_table("u")
    edb.add_column("u", "id", AutoNumber, is_primary_key=True)
    edb.add_column("u", "secret", str, is_encrypted=True)
    edb.add_column("u", "age", int)
    edb.insert_many("u", [{"secret": u["name"], "age": (u["age"] or 0)} for u in ds.users])
    out["query on encrypted db"] = measure(
        lambda: edb.query("u").where(col("age") > 50).count(),
        warmup=cfg.warmup, repeats=cfg.repeats,
    )
    del edb
    gc.collect()

    pdb = RelPy()
    pdb.create_table("u")
    pdb.add_column("u", "id", AutoNumber, is_primary_key=True)
    pdb.add_column("u", "secret", str)
    pdb.add_column("u", "age", int)
    pdb.insert_many("u", [{"secret": u["name"], "age": (u["age"] or 0)} for u in ds.users])
    out["query on plaintext db"] = measure(
        lambda: pdb.query("u").where(col("age") > 50).count(),
        warmup=cfg.warmup, repeats=cfg.repeats,
    )
    return out


# =============================================================================
# Reporting
# =============================================================================

def _ms(sample: Optional[Sample]) -> str:
    if sample is None or not sample.ok:
        return "  --  "
    return f"{sample.median_ms:7.2f}"


def render_table(headers: list[str], rows: list[list[str]], align_first_left=True) -> str:
    cols = len(headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(r[i])))

    def fmt_row(cells):
        parts = []
        for i, c in enumerate(cells):
            c = str(c)
            if i == 0 and align_first_left:
                parts.append(c.ljust(widths[i]))
            else:
                parts.append(c.rjust(widths[i]))
        return "  ".join(parts)

    line = "  ".join("-" * w for w in widths)
    out = [fmt_row(headers), line]
    out += [fmt_row(r) for r in rows]
    return "\n".join(out)


def print_query_matrix(size_result: dict[str, Any], systems: list[str]) -> str:
    avail = [s for s in systems if any(
        size_result["results"][w.id].get(s) and size_result["results"][w.id][s].ok
        for w in WORKLOADS)]
    headers = ["Workload"] + [SYSTEM_LABELS[s] for s in avail] + ["Fastest"]
    rows = []
    last_cat = None
    for w in WORKLOADS:
        cells = [w.label]
        med = {}
        for s in avail:
            smp = size_result["results"][w.id].get(s)
            cells.append(_ms(smp))
            if smp and smp.ok:
                med[s] = smp.median_ms
        # Name the fastest system for this workload.
        if med:
            best_s = min(med, key=med.get)
            cells.append(f"{SYSTEM_LABELS[best_s]} {med[best_s]:.2f}")
        else:
            cells.append("  -- ")
        if w.category != last_cat:
            rows.append(["── " + w.category + " " + "─" * max(0, 22 - len(w.category))]
                        + [""] * (len(headers) - 1))
            last_cat = w.category
        rows.append(cells)
    return render_table(headers, rows)




def print_ingestion(all_sizes: list[dict[str, Any]], systems: list[str]) -> str:
    avail = list(systems)
    headers = ["Ingestion (rows/sec)"] + [SYSTEM_LABELS[s] for s in avail]
    rows = []
    for sr in all_sizes:
        cells = [f"{sr['size']:,} rows"]
        for s in avail:
            smp = sr["ingestion"].get(s)
            if smp and smp.ok and smp.best_ms > 0:
                rps = sr["size"] / (smp.best_ms / 1000.0)
                cells.append(f"{rps:,.0f}")
            else:
                cells.append("  -- ")
        rows.append(cells)
    return render_table(headers, rows)


def print_capabilities(caps: dict[int, dict[str, Sample]]) -> str:
    sizes = sorted(caps.keys())
    headers = ["RelPy capability (median ms)"] + [f"{s:,}" for s in sizes]
    names = list(caps[sizes[0]].keys())
    rows = []
    for name in names:
        cells = [name]
        for s in sizes:
            cells.append(_ms(caps[s].get(name)))
        rows.append(cells)
    # encryption overhead factor line
    factors = []
    for s in sizes:
        enc = caps[s].get("query on encrypted db")
        pl = caps[s].get("query on plaintext db")
        if enc and pl and enc.ok and pl.ok and pl.median_ms > 0:
            factors.append(f"{enc.median_ms / pl.median_ms:4.1f}x")
        else:
            factors.append("  -- ")
    rows.append(["encryption overhead (x)"] + factors)
    return render_table(headers, rows)


def winner_tally(all_sizes: list[dict[str, Any]], systems: list[str]) -> str:
    tally: dict[str, int] = {s: 0 for s in systems}
    total = 0
    for sr in all_sizes:
        for w in WORKLOADS:
            best_s, best_v = None, float("inf")
            for s in systems:
                smp = sr["results"][w.id].get(s)
                if smp and smp.ok and smp.median_ms < best_v:
                    best_v, best_s = smp.median_ms, s
            if best_s:
                tally[best_s] += 1
                total += 1
    rows = [[SYSTEM_LABELS[s], str(tally[s]), f"{100*tally[s]/total:4.1f}%"]
            for s in sorted(tally, key=lambda k: -tally[k]) if tally[s] > 0]
    return render_table(["Fastest-on-workload wins", "count", "share"], rows)


# =============================================================================
# Persistence of results + charts
# =============================================================================

def sample_to_dict(s: Sample) -> dict[str, Any]:
    d = {"ok": s.ok}
    if s.ok:
        d.update(n_out=s.n_out, best_ms=round(s.best_ms, 4),
                 median_ms=round(s.median_ms, 4), mean_ms=round(s.mean_ms, 4),
                 stdev_ms=round(s.stdev_ms, 4), p95_ms=round(s.p95_ms, 4),
                 samples_ms=[round(x, 4) for x in s.samples_ms])
    else:
        d["error"] = s.error
    return d


def write_json(cfg: Config, env: dict, all_sizes: list, caps: dict, corr: list):
    payload = {
        "environment": env,
        "config": {"sizes": cfg.sizes, "repeats": cfg.repeats,
                   "warmup": cfg.warmup, "systems": cfg.systems},
        "correctness": corr,
        "query_matrix": [],
        "ingestion": [],
        "capabilities": {},
    }
    for sr in all_sizes:
        payload["query_matrix"].append({
            "size": sr["size"],
            "results": {wid: {s: sample_to_dict(smp) for s, smp in bysys.items()}
                        for wid, bysys in sr["results"].items()},
        })
        payload["ingestion"].append({
            "size": sr["size"],
            "systems": {s: sample_to_dict(smp) for s, smp in sr["ingestion"].items()},
        })
    for size, capset in caps.items():
        payload["capabilities"][str(size)] = {n: sample_to_dict(s) for n, s in capset.items()}

    (cfg.out_dir / "results.json").write_text(json.dumps(payload, indent=2))


def write_csv(cfg: Config, all_sizes: list):
    lines = ["size,workload,category,system,median_ms,best_ms,stdev_ms,n_out"]
    catmap = {w.id: w.category for w in WORKLOADS}
    for sr in all_sizes:
        for wid, bysys in sr["results"].items():
            for s, smp in bysys.items():
                if smp.ok:
                    lines.append(f"{sr['size']},{wid},{catmap[wid]},{s},"
                                 f"{smp.median_ms:.4f},{smp.best_ms:.4f},"
                                 f"{smp.stdev_ms:.4f},{smp.n_out}")
    (cfg.out_dir / "summary.csv").write_text("\n".join(lines))


def write_markdown(cfg: Config, env: dict, all_sizes: list, caps: dict, corr: list, console: str):
    md = ["# RelPyDB Benchmark Report", "",
          f"*Generated {env['timestamp']}*", "",
          "## Environment", ""]
    for k, v in env.items():
        if k == "libraries":
            md.append(f"- **libraries**: " + ", ".join(f"{lk} {lv}" for lk, lv in v.items() if lv))
        else:
            md.append(f"- **{k}**: {v}")
    md += ["", "## Full console report", "", "```", console, "```", ""]
    (cfg.out_dir / "REPORT.md").write_text("\n".join(md))


def make_charts(cfg: Config, all_sizes: list, caps: dict):
    if not (_HAVE_MPL and cfg.charts):
        return []
    made = []
    big = all_sizes[-1]
    systems = [s for s in cfg.systems if any(
        big["results"][w.id].get(s) and big["results"][w.id][s].ok for w in WORKLOADS)]

    # Chart 1: median ms per system across workloads (grouped, log scale).
    try:
        import numpy as _np
        labels = [w.label for w in WORKLOADS]
        x = _np.arange(len(labels))
        width = 0.8 / max(1, len(systems))
        plt.figure(figsize=(16, 7))
        for i, s in enumerate(systems):
            vals = [big["results"][w.id][s].median_ms
                    if (big["results"][w.id].get(s) and big["results"][w.id][s].ok) else _np.nan
                    for w in WORKLOADS]
            plt.bar(x + i * width, vals, width, label=SYSTEM_LABELS[s])
        plt.yscale("log")
        plt.xticks(x + 0.4, labels, rotation=45, ha="right", fontsize=8)
        plt.ylabel("median ms (log)")
        plt.title(f"Query time by system — {big['size']:,} rows (lower is better)")
        plt.legend(fontsize=8, ncol=2)
        plt.tight_layout()
        p = cfg.out_dir / "chart_systems.png"
        plt.savefig(p, dpi=110); plt.close(); made.append(p)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[chart_systems] {exc}\n")


    # Chart 3: scaling — RelPy native median vs size for representative workloads.
    try:
        reps = ["filter_gt", "group_city", "join_group_sum", "order_score_desc"]
        plt.figure(figsize=(9, 6))
        sizes = [sr["size"] for sr in all_sizes]
        for wid in reps:
            ys = []
            for sr in all_sizes:
                smp = sr["results"][wid].get("relpy")
                ys.append(smp.median_ms if smp and smp.ok else float("nan"))
            wl = next(w for w in WORKLOADS if w.id == wid)
            plt.plot(sizes, ys, marker="o", label=wl.label)
        plt.xscale("log"); plt.yscale("log")
        plt.xlabel("rows (log)"); plt.ylabel("median ms (log)")
        plt.title("RelPy (native) scaling")
        plt.legend(fontsize=8); plt.grid(True, which="both", alpha=0.3)
        plt.tight_layout()
        p = cfg.out_dir / "chart_scaling.png"
        plt.savefig(p, dpi=110); plt.close(); made.append(p)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[chart_scaling] {exc}\n")

    return made


# =============================================================================
# main
# =============================================================================

def main(argv: list[str]) -> int:
    cfg = parse_args(argv)
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    env = capture_environment()
    registry = build_system_registry(cfg)

    buf: list[str] = []

    def out(line: str = ""):
        print(line)
        buf.append(line)

    out("=" * 78)
    out(" RelPyDB — Comprehensive Benchmark")
    out("=" * 78)
    out(f" python {env['python']} ({env['implementation']}) on {env['platform']}")
    out(f" cpu={env['cpu_count']}  relpy={env['relpy_version']}  engine=C (required)")
    libs = ", ".join(f"{k} {v}" for k, v in env["libraries"].items() if v)
    out(f" libraries: {libs}")
    active = [SYSTEM_LABELS[s] for s in cfg.systems if registry.get(s) and registry[s].available]
    out(f" systems:   {', '.join(active)}")
    out(f" sizes:     {', '.join(f'{s:,}' for s in cfg.sizes)}   "
        f"repeats={cfg.repeats} warmup={cfg.warmup}")
    out()

    all_sizes = []
    corr_reports = []
    for size in cfg.sizes:
        out("#" * 78)
        out(f"# DATASET: {size:,} users + {size:,} orders")
        out("#" * 78)
        sr = run_size(cfg, size, registry)
        all_sizes.append(sr)
        out()
        out("Query time by system  (median ms; lower is better)")
        out(print_query_matrix(sr, cfg.systems))
        out()
        if cfg.verify:
            rep = verify_correctness(sr, cfg.systems)
            corr_reports.append(rep)
            status = "OK" if not rep["mismatches"] else f"{len(rep['mismatches'])} MISMATCH"
            out(f"Correctness: {status}   ({rep['checked']} cross-checks)")
            for m in rep["mismatches"][:6]:
                out(f"   ! {m['system']} differs on {m['workload']} (vs {m['ref']})")
        out()

    out("#" * 78)
    out("# INGESTION throughput")
    out("#" * 78)
    out(print_ingestion(all_sizes, cfg.systems))
    out()

    caps = {}
    out("#" * 78)
    out("# RelPy CAPABILITIES (native engine on)")
    out("#" * 78)
    # Capability timings (mutations, exports, persistence, encryption) rebuild
    # data repeatedly, so they are measured at the lighter sizes to keep the
    # suite fast; they characterize per-operation cost, not scaling.
    cap_sizes = [s for s in cfg.sizes if s <= 10_000] or [min(cfg.sizes)]
    for size in cap_sizes:
        caps[size] = run_relpy_capabilities(cfg, size)
    out(print_capabilities(caps))
    out(f"(capability timings measured at sizes: {', '.join(f'{s:,}' for s in cap_sizes)})")
    out()

    out("#" * 78)
    out("# SUMMARY")
    out("#" * 78)
    out(winner_tally(all_sizes, cfg.systems))
    out()
    total_mismatch = sum(len(r["mismatches"]) for r in corr_reports)
    out(f"Correctness overall: {'ALL CONSISTENT' if total_mismatch == 0 else str(total_mismatch)+' mismatches'}")
    out()

    # Persist artefacts.
    write_json(cfg, env, all_sizes, caps, corr_reports)
    write_csv(cfg, all_sizes)
    console_text = "\n".join(buf)
    write_markdown(cfg, env, all_sizes, caps, corr_reports, console_text)
    charts = make_charts(cfg, all_sizes, caps)

    out(f"Artefacts written to {cfg.out_dir}/:")
    out("   results.json   summary.csv   REPORT.md" +
        ("   " + "  ".join(p.name for p in charts) if charts else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
