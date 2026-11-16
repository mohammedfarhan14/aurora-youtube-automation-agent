import asyncio

from sqlalchemy import text

from app.database.engine import get_engine


async def main() -> None:
    engine = get_engine()

    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        print("Database connection successful:", result.scalar_one())

        result = await connection.execute(text("SELECT current_database(), current_user"))
        print("Database identity:", result.one())

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
