import duckdb
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

db_path = DATA_DIR / "tpch.duckdb"

print("Creating DuckDB database:", db_path)

con = duckdb.connect(str(db_path))

print("Installing/loading TPC-H extension...")
con.execute("INSTALL tpch;")
con.execute("LOAD tpch;")

# Start clean if script is rerun
tables = [
    "lineitem",
    "orders",
    "partsupp",
    "customer",
    "supplier",
    "part",
    "nation",
    "region",
]

for table in tables:
    con.execute(f"DROP TABLE IF EXISTS {table}")

print("Generating TPC-H SF1...")
con.execute("CALL dbgen(sf = 1)")

print("\nGenerated tables:")

for table in [
    "region",
    "nation",
    "supplier",
    "customer",
    "part",
    "partsupp",
    "orders",
    "lineitem",
]:
    count = con.execute(
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()[0]

    print(f"{table:10s}: {count:,}")

print("\nExporting CSV files...")

for table in [
    "region",
    "nation",
    "supplier",
    "customer",
    "part",
    "partsupp",
    "orders",
    "lineitem",
]:
    output = DATA_DIR / f"{table}.csv"

    con.execute(
        f"""
        COPY {table}
        TO '{output}'
        (FORMAT CSV, HEADER TRUE)
        """
    )

    print(f"Exported: {output}")

con.close()

print("\nTPC-H generation complete.")
