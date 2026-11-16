"""Build a correctly escaped PostgreSQL URL for containerized processes."""

import os
import sys

from sqlalchemy.engine import URL


def main() -> None:
    """Set DATABASE_URL from discrete PostgreSQL settings, then exec the process."""
    if os.environ.get("DATABASE_HOST"):
        required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise SystemExit(
                "Missing required database environment variables: " + ", ".join(missing)
            )

        url = URL.create(
            drivername="postgresql+psycopg",
            username=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            host=os.environ["DATABASE_HOST"],
            port=int(os.environ.get("DATABASE_PORT", "5432")),
            database=os.environ["POSTGRES_DB"],
        )
        os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)

    if len(sys.argv) < 2:
        raise SystemExit("A process command is required.")

    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
