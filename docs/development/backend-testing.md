# Backend integration testing

## Safety boundary

The backend test suite is allowed to connect only to a dedicated PostgreSQL
database whose name ends in `_test`. The pytest configuration rejects a
missing, malformed, non-PostgreSQL, or incorrectly named test database before
test modules import the application.

Do not point `TEST_DATABASE_URL` at your development, staging, or production
database. The tests create and delete records as part of their integration
coverage.

## Local setup

1. Keep the normal application database in `DATABASE_URL`, for example
   `aurora_studio`.
2. Create a separate database named `aurora_studio_test`, owned by the same
   local development role. For a fresh local Docker PostgreSQL instance, run
   the following once from the repository root:

   ```bash
   docker exec -it aurora-studio-postgres-1 psql -U aurora -d postgres
   ```

   Then at the PostgreSQL prompt:

   ```sql
   CREATE DATABASE aurora_studio_test OWNER aurora;
   \q
   ```

   If your container has a different name, check it with
   `docker ps --format '{{.Names}}'`. If the test database already exists,
   do not create it again.

3. Add a `TEST_DATABASE_URL` entry to your ignored local `.env` file. It must
   point to `aurora_studio_test` and use the local PostgreSQL port configured
   by Docker Compose (currently `5433` on the host).

4. From the repository root, read the test URL from `.env` without sourcing
   the file. This avoids Bash interpreting unquoted values such as
   `APP_NAME=Aurora Studio` as separate shell tokens. Then run migrations
   against the test database:

   ```bash
   TEST_DB_URL="$(sed -n 's/^TEST_DATABASE_URL=//p' .env)"
   test -n "$TEST_DB_URL" || { echo "TEST_DATABASE_URL is missing from .env"; exit 1; }

   cd backend/api
   DATABASE_URL="$TEST_DB_URL" alembic upgrade head
   unset TEST_DB_URL

   pytest -q
   ```

The pytest safety hook independently loads `TEST_DATABASE_URL` from the
repository's `.env` file and overrides `DATABASE_URL` before importing the
application. Tests therefore cannot silently fall back to the development
database.

## CI

GitHub Actions provisions a disposable PostgreSQL service named
`aurora_studio_test`. Both migrations and tests use that database. The CI
credentials are temporary and must never be reused outside GitHub Actions.
