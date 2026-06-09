import math
import random
import time
from pathlib import Path
from typing import Any, Callable

import duckdb

from sqlalchemy import create_engine, text

from relpy import (
    RelPy,
    AutoNumber,
    col,
    count,
    sum_,
    avg,
    max_,
)


# =============================================================================
# Configuration
# =============================================================================

RANDOM_SEED = 42

ORDER_COUNT = 200
CUSTOMER_COUNT = 200
SUPPORT_TICKET_COUNT = 250

QUERY_ITERATIONS = 30

FULFILLED_STATUSES = ("paid", "shipped", "delivered")
RISK_STATUSES = ("pending", "cancelled", "refunded")

PERSISTENCE_FILE_PATH = Path("benchmark_database.relpy.json")


# =============================================================================
# Timing helpers
# =============================================================================

def now() -> float:
    return time.perf_counter()


def ms(seconds: float) -> float:
    return seconds * 1000


def format_optional_ms(value: float | None) -> str:
    if value is None:
        return "ERR"

    return f"{value:.3f}"


def benchmark(
    label: str,
    func: Callable[[], Any],
    iterations: int,
) -> tuple[Any, float]:
    """
    Runs a function multiple times and returns:
        last_result, total_seconds
    """

    last_result = func()

    start = now()

    for _ in range(iterations):
        last_result = func()

    end = now()

    total = end - start

    print(
        f"{label:<24} "
        f"total={ms(total):>10.3f} ms | "
        f"avg={ms(total / iterations):>8.3f} ms"
    )

    return last_result, total


# =============================================================================
# Data generation
# =============================================================================

def generate_data() -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(RANDOM_SEED)

    regions = [
        {"id": 1, "name": "North"},
        {"id": 2, "name": "South"},
        {"id": 3, "name": "East"},
        {"id": 4, "name": "West"},
        {"id": 5, "name": "Central"},
        {"id": 6, "name": "Coastal"},
        {"id": 7, "name": "Mountain"},
        {"id": 8, "name": "International"},
    ]

    categories = [
        {"id": 1, "name": "Electronics"},
        {"id": 2, "name": "Office"},
        {"id": 3, "name": "Furniture"},
        {"id": 4, "name": "Books"},
        {"id": 5, "name": "Gaming"},
        {"id": 6, "name": "Home"},
        {"id": 7, "name": "Sports"},
        {"id": 8, "name": "Garden"},
        {"id": 9, "name": "Fashion"},
        {"id": 10, "name": "Health"},
        {"id": 11, "name": "Automotive"},
        {"id": 12, "name": "Music"},
    ]

    suppliers = []

    for supplier_id in range(1, 41):
        suppliers.append({
            "id": supplier_id,
            "name": f"Supplier {supplier_id}",
            "region_id": rng.choice(regions)["id"],
            "rating": rng.randint(1, 5),
        })

    sales_reps = []

    for rep_id in range(1, 41):
        sales_reps.append({
            "id": rep_id,
            "name": f"Rep {rep_id}",
            "region_id": rng.choice(regions)["id"],
            "level": rng.choice(["junior", "mid", "senior", "lead"]),
        })

    customers = []

    for customer_id in range(1, CUSTOMER_COUNT + 1):
        customers.append({
            "id": customer_id,
            "name": f"Customer {customer_id}",
            "region_id": rng.choice(regions)["id"],
            "tier": rng.choice(["bronze", "silver", "gold", "platinum"]),
            "status": rng.choices(["active", "inactive"], weights=[85, 15])[0],
            "signup_month": f"2025-{rng.randint(1, 12):02d}",
        })

    products = []

    for product_id in range(1, 121):
        price = round(rng.uniform(10, 1200), 2)
        cost = round(price * rng.uniform(0.35, 0.75), 2)

        products.append({
            "id": product_id,
            "name": f"Product {product_id}",
            "category_id": rng.choice(categories)["id"],
            "supplier_id": rng.choice(suppliers)["id"],
            "price": price,
            "cost": cost,
        })

    orders = []
    order_statuses = ["paid", "shipped", "delivered", "pending", "cancelled", "refunded"]
    order_channels = ["website", "mobile", "phone", "partner"]

    for order_id in range(1, ORDER_COUNT + 1):
        customer = rng.choice(customers)

        orders.append({
            "id": order_id,
            "customer_id": customer["id"],
            "sales_rep_id": rng.choice(sales_reps)["id"],
            "order_month": f"2026-{rng.randint(1, 12):02d}",
            "status": rng.choices(
                order_statuses,
                weights=[35, 20, 20, 12, 8, 5],
            )[0],
            "channel": rng.choice(order_channels),
        })

    order_items = []
    order_item_id = 1
    order_totals: dict[int, float] = {}

    for order in orders:
        item_count = rng.randint(1, 5)
        order_total = 0.0

        chosen_products = rng.sample(products, item_count)

        for product in chosen_products:
            quantity = rng.randint(1, 5)
            discount = rng.choice([0.0, 0.05, 0.10, 0.15, 0.20])
            line_total = round(quantity * product["price"] * (1 - discount), 2)

            order_items.append({
                "id": order_item_id,
                "order_id": order["id"],
                "product_id": product["id"],
                "quantity": quantity,
                "unit_price": product["price"],
                "discount": discount,
                "line_total": line_total,
            })

            order_total += line_total
            order_item_id += 1

        order_totals[order["id"]] = round(order_total, 2)

    payments = []

    for payment_id, order in enumerate(orders, start=1):
        if order["status"] in FULFILLED_STATUSES:
            payment_status = "settled"
        elif order["status"] == "refunded":
            payment_status = "refunded"
        elif order["status"] == "cancelled":
            payment_status = "cancelled"
        else:
            payment_status = rng.choice(["pending", "failed"])

        payments.append({
            "id": payment_id,
            "order_id": order["id"],
            "amount": order_totals[order["id"]],
            "payment_method": rng.choice(["credit_card", "paypal", "bank_transfer", "cash"]),
            "status": payment_status,
        })

    support_tickets = []
    ticket_statuses = ["open", "closed", "escalated"]
    priorities = ["low", "medium", "high", "critical"]

    for ticket_id in range(1, SUPPORT_TICKET_COUNT + 1):
        support_tickets.append({
            "id": ticket_id,
            "customer_id": rng.choice(customers)["id"],
            "product_id": rng.choice(products)["id"],
            "ticket_month": f"2026-{rng.randint(1, 12):02d}",
            "status": rng.choices(ticket_statuses, weights=[35, 50, 15])[0],
            "priority": rng.choice(priorities),
            "priority_score": rng.randint(1, 4),
            "satisfaction": rng.randint(1, 5),
        })

    return {
        "regions": regions,
        "customers": customers,
        "sales_reps": sales_reps,
        "categories": categories,
        "suppliers": suppliers,
        "products": products,
        "orders": orders,
        "order_items": order_items,
        "payments": payments,
        "support_tickets": support_tickets,
    }


