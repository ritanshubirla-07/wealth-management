from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

DATABASE_URL = "sqlite:///./VIKABH.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the first release. SQLite's create_all() only creates
# missing TABLES, never missing columns, and this project has no migration tool,
# so new columns on an existing table are added here on startup. Each entry is
# (table, column, SQL type); adding a column is idempotent and non-destructive.
_ADDED_COLUMNS = (
    ("holdings", "isin", "VARCHAR(12)"),
    ("holdings", "quantity", "FLOAT"),
    ("market_data", "fundamentals_json", "TEXT"),
    ("market_data", "fundamentals_as_of", "DATETIME"),
    ("holdings", "nse_symbol", "VARCHAR(30)"),
    ("holdings", "bse_code", "VARCHAR(30)"),
    ("market_data", "price_source", "VARCHAR(40)"),
    ("market_data", "yahoo_json", "TEXT"),
    ("market_data", "yahoo_as_of", "DATETIME"),
    ("market_data", "yahoo_symbol", "VARCHAR(30)"),
    ("market_data", "yahoo_industry", "VARCHAR(120)"),
    ("market_data", "yahoo_sector", "VARCHAR(80)"),
)


def _apply_column_migrations() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, column, sql_type in _ADDED_COLUMNS:
            if table not in existing_tables:
                continue  # create_all() will build it complete
            columns = {c["name"] for c in inspector.get_columns(table)}
            if column in columns:
                continue
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {sql_type}'))


def create_tables() -> None:
    from app import models  # noqa: F401 - ensure models are registered with Base metadata
    _apply_column_migrations()
    Base.metadata.create_all(bind=engine)
