import asyncio
import sqlalchemy as sa
from services.admin_web.db import admin_engine

SQL = (
    "select column_name from information_schema.columns "
    "where table_name = 'oauth_login_attempt' order by ordinal_position"
)

async def main():
    async with admin_engine.begin() as c:
        rows = await c.execute(sa.text(SQL))
        print("admin columns:", [r[0] for r in rows])

asyncio.run(main())