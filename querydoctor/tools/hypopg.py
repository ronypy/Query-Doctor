def reset_hypothetical(conn):

    with conn.cursor() as cur:

        cur.execute(
            "SELECT hypopg_reset()"
        )


def create_hypothetical_index(conn, index_sql: str):

    with conn.cursor() as cur:

        cur.execute(
            "SELECT * FROM hypopg_create_index(%s)",
            (index_sql,),
        )

        row = cur.fetchone()

    return {
        "indexrelid": row[0],
        "indexname": row[1],
    }


def list_hypothetical_indexes(conn):

    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                indexrelid,
                index_name,
                table_name
            FROM hypopg_list_indexes
        """)

        rows = cur.fetchall()

    return [
        {
            "indexrelid": r[0],
            "indexname": r[1],
            "table": r[2],
        }
        for r in rows
    ]


def hypothetical_index_size(conn, indexrelid: int):

    with conn.cursor() as cur:

        cur.execute(
            "SELECT hypopg_relation_size(%s)",
            (indexrelid,),
        )

        return int(cur.fetchone()[0])
