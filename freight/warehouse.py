"""The SQL warehouse: load raw tables, build staging and marts, run data-quality checks.

Uses SQLite from the standard library, so there is nothing to install. The SQL
lives in ``freight/sql`` as ordered files: staging (``01``), marts (``02``) and
checks (``03``).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pandas as pd

from .simulate import Assumptions

SQL_DIR = Path(__file__).parent / "sql"
PENALTY_CAP_DAYS = 5


class DataQualityError(RuntimeError):
    """Raised when one or more data-quality checks fail."""


def build_warehouse(tables: dict[str, pd.DataFrame], a: Assumptions = Assumptions(), db_path: str | Path = ":memory:") -> sqlite3.Connection:
    """Load the raw tables and build every layer. Returns an open connection."""
    conn = sqlite3.connect(str(db_path))
    for name, df in tables.items():
        df.to_sql(name, conn, index=False, if_exists="replace")
    conn.execute("DROP TABLE IF EXISTS ref_params")
    conn.execute("CREATE TABLE ref_params (name TEXT PRIMARY KEY, value REAL, value_text TEXT)")
    conn.executemany(
        "INSERT INTO ref_params VALUES (?, ?, ?)",
        [("late_penalty_per_day", a.late_penalty_per_day, None), ("penalty_cap_days", PENALTY_CAP_DAYS, None), ("as_of", None, a.end)],
    )
    for sql_file in sorted(SQL_DIR.glob("0[12]_*.sql")):
        conn.executescript(sql_file.read_text())
    conn.commit()
    return conn


def _parse_checks(text: str) -> dict[str, str]:
    blocks = re.split(r"^-- check:\s*(\S+)\s*$", text, flags=re.MULTILINE)
    return {name: re.sub(r"^--.*$", "", sql, flags=re.MULTILINE).strip() for name, sql in zip(blocks[1::2], blocks[2::2])}


def run_checks(conn: sqlite3.Connection) -> dict[str, int]:
    """Run every check in ``03_checks.sql``; each returns its number of violations."""
    checks = _parse_checks((SQL_DIR / "03_checks.sql").read_text())
    return {name: int(conn.execute(sql).fetchone()[0]) for name, sql in checks.items()}


def assert_quality(conn: sqlite3.Connection) -> dict[str, int]:
    """Run the checks and raise ``DataQualityError`` listing any that fail."""
    results = run_checks(conn)
    failed = {k: v for k, v in results.items() if v != 0}
    if failed:
        raise DataQualityError(f"data-quality checks failed: {failed}")
    return results


def read_table(conn: sqlite3.Connection, name: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {name}", conn)
