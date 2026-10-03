from pathlib import Path
import psycopg

QUERY_DIR = Path("workload/tpch_queries")

conn = psycopg.connect(
    host="localhost",
    port=5433,
    dbname="tpch",
    user="postgres",
    password="postgres",
    autocommit=True
)

working = []
failed = []

for path in sorted(QUERY_DIR.glob("q*.sql")):

    sql = path.read_text().strip()

    # Remove final semicolon because we're adding EXPLAIN
    sql = sql.rstrip(";")

    print(f"{path.name:10s}", end=" ")

    try:

        with conn.cursor() as cur:

            cur.execute("SET statement_timeout = '30s'")

            cur.execute(
                "EXPLAIN (FORMAT JSON) " + sql
            )

            cur.fetchone()

        print("✓ OK")
        working.append(path.name)

    except Exception as e:

        print("✗ FAILED")

        # Only show first line of error
        error = str(e).split("\n")[0]

        print(f"    {error}")

        failed.append((path.name, error))


conn.close()


print("\n==============================")
print("SUMMARY")
print("==============================")

print(f"\nWorking queries: {len(working)}")

for q in working:
    print(" ✓", q)

print(f"\nFailed queries: {len(failed)}")

for q, error in failed:
    print(" ✗", q, "-", error)
