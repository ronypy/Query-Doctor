import psycopg

from querydoctor.config import get_settings


def get_conn(admin: bool = False):
    """
    Agent connection (default): qd_agent role, read-only transactions.
    HypoPG works on this connection too — hypothetical indexes live only
    in backend memory, so read-only mode does not block them.

    Admin connection: only for scripts / optional real validation.
    """

    settings = get_settings()

    url = settings.admin_database_url if admin else settings.database_url

    conn = psycopg.connect(url, autocommit=True)

    with conn.cursor() as cur:
        cur.execute(
            f"SET statement_timeout = {int(settings.statement_timeout_ms)}"
        )

        if not admin:
            cur.execute("SET default_transaction_read_only = on")

    return conn


def wait_for_db(admin: bool = False, timeout_s: float = 60.0) -> None:
    """
    Block until the server accepts connections again — used after a backend
    crash (e.g. a HypoPG segfault makes PostgreSQL restart every backend).
    """

    import time

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            get_conn(admin=admin).close()
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


CRASH_ERROR = ("HypoPG experiment crashed the PostgreSQL backend "
               "(connection lost); candidate excluded")