# =============================================================================
# SQL schema
# =============================================================================

CREATE_TABLES_SQL = [
    """
    CREATE TABLE regions (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE customers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        region_id INTEGER NOT NULL,
        tier TEXT NOT NULL,
        status TEXT NOT NULL,
        signup_month TEXT NOT NULL,
        FOREIGN KEY (region_id) REFERENCES regions(id)
    )
    """,
    """
    CREATE TABLE sales_reps (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        region_id INTEGER NOT NULL,
        level TEXT NOT NULL,
        FOREIGN KEY (region_id) REFERENCES regions(id)
    )
    """,
    """
    CREATE TABLE categories (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE suppliers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        region_id INTEGER NOT NULL,
        rating INTEGER NOT NULL,
        FOREIGN KEY (region_id) REFERENCES regions(id)
    )
    """,
    """
    CREATE TABLE products (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        category_id INTEGER NOT NULL,
        supplier_id INTEGER NOT NULL,
        price DOUBLE NOT NULL,
        cost DOUBLE NOT NULL,
        FOREIGN KEY (category_id) REFERENCES categories(id),
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
    )
    """,
    """
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL,
        sales_rep_id INTEGER NOT NULL,
        order_month TEXT NOT NULL,
        status TEXT NOT NULL,
        channel TEXT NOT NULL,
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (sales_rep_id) REFERENCES sales_reps(id)
    )
    """,
    """
    CREATE TABLE order_items (
        id INTEGER PRIMARY KEY,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price DOUBLE NOT NULL,
        discount DOUBLE NOT NULL,
        line_total DOUBLE NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (product_id) REFERENCES products(id)
    )
    """,
    """
    CREATE TABLE payments (
        id INTEGER PRIMARY KEY,
        order_id INTEGER NOT NULL,
        amount DOUBLE NOT NULL,
        payment_method TEXT NOT NULL,
        status TEXT NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders(id)
    )
    """,
    """
    CREATE TABLE support_tickets (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        ticket_month TEXT NOT NULL,
        status TEXT NOT NULL,
        priority TEXT NOT NULL,
        priority_score INTEGER NOT NULL,
        satisfaction INTEGER NOT NULL,
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (product_id) REFERENCES products(id)
    )
    """,
]


CREATE_INDEXES_SQL = [
    "CREATE INDEX idx_customers_region_status ON customers(region_id, status)",
    "CREATE INDEX idx_orders_status ON orders(status)",
    "CREATE INDEX idx_orders_customer_status ON orders(customer_id, status)",
    "CREATE INDEX idx_orders_sales_rep_status ON orders(sales_rep_id, status)",
    "CREATE INDEX idx_order_items_order ON order_items(order_id)",
    "CREATE INDEX idx_order_items_product ON order_items(product_id)",
    "CREATE INDEX idx_payments_order_status ON payments(order_id, status)",
    "CREATE INDEX idx_products_category_supplier ON products(category_id, supplier_id)",
    "CREATE INDEX idx_tickets_product_status ON support_tickets(product_id, status)",
    "CREATE INDEX idx_tickets_customer_status ON support_tickets(customer_id, status)",
]


INSERT_SQL = {
    "regions": """
        INSERT INTO regions (id, name)
        VALUES (:id, :name)
    """,
    "customers": """
        INSERT INTO customers (id, name, region_id, tier, status, signup_month)
        VALUES (:id, :name, :region_id, :tier, :status, :signup_month)
    """,
    "sales_reps": """
        INSERT INTO sales_reps (id, name, region_id, level)
        VALUES (:id, :name, :region_id, :level)
    """,
    "categories": """
        INSERT INTO categories (id, name)
        VALUES (:id, :name)
    """,
    "suppliers": """
        INSERT INTO suppliers (id, name, region_id, rating)
        VALUES (:id, :name, :region_id, :rating)
    """,
    "products": """
        INSERT INTO products (id, name, category_id, supplier_id, price, cost)
        VALUES (:id, :name, :category_id, :supplier_id, :price, :cost)
    """,
    "orders": """
        INSERT INTO orders (id, customer_id, sales_rep_id, order_month, status, channel)
        VALUES (:id, :customer_id, :sales_rep_id, :order_month, :status, :channel)
    """,
    "order_items": """
        INSERT INTO order_items (id, order_id, product_id, quantity, unit_price, discount, line_total)
        VALUES (:id, :order_id, :product_id, :quantity, :unit_price, :discount, :line_total)
    """,
    "payments": """
        INSERT INTO payments (id, order_id, amount, payment_method, status)
        VALUES (:id, :order_id, :amount, :payment_method, :status)
    """,
    "support_tickets": """
        INSERT INTO support_tickets (
            id, customer_id, product_id, ticket_month, status,
            priority, priority_score, satisfaction
        )
        VALUES (
            :id, :customer_id, :product_id, :ticket_month, :status,
            :priority, :priority_score, :satisfaction
        )
    """,
}


