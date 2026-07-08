from __future__ import annotations

from pathlib import Path

from ghapi_crawler.config import load_settings
from ghapi_crawler.db import open_connection


def _migration_files() -> list[Path]:
    root = Path(__file__).resolve().parent / "migrations"
    return sorted(root.glob("*.sql"))


def run_migrations() -> int:
    settings = load_settings()
    applied = 0

    with open_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute("SELECT version FROM schema_migrations")
            done = {row["version"] for row in cur.fetchall()}

            for path in _migration_files():
                version = path.name
                if version in done:
                    continue
                sql = path.read_text()
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
                applied += 1
                print(f"Applied migration {version}")

        conn.commit()

    if applied == 0:
        print("No pending migrations")
    return applied


def main() -> None:
    run_migrations()


if __name__ == "__main__":
    main()

