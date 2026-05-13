# from logging.config import fileConfig
# from sqlalchemy import pool
# from sqlalchemy.ext.asyncio import async_engine_from_config
# from alembic import context
# import sys
# import os

# # Add project root to path
# sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# from app.core.config import settings
# from app.core.database import Base

# # Import ALL models here so Alembic can detect them
# from app.models.user import User
# from app.models.business import Business
# from app.models.otp_code import OTPCode
# from app.models.customer import Customer
# from app.models.transaction import Transaction
# from app.models.message_log import MessageLog

# config = context.config
# config.set_main_option("sqlalchemy.url", settings.database_url)

# if config.config_file_name is not None:
#     fileConfig(config.config_file_name)

# target_metadata = Base.metadata


# def run_migrations_offline() -> None:
#     url = config.get_main_option("sqlalchemy.url")
#     context.configure(
#         url=url,
#         target_metadata=target_metadata,
#         literal_binds=True,
#         dialect_opts={"paramstyle": "named"},
#     )
#     with context.begin_transaction():
#         context.run_migrations()


# def do_run_migrations(connection):
#     context.configure(connection=connection, target_metadata=target_metadata)
#     with context.begin_transaction():
#         context.run_migrations()


# async def run_async_migrations():
#     connectable = async_engine_from_config(
#         config.get_section(config.config_ini_section, {}),
#         prefix="sqlalchemy.",
#         poolclass=pool.NullPool,
#     )
#     async with connectable.connect() as connection:
#         await connection.run_sync(do_run_migrations)
#     await connectable.dispose()


# def run_migrations_online() -> None:
#     import asyncio
#     asyncio.run(run_async_migrations())


# if context.is_offline_mode():
#     run_migrations_offline()
# else:
#     run_migrations_online()


from logging.config import fileConfig
from sqlalchemy import pool, create_engine
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.core.config import settings
from app.core.database import Base

# Import ALL models here so Alembic can detect them
from app.models.user import User
from app.models.business import Business
from app.models.otp_code import OTPCode
from app.models.customer import Customer
from app.models.transaction import Transaction
from app.models.message_log import MessageLog

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    # Get the database URL from settings instead of config section
    database_url = settings.database_url
    
    # Configure asyncio engine directly with the URL
    connectable = async_engine_from_config(
        {"sqlalchemy.url": database_url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    import asyncio
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()