# =============================================================================
# SQL queries
# =============================================================================

SQL_QUERIES = {
    "q01_revenue_by_region_category": """
        SELECT
            r.name AS region,
            c.name AS category,
            COUNT(*) AS line_count,
            SUM(oi.line_total) AS revenue,
            AVG(oi.quantity) AS avg_quantity,
            MAX(p.price) AS max_price
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        JOIN categories c ON c.id = p.category_id
        JOIN customers cu ON cu.id = o.customer_id
        JOIN regions r ON r.id = cu.region_id
        WHERE o.status IN ('paid', 'shipped', 'delivered')
        GROUP BY r.name, c.name
        HAVING SUM(oi.line_total) >= 2000
        ORDER BY revenue DESC, region ASC, category ASC
        LIMIT 20
    """,
    "q02_top_customers_by_paid_amount": """
        SELECT
            cu.id AS customer_id,
            cu.name AS customer_name,
            cu.tier AS tier,
            COUNT(*) AS paid_order_count,
            SUM(pa.amount) AS paid_amount,
            AVG(pa.amount) AS avg_order_amount
        FROM customers cu
        JOIN orders o ON o.customer_id = cu.id
        JOIN payments pa ON pa.order_id = o.id
        WHERE pa.status = 'settled'
        GROUP BY cu.id, cu.name, cu.tier
        HAVING SUM(pa.amount) >= 1000
        ORDER BY paid_amount DESC, customer_id ASC
        LIMIT 15
    """,
    "q03_supplier_category_performance": """
        SELECT
            s.name AS supplier,
            c.name AS category,
            COUNT(*) AS line_count,
            SUM(oi.line_total) AS revenue,
            AVG(oi.unit_price) AS avg_unit_price,
            MAX(p.price) AS max_price
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        JOIN suppliers s ON s.id = p.supplier_id
        JOIN categories c ON c.id = p.category_id
        WHERE o.status IN ('paid', 'shipped', 'delivered')
        GROUP BY s.name, c.name
        HAVING SUM(oi.line_total) >= 1500
        ORDER BY revenue DESC, supplier ASC, category ASC
        LIMIT 20
    """,
    "q04_sales_rep_monthly_performance": """
        SELECT
            sr.name AS sales_rep,
            o.order_month AS month,
            COUNT(*) AS order_count,
            SUM(pa.amount) AS revenue,
            AVG(pa.amount) AS avg_order_amount
        FROM orders o
        JOIN sales_reps sr ON sr.id = o.sales_rep_id
        JOIN payments pa ON pa.order_id = o.id
        WHERE pa.status = 'settled'
        GROUP BY sr.name, o.order_month
        HAVING SUM(pa.amount) >= 1000
        ORDER BY revenue DESC, sales_rep ASC, month ASC
        LIMIT 20
    """,
    "q05_payment_method_stats": """
        SELECT
            pa.payment_method AS payment_method,
            pa.status AS payment_status,
            COUNT(*) AS payment_count,
            SUM(pa.amount) AS total_amount,
            AVG(pa.amount) AS avg_amount,
            MAX(pa.amount) AS max_amount
        FROM payments pa
        GROUP BY pa.payment_method, pa.status
        HAVING COUNT(*) >= 2
        ORDER BY total_amount DESC, payment_method ASC, payment_status ASC
    """,
    "q06_support_tickets_by_category_status": """
        SELECT
            c.name AS category,
            st.status AS ticket_status,
            COUNT(*) AS ticket_count,
            AVG(st.priority_score) AS avg_priority_score,
            AVG(st.satisfaction) AS avg_satisfaction
        FROM support_tickets st
        JOIN products p ON p.id = st.product_id
        JOIN categories c ON c.id = p.category_id
        GROUP BY c.name, st.status
        HAVING COUNT(*) >= 3
        ORDER BY ticket_count DESC, category ASC, ticket_status ASC
        LIMIT 20
    """,
    "q07_active_customer_revenue_by_region_tier": """
        SELECT
            r.name AS region,
            cu.tier AS tier,
            COUNT(*) AS order_count,
            SUM(pa.amount) AS revenue,
            AVG(pa.amount) AS avg_order_amount
        FROM customers cu
        JOIN regions r ON r.id = cu.region_id
        JOIN orders o ON o.customer_id = cu.id
        JOIN payments pa ON pa.order_id = o.id
        WHERE cu.status = 'active'
          AND pa.status = 'settled'
        GROUP BY r.name, cu.tier
        HAVING SUM(pa.amount) >= 2000
        ORDER BY revenue DESC, region ASC, tier ASC
        LIMIT 20
    """,
    "q08_monthly_category_revenue": """
        SELECT
            o.order_month AS month,
            c.name AS category,
            COUNT(*) AS line_count,
            SUM(oi.line_total) AS revenue,
            AVG(oi.quantity) AS avg_quantity
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        JOIN categories c ON c.id = p.category_id
        WHERE o.status IN ('paid', 'shipped', 'delivered')
        GROUP BY o.order_month, c.name
        HAVING SUM(oi.line_total) >= 1500
        ORDER BY month ASC, revenue DESC, category ASC
        LIMIT 30
    """,
    "q09_product_demand": """
        SELECT
            p.name AS product,
            c.name AS category,
            SUM(oi.quantity) AS units,
            SUM(oi.line_total) AS revenue,
            AVG(oi.discount) AS avg_discount
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        JOIN categories c ON c.id = p.category_id
        JOIN orders o ON o.id = oi.order_id
        WHERE o.status IN ('paid', 'shipped', 'delivered')
        GROUP BY p.name, c.name
        HAVING SUM(oi.quantity) >= 5
        ORDER BY units DESC, revenue DESC, product ASC
        LIMIT 20
    """,
    "q10_risk_orders_by_region_tier_status": """
        SELECT
            r.name AS region,
            cu.tier AS tier,
            o.status AS order_status,
            COUNT(*) AS order_count,
            SUM(pa.amount) AS at_risk_amount,
            AVG(pa.amount) AS avg_risk_amount
        FROM orders o
        JOIN customers cu ON cu.id = o.customer_id
        JOIN regions r ON r.id = cu.region_id
        JOIN payments pa ON pa.order_id = o.id
        WHERE o.status IN ('pending', 'cancelled', 'refunded')
        GROUP BY r.name, cu.tier, o.status
        HAVING COUNT(*) >= 1
        ORDER BY at_risk_amount DESC, region ASC, tier ASC, order_status ASC
        LIMIT 20
    """,
}


# =============================================================================
# SQLAlchemy SQLite
# =============================================================================

def build_sqlalchemy_db(data: dict[str, list[dict[str, Any]]]):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    with engine.begin() as conn:
        for statement in CREATE_TABLES_SQL:
            conn.execute(text(statement))

        for table_name, rows in data.items():
            if rows:
                conn.execute(text(INSERT_SQL[table_name]), rows)

        for statement in CREATE_INDEXES_SQL:
            conn.execute(text(statement))

    return engine


def run_sqlalchemy_query(engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(text(sql)).mappings().all()
        ]


# =============================================================================
# DuckDB
# =============================================================================

def _duckdb_insert_statement(table_name: str, row: dict[str, Any]) -> str:
    columns = list(row.keys())
    placeholders = ", ".join(["?"] * len(columns))
    column_sql = ", ".join(columns)

    return f"INSERT INTO {table_name} ({column_sql}) VALUES ({placeholders})"


def build_duckdb(data: dict[str, list[dict[str, Any]]]):
    conn = duckdb.connect(database=":memory:")

    for statement in CREATE_TABLES_SQL:
        conn.execute(statement)

    for table_name, rows in data.items():
        if not rows:
            continue

        statement = _duckdb_insert_statement(table_name, rows[0])
        values = [
            tuple(row[column_name] for column_name in rows[0].keys())
            for row in rows
        ]

        conn.executemany(statement, values)

    for statement in CREATE_INDEXES_SQL:
        try:
            conn.execute(statement)
        except Exception:
            pass

    return conn


def run_duckdb_query(conn, sql: str) -> list[dict[str, Any]]:
    cursor = conn.execute(sql)
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()

    return [
        dict(zip(columns, row))
        for row in rows
    ]


# =============================================================================
# RelPy
# =============================================================================

def build_relpy(data: dict[str, list[dict[str, Any]]]) -> RelPy:
    db = RelPy()

    db.create_table("regions")
    db.add_column("regions", "id", AutoNumber, is_primary_key=True)
    db.add_column("regions", "name", str, nullable=False)

    db.create_table("customers")
    db.add_column("customers", "id", AutoNumber, is_primary_key=True)
    db.add_column("customers", "name", str, nullable=False)
    db.add_column("customers", "region_id", int, nullable=False, references="regions.id")
    db.add_column("customers", "tier", str, nullable=False)
    db.add_column("customers", "status", str, nullable=False)
    db.add_column("customers", "signup_month", str, nullable=False)

    db.create_table("sales_reps")
    db.add_column("sales_reps", "id", AutoNumber, is_primary_key=True)
    db.add_column("sales_reps", "name", str, nullable=False)
    db.add_column("sales_reps", "region_id", int, nullable=False, references="regions.id")
    db.add_column("sales_reps", "level", str, nullable=False)

    db.create_table("categories")
    db.add_column("categories", "id", AutoNumber, is_primary_key=True)
    db.add_column("categories", "name", str, nullable=False)

    db.create_table("suppliers")
    db.add_column("suppliers", "id", AutoNumber, is_primary_key=True)
    db.add_column("suppliers", "name", str, nullable=False)
    db.add_column("suppliers", "region_id", int, nullable=False, references="regions.id")
    db.add_column("suppliers", "rating", int, nullable=False)

    db.create_table("products")
    db.add_column("products", "id", AutoNumber, is_primary_key=True)
    db.add_column("products", "name", str, nullable=False)
    db.add_column("products", "category_id", int, nullable=False, references="categories.id")
    db.add_column("products", "supplier_id", int, nullable=False, references="suppliers.id")
    db.add_column("products", "price", float, nullable=False)
    db.add_column("products", "cost", float, nullable=False)

    db.create_table("orders")
    db.add_column("orders", "id", AutoNumber, is_primary_key=True)
    db.add_column("orders", "customer_id", int, nullable=False, references="customers.id")
    db.add_column("orders", "sales_rep_id", int, nullable=False, references="sales_reps.id")
    db.add_column("orders", "order_month", str, nullable=False)
    db.add_column("orders", "status", str, nullable=False)
    db.add_column("orders", "channel", str, nullable=False)

    db.create_table("order_items")
    db.add_column("order_items", "id", AutoNumber, is_primary_key=True)
    db.add_column("order_items", "order_id", int, nullable=False, references="orders.id")
    db.add_column("order_items", "product_id", int, nullable=False, references="products.id")
    db.add_column("order_items", "quantity", int, nullable=False)
    db.add_column("order_items", "unit_price", float, nullable=False)
    db.add_column("order_items", "discount", float, nullable=False)
    db.add_column("order_items", "line_total", float, nullable=False)

    db.create_table("payments")
    db.add_column("payments", "id", AutoNumber, is_primary_key=True)
    db.add_column("payments", "order_id", int, nullable=False, references="orders.id")
    db.add_column("payments", "amount", float, nullable=False)
    db.add_column("payments", "payment_method", str, nullable=False)
    db.add_column("payments", "status", str, nullable=False)

    db.create_table("support_tickets")
    db.add_column("support_tickets", "id", AutoNumber, is_primary_key=True)
    db.add_column("support_tickets", "customer_id", int, nullable=False, references="customers.id")
    db.add_column("support_tickets", "product_id", int, nullable=False, references="products.id")
    db.add_column("support_tickets", "ticket_month", str, nullable=False)
    db.add_column("support_tickets", "status", str, nullable=False)
    db.add_column("support_tickets", "priority", str, nullable=False)
    db.add_column("support_tickets", "priority_score", int, nullable=False)
    db.add_column("support_tickets", "satisfaction", int, nullable=False)

    insert_order = [
        "regions",
        "customers",
        "sales_reps",
        "categories",
        "suppliers",
        "products",
        "orders",
        "order_items",
        "payments",
        "support_tickets",
    ]

    for table_name in insert_order:
        for row in data[table_name]:
            row_without_id = {
                key: value
                for key, value in row.items()
                if key != "id"
            }

            db.insert(table_name, row_without_id)

    db.create_index("customers", ["region_id", "status"])
    db.create_index("orders", "status")
    db.create_index("orders", ["customer_id", "status"])
    db.create_index("orders", ["sales_rep_id", "status"])
    db.create_index("order_items", "order_id")
    db.create_index("order_items", "product_id")
    db.create_index("payments", ["order_id", "status"])
    db.create_index("products", ["category_id", "supplier_id"])
    db.create_index("support_tickets", ["product_id", "status"])
    db.create_index("support_tickets", ["customer_id", "status"])

    return db


