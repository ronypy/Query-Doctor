import duckdb
import re
from pathlib import Path

DB_PATH = "data/tpch.duckdb"
OUTPUT_DIR = Path("workload/tpch_queries")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

con = duckdb.connect(DB_PATH)

con.execute("INSTALL tpch")
con.execute("LOAD tpch")

rows = con.execute("""
    SELECT query_nr, query
    FROM tpch_queries()
    ORDER BY query_nr
""").fetchall()

print(f"Found {len(rows)} TPC-H queries")

for query_nr, query in rows:

    # Convert DuckDB interval syntax to PostgreSQL syntax.
    #
    # DuckDB:
    # INTERVAL '3' MONTH
    #
    # PostgreSQL:
    # INTERVAL '3 months'

    query = re.sub(
        r"INTERVAL\s+'(\d+)'\s+DAY",
        r"INTERVAL '\1 days'",
        query,
        flags=re.IGNORECASE
    )

    query = re.sub(
        r"INTERVAL\s+'(\d+)'\s+MONTH",
        r"INTERVAL '\1 months'",
        query,
        flags=re.IGNORECASE
    )

    query = re.sub(
        r"INTERVAL\s+'(\d+)'\s+YEAR",
        r"INTERVAL '\1 years'",
        query,
        flags=re.IGNORECASE
    )

    filename = OUTPUT_DIR / f"q{query_nr:02d}.sql"

    filename.write_text(query.strip() + "\n")

    print(f"Exported {filename}")

con.close()

print("\nDone.")
