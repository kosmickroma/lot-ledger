import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn

conn = get_conn()
try:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT state_cd, COUNT(*) as cnt
            FROM denton_parcels
            GROUP BY state_cd
            ORDER BY 2 DESC
            LIMIT 40
            """
        )
        print(f"{'state_cd':<15} {'count':>10}")
        print("-" * 27)
        for row in cur.fetchall():
            print(f"{str(row[0]):<15} {row[1]:>10,}")
finally:
    release_conn(conn)