def save_and_load_relpy_db(
    db: RelPy,
    file_path: str | Path,
) -> tuple[RelPy, float, float, int]:
    """
    Saves a RelPy database to local storage and loads it back.

    Returns:
        loaded_db, save_seconds, load_seconds, file_size_bytes
    """

    path = Path(file_path)

    if path.exists():
        path.unlink()

    start = now()
    db.save(path)
    save_time = now() - start

    file_size_bytes = path.stat().st_size

    start = now()
    loaded_db = RelPy.load(path)
    load_time = now() - start

    return loaded_db, save_time, load_time, file_size_bytes


def run_persistence_mutation_check(db: RelPy) -> None:
    """
    Verifies that a loaded RelPy database is not only readable,
    but can still accept new rows, maintain AutoNumber sequences,
    use foreign keys, and use indexes.
    """

    initial_count = db.query("support_tickets").count()

    inserted = db.insert("support_tickets", {
        "customer_id": 1,
        "product_id": 1,
        "ticket_month": "2026-12",
        "status": "open",
        "priority": "critical",
        "priority_score": 4,
        "satisfaction": 5,
    })

    new_count = db.query("support_tickets").count()

    if new_count != initial_count + 1:
        raise AssertionError(
            "Loaded DB mutation check failed: support_tickets count did not increase by 1."
        )

    if inserted["id"] <= SUPPORT_TICKET_COUNT:
        raise AssertionError(
            "Loaded DB mutation check failed: AutoNumber sequence was not restored correctly."
        )

    matching_rows = (
        db.query("support_tickets")
          .where(
              (col("customer_id") == 1) &
              (col("status") == "open")
          )
          .to_list()
    )

    if not any(row["id"] == inserted["id"] for row in matching_rows):
        raise AssertionError(
            "Loaded DB mutation check failed: inserted row was not found by indexed query."
        )

    if "__relpy_row_id__" in inserted:
        raise AssertionError(
            "Loaded DB mutation check failed: internal row id leaked from insert()."
        )

    for row in matching_rows:
        if "__relpy_row_id__" in row:
            raise AssertionError(
                "Loaded DB mutation check failed: internal row id leaked from query output."
            )


