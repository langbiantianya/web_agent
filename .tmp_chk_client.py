import asyncio
import sqlalchemy as sa
from app.config.db import engine

SQL = (
    "select column_name from information_schema.columns "
    "where table_name = 'oauth_login_attempt' order by ordinal_position"
)

async def main():
    async with engine.begin() as c:
        rows = await c.execute(sa.text(SQL))
        print("client columns:", [r[0] for r in rows])

asyncio.run(main())