def relpy_queries(db: RelPy) -> dict[str, Callable[[], list[dict[str, Any]]]]:
    return {
        "q01_revenue_by_region_category": lambda: (
            db.query("orders")
              .join("order_items")
              .join("products")
              .join("categories")
              .join("customers")
              .join("regions")
              .where(col("orders.status").in_(FULFILLED_STATUSES))
              .group_by("regions.name", "categories.name")
              .aggregate(
                  line_count=count(),
                  revenue=sum_("order_items.line_total"),
                  avg_quantity=avg("order_items.quantity"),
                  max_price=max_("products.price"),
              )
              .having(col("revenue") >= 2000)
              .order_by(("revenue", "desc"), ("regions.name", "asc"), ("categories.name", "asc"))
              .limit(20)
              .to_list()
        ),
        "q02_top_customers_by_paid_amount": lambda: (
            db.query("customers")
              .join("orders")
              .join("payments")
              .where(col("payments.status") == "settled")
              .group_by("customers.id", "customers.name", "customers.tier")
              .aggregate(
                  paid_order_count=count(),
                  paid_amount=sum_("payments.amount"),
                  avg_order_amount=avg("payments.amount"),
              )
              .having(col("paid_amount") >= 1000)
              .order_by(("paid_amount", "desc"), ("customers.id", "asc"))
              .limit(15)
              .to_list()
        ),
        "q03_supplier_category_performance": lambda: (
            db.query("orders")
              .join("order_items")
              .join("products")
              .join("suppliers")
              .join("categories")
              .where(col("orders.status").in_(FULFILLED_STATUSES))
              .group_by("suppliers.name", "categories.name")
              .aggregate(
                  line_count=count(),
                  revenue=sum_("order_items.line_total"),
                  avg_unit_price=avg("order_items.unit_price"),
                  max_price=max_("products.price"),
              )
              .having(col("revenue") >= 1500)
              .order_by(("revenue", "desc"), ("suppliers.name", "asc"), ("categories.name", "asc"))
              .limit(20)
              .to_list()
        ),
        "q04_sales_rep_monthly_performance": lambda: (
            db.query("orders")
              .join("sales_reps")
              .join("payments")
              .where(col("payments.status") == "settled")
              .group_by("sales_reps.name", "orders.order_month")
              .aggregate(
                  order_count=count(),
                  revenue=sum_("payments.amount"),
                  avg_order_amount=avg("payments.amount"),
              )
              .having(col("revenue") >= 1000)
              .order_by(("revenue", "desc"), ("sales_reps.name", "asc"), ("orders.order_month", "asc"))
              .limit(20)
              .to_list()
        ),
        "q05_payment_method_stats": lambda: (
            db.query("payments")
              .group_by("payment_method", "status")
              .aggregate(
                  payment_count=count(),
                  total_amount=sum_("amount"),
                  avg_amount=avg("amount"),
                  max_amount=max_("amount"),
              )
              .having(col("payment_count") >= 2)
              .order_by(("total_amount", "desc"), ("payment_method", "asc"), ("status", "asc"))
              .to_list()
        ),
        "q06_support_tickets_by_category_status": lambda: (
            db.query("support_tickets")
              .join("products")
              .join("categories")
              .group_by("categories.name", "support_tickets.status")
              .aggregate(
                  ticket_count=count(),
                  avg_priority_score=avg("support_tickets.priority_score"),
                  avg_satisfaction=avg("support_tickets.satisfaction"),
              )
              .having(col("ticket_count") >= 3)
              .order_by(("ticket_count", "desc"), ("categories.name", "asc"), ("support_tickets.status", "asc"))
              .limit(20)
              .to_list()
        ),
        "q07_active_customer_revenue_by_region_tier": lambda: (
            db.query("customers")
              .join("regions")
              .join("orders")
              .join("payments")
              .where(
                  (col("customers.status") == "active") &
                  (col("payments.status") == "settled")
              )
              .group_by("regions.name", "customers.tier")
              .aggregate(
                  order_count=count(),
                  revenue=sum_("payments.amount"),
                  avg_order_amount=avg("payments.amount"),
              )
              .having(col("revenue") >= 2000)
              .order_by(("revenue", "desc"), ("regions.name", "asc"), ("customers.tier", "asc"))
              .limit(20)
              .to_list()
        ),
        "q08_monthly_category_revenue": lambda: (
            db.query("orders")
              .join("order_items")
              .join("products")
              .join("categories")
              .where(col("orders.status").in_(FULFILLED_STATUSES))
              .group_by("orders.order_month", "categories.name")
              .aggregate(
                  line_count=count(),
                  revenue=sum_("order_items.line_total"),
                  avg_quantity=avg("order_items.quantity"),
              )
              .having(col("revenue") >= 1500)
              .order_by(("orders.order_month", "asc"), ("revenue", "desc"), ("categories.name", "asc"))
              .limit(30)
              .to_list()
        ),
        "q09_product_demand": lambda: (
            db.query("order_items")
              .join("products")
              .join("categories")
              .join("orders")
              .where(col("orders.status").in_(FULFILLED_STATUSES))
              .group_by("products.name", "categories.name")
              .aggregate(
                  units=sum_("order_items.quantity"),
                  revenue=sum_("order_items.line_total"),
                  avg_discount=avg("order_items.discount"),
              )
              .having(col("units") >= 5)
              .order_by(("units", "desc"), ("revenue", "desc"), ("products.name", "asc"))
              .limit(20)
              .to_list()
        ),
        "q10_risk_orders_by_region_tier_status": lambda: (
            db.query("orders")
              .join("customers")
              .join("regions")
              .join("payments")
              .where(col("orders.status").in_(RISK_STATUSES))
              .group_by("regions.name", "customers.tier", "orders.status")
              .aggregate(
                  order_count=count(),
                  at_risk_amount=sum_("payments.amount"),
                  avg_risk_amount=avg("payments.amount"),
              )
              .having(col("order_count") >= 1)
              .order_by(("at_risk_amount", "desc"), ("regions.name", "asc"), ("customers.tier", "asc"), ("orders.status", "asc"))
              .limit(20)
              .to_list()
        ),
    }


# =============================================================================
# Compatibility normalization and diagnostics
# =============================================================================

RELATIONAL_KEY_MAP = {
    "q01_revenue_by_region_category": {
        "region": "regions.name",
        "category": "categories.name",
    },
    "q02_top_customers_by_paid_amount": {
        "customer_id": "customers.id",
        "customer_name": "customers.name",
        "tier": "customers.tier",
    },
    "q03_supplier_category_performance": {
        "supplier": "suppliers.name",
        "category": "categories.name",
    },
    "q04_sales_rep_monthly_performance": {
        "sales_rep": "sales_reps.name",
        "month": "orders.order_month",
    },
    "q05_payment_method_stats": {
        "payment_method": "payment_method",
        "payment_status": "status",
    },
    "q06_support_tickets_by_category_status": {
        "category": "categories.name",
        "ticket_status": "support_tickets.status",
    },
    "q07_active_customer_revenue_by_region_tier": {
        "region": "regions.name",
        "tier": "customers.tier",
    },
    "q08_monthly_category_revenue": {
        "month": "orders.order_month",
        "category": "categories.name",
    },
    "q09_product_demand": {
        "product": "products.name",
        "category": "categories.name",
    },
    "q10_risk_orders_by_region_tier_status": {
        "region": "regions.name",
        "tier": "customers.tier",
        "order_status": "orders.status",
    },
}


def canonicalize_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)

    return value


def canonicalize_rows(
    query_name: str,
    rows: list[dict[str, Any]],
    source: str,
) -> list[dict[str, Any]]:
    key_map = RELATIONAL_KEY_MAP.get(query_name, {})
    canonical_rows = []

    for row in rows:
        canonical_row = {}

        if source == "relpy":
            for canonical_key, relpy_key in key_map.items():
                canonical_row[canonical_key] = row.get(relpy_key)

            for key, value in row.items():
                if key in key_map.values():
                    continue

                canonical_row[key] = value

        else:
            canonical_row = dict(row)

        canonical_rows.append({
            key: canonicalize_value(value)
            for key, value in canonical_row.items()
        })

    return canonical_rows


def values_equal(
    left: Any,
    right: Any,
    float_tolerance: float = 1e-6,
) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(
            float(left),
            float(right),
            rel_tol=float_tolerance,
            abs_tol=float_tolerance,
        )

    return left == right


def rows_equal(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    float_tolerance: float = 1e-6,
) -> bool:
    if len(left_rows) != len(right_rows):
        return False

    for left_row, right_row in zip(left_rows, right_rows):
        if set(left_row.keys()) != set(right_row.keys()):
            return False

        for key in left_row.keys():
            if not values_equal(left_row[key], right_row[key], float_tolerance):
                return False

    return True


def print_first_difference(
    label: str,
    expected_rows: list[dict[str, Any]],
    actual_rows: list[dict[str, Any]],
) -> None:
    print(f"\n--- First difference: {label} ---")
    print(f"Expected length: {len(expected_rows)}")
    print(f"Actual length:   {len(actual_rows)}")

    max_length = max(len(expected_rows), len(actual_rows))

    for index in range(max_length):
        if index >= len(expected_rows):
            print(f"Extra actual row at index {index}:")
            print(actual_rows[index])
            return

        if index >= len(actual_rows):
            print(f"Missing actual row at index {index}:")
            print(expected_rows[index])
            return

        expected_row = expected_rows[index]
        actual_row = actual_rows[index]

        if set(expected_row.keys()) != set(actual_row.keys()):
            print(f"Different keys at index {index}:")
            print("Expected keys:", sorted(expected_row.keys()))
            print("Actual keys:  ", sorted(actual_row.keys()))
            print("Expected row:", expected_row)
            print("Actual row:  ", actual_row)
            return

        for key in expected_row.keys():
            if not values_equal(expected_row[key], actual_row[key]):
                print(f"Different value at index {index}, column '{key}':")
                print("Expected:", expected_row[key], type(expected_row[key]))
                print("Actual:  ", actual_row[key], type(actual_row[key]))
                print("Expected row:", expected_row)
                print("Actual row:  ", actual_row)
                return

    print("No visible difference found.")


# =============================================================================
# Main benchmark
# =============================================================================

def main():
    print("=== RelPy Scalability / Capability / Compatibility Benchmark ===")
    print(
        f"Dataset: {CUSTOMER_COUNT} customers, "
        f"{ORDER_COUNT} orders, "
        f"{SUPPORT_TICKET_COUNT} support tickets"
    )
    print(f"Query iterations per engine: {QUERY_ITERATIONS}")
    print()

    data = generate_data()

    print("=== Row counts ===")
    for table_name, rows in data.items():
        print(f"{table_name:<16} {len(rows)}")
    print()

    compatibility_failures: list[str] = []
    capability_failures: list[tuple[str, str, Exception]] = []
    timing_summary: dict[str, dict[str, float | None]] = {}

    print("=== Build time ===")

    start = now()
    sqlalchemy_engine = build_sqlalchemy_db(data)
    sqlalchemy_build_time = now() - start
    print(f"{'SQLAlchemy SQLite':<24} {ms(sqlalchemy_build_time):>10.3f} ms")

    start = now()
    duckdb_conn = build_duckdb(data)
    duckdb_build_time = now() - start
    print(f"{'DuckDB':<24} {ms(duckdb_build_time):>10.3f} ms")

    start = now()
    relpy_db = build_relpy(data)
    relpy_build_time = now() - start
    print(f"{'RelPy':<24} {ms(relpy_build_time):>10.3f} ms")

    print()

    print("=== RelPy local persistence ===")

    loaded_relpy_db: RelPy | None = None
    persistence_file_size: int | None = None

    try:
        (
            loaded_relpy_db,
            relpy_save_time,
            relpy_load_time,
            persistence_file_size,
        ) = save_and_load_relpy_db(relpy_db, PERSISTENCE_FILE_PATH)

        print(f"{'RelPy save':<24} {ms(relpy_save_time):>10.3f} ms")
        print(f"{'RelPy load':<24} {ms(relpy_load_time):>10.3f} ms")
        print(f"{'Local file size':<24} {persistence_file_size:>10} bytes")
        print(f"{'Local file path':<24} {str(PERSISTENCE_FILE_PATH)}")

    except Exception as error:
        print(f"RelPy persistence failed: {type(error).__name__}: {error}")
        capability_failures.append(("persistence_save_load", "RelPy", error))

    print()

    relpy_query_functions = relpy_queries(relpy_db)
    loaded_relpy_query_functions = (
        relpy_queries(loaded_relpy_db)
        if loaded_relpy_db is not None
        else None
    )

    for query_name, sql in SQL_QUERIES.items():
        print(f"=== {query_name} ===")

        sql_success = True
        duck_success = True
        relpy_success = True
        loaded_relpy_success = loaded_relpy_query_functions is not None

        try:
            sql_result, sql_time = benchmark(
                "SQLAlchemy SQLite",
                lambda sql=sql: run_sqlalchemy_query(sqlalchemy_engine, sql),
                QUERY_ITERATIONS,
            )
        except Exception as error:
            print(f"SQLAlchemy failed: {type(error).__name__}: {error}")
            capability_failures.append((query_name, "SQLAlchemy", error))
            sql_result = []
            sql_time = 0.0
            sql_success = False

        try:
            duck_result, duck_time = benchmark(
                "DuckDB",
                lambda sql=sql: run_duckdb_query(duckdb_conn, sql),
                QUERY_ITERATIONS,
            )
        except Exception as error:
            print(f"DuckDB failed: {type(error).__name__}: {error}")
            capability_failures.append((query_name, "DuckDB", error))
            duck_result = []
            duck_time = 0.0
            duck_success = False

        try:
            relpy_result, relpy_time = benchmark(
                "RelPy",
                relpy_query_functions[query_name],
                QUERY_ITERATIONS,
            )
        except Exception as error:
            print(f"RelPy failed: {type(error).__name__}: {error}")
            capability_failures.append((query_name, "RelPy", error))
            relpy_result = []
            relpy_time = 0.0
            relpy_success = False

        if loaded_relpy_query_functions is not None:
            try:
                loaded_relpy_result, loaded_relpy_time = benchmark(
                    "RelPy Loaded",
                    loaded_relpy_query_functions[query_name],
                    QUERY_ITERATIONS,
                )
            except Exception as error:
                print(f"RelPy Loaded failed: {type(error).__name__}: {error}")
                capability_failures.append((query_name, "RelPy Loaded", error))
                loaded_relpy_result = []
                loaded_relpy_time = 0.0
                loaded_relpy_success = False
        else:
            loaded_relpy_result = []
            loaded_relpy_time = 0.0

        if sql_success and duck_success and relpy_success:
            canonical_sql = canonicalize_rows(query_name, sql_result, "sql")
            canonical_duck = canonicalize_rows(query_name, duck_result, "sql")
            canonical_relpy = canonicalize_rows(query_name, relpy_result, "relpy")

            sql_vs_duck = rows_equal(canonical_sql, canonical_duck)
            sql_vs_relpy = rows_equal(canonical_sql, canonical_relpy)

            if loaded_relpy_success:
                canonical_loaded_relpy = canonicalize_rows(
                    query_name,
                    loaded_relpy_result,
                    "relpy",
                )
                sql_vs_loaded_relpy = rows_equal(canonical_sql, canonical_loaded_relpy)
                relpy_vs_loaded_relpy = rows_equal(canonical_relpy, canonical_loaded_relpy)
            else:
                canonical_loaded_relpy = []
                sql_vs_loaded_relpy = True
                relpy_vs_loaded_relpy = True

            if (
                sql_vs_duck and
                sql_vs_relpy and
                sql_vs_loaded_relpy and
                relpy_vs_loaded_relpy
            ):
                print("Compatibility: PASS")
            else:
                print("Compatibility: FAIL")

                compatibility_failures.append(query_name)

                if not sql_vs_duck:
                    print_first_difference(
                        label="SQLAlchemy vs DuckDB",
                        expected_rows=canonical_sql,
                        actual_rows=canonical_duck,
                    )

                if not sql_vs_relpy:
                    print_first_difference(
                        label="SQLAlchemy vs RelPy",
                        expected_rows=canonical_sql,
                        actual_rows=canonical_relpy,
                    )

                if not sql_vs_loaded_relpy:
                    print_first_difference(
                        label="SQLAlchemy vs RelPy Loaded",
                        expected_rows=canonical_sql,
                        actual_rows=canonical_loaded_relpy,
                    )

                if not relpy_vs_loaded_relpy:
                    print_first_difference(
                        label="RelPy vs RelPy Loaded",
                        expected_rows=canonical_relpy,
                        actual_rows=canonical_loaded_relpy,
                    )
        else:
            print("Compatibility: SKIPPED because at least one required engine failed.")

        timing_summary[query_name] = {
            "sqlalchemy_ms": ms(sql_time / QUERY_ITERATIONS) if sql_time else None,
            "duckdb_ms": ms(duck_time / QUERY_ITERATIONS) if duck_time else None,
            "relpy_ms": ms(relpy_time / QUERY_ITERATIONS) if relpy_time else None,
            "relpy_loaded_ms": (
                ms(loaded_relpy_time / QUERY_ITERATIONS)
                if loaded_relpy_time
                else None
            ),
        }

        print()

    print("=== RelPy loaded database mutation check ===")

    if loaded_relpy_db is not None:
        try:
            run_persistence_mutation_check(loaded_relpy_db)
            print("Persistence mutation check: PASS")
        except Exception as error:
            print(f"Persistence mutation check: FAIL - {type(error).__name__}: {error}")
            capability_failures.append(("persistence_mutation", "RelPy Loaded", error))
    else:
        print("Persistence mutation check: SKIPPED because loading failed.")

    print()

    print("=== Final capability check ===")
    print("JOIN:        tested through multi-table joins")
    print("FK JOIN:     tested through join(...) without explicit on")
    print("GROUP BY:    tested")
    print("HAVING:      tested")
    print("ORDER BY:    tested")
    print("LIMIT:       tested")
    print("INDEXES:     created and used for equality lookups where applicable")
    print("EXPORT:      to_list() tested through query outputs")
    print("PERSISTENCE: save/load tested through local .relpy.json storage")
    print("MUTATION:    loaded DB insert/index/AutoNumber tested")
    print()

    print("=== Timing summary - average ms per query ===")
    print(
        f"{'Query':<42} "
        f"{'SQLAlchemy':>12} "
        f"{'DuckDB':>12} "
        f"{'RelPy':>12} "
        f"{'Loaded':>12}"
    )

    for query_name, timings in timing_summary.items():
        print(
            f"{query_name:<42} "
            f"{format_optional_ms(timings['sqlalchemy_ms']):>12} "
            f"{format_optional_ms(timings['duckdb_ms']):>12} "
            f"{format_optional_ms(timings['relpy_ms']):>12} "
            f"{format_optional_ms(timings['relpy_loaded_ms']):>12}"
        )

    print()

    if persistence_file_size is not None:
        print("=== Persistence summary ===")
        print(f"File path: {PERSISTENCE_FILE_PATH}")
        print(f"File size: {persistence_file_size} bytes")
        print()

    if compatibility_failures:
        print("=== Compatibility failures ===")
        for query_name in compatibility_failures:
            print(f"FAIL - {query_name}")
    else:
        print("ALL COMPATIBILITY CHECKS PASSED")

    print()

    if capability_failures:
        print("=== Capability failures ===")
        for query_name, engine_name, error in capability_failures:
            print(f"{query_name} - {engine_name}: {type(error).__name__}: {error}")
    else:
        print("ALL CAPABILITY CHECKS PASSED")

    print()
    print("Important note:")
    print(
        "SQLAlchemy here uses SQLite in-memory. "
        "DuckDB is a real analytical engine. "
        "RelPy is a Python in-memory relational library. "
        "RelPy Loaded is the same RelPy database after save/load from local storage. "
        "So this benchmark is useful for capability and relative behavior, "
        "but it is not a scientific database benchmark."
    )


if __name__ == "__main__":
    main()
