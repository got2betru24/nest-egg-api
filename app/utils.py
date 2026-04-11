# =============================================================================
# NestEgg - app/utils.py
# Shared utility helpers for the API layer.
# =============================================================================

from __future__ import annotations

import csv
import io
from datetime import datetime


def current_year() -> int:
    return datetime.utcnow().year


def parse_ss_earnings_csv(content: bytes) -> list[dict]:
    """
    Parse a Social Security earnings CSV upload.

    Expected format (header row required):
        year,earnings
        2000,45000.00
        2001,48000.00
        ...

    Returns:
        List of dicts: [{"year": int, "earnings": float}, ...]

    Raises:
        ValueError: If the CSV is malformed or missing required columns.
    """
    text = content.decode("utf-8-sig").strip()  # Handle BOM if present
    reader = csv.DictReader(io.StringIO(text))

    required_cols = {"year", "earnings"}
    if not reader.fieldnames:
        raise ValueError("CSV file appears to be empty.")

    actual_cols = {col.strip().lower() for col in reader.fieldnames}
    if not required_cols.issubset(actual_cols):
        missing = required_cols - actual_cols
        raise ValueError(f"CSV is missing required columns: {missing}")

    rows: list[dict] = []
    for i, row in enumerate(reader, start=2):  # Line 2+ (1 = header)
        try:
            year_val = int(row["year"].strip())
            earnings_val = float(row["earnings"].strip().replace(",", ""))
        except (ValueError, KeyError) as e:
            raise ValueError(f"Invalid data on row {i}: {e}")

        if year_val < 1950 or year_val > 2040:
            raise ValueError(f"Row {i}: year {year_val} is out of expected range 1950–2040.")
        if earnings_val < 0:
            raise ValueError(f"Row {i}: earnings cannot be negative.")

        rows.append({"year": year_val, "earnings": earnings_val})

    if not rows:
        raise ValueError("CSV contains no data rows.")

    return rows


def round_currency(value: float, decimals: int = 2) -> float:
    """Round to currency precision."""
    return round(value, decimals)


def format_currency(value: float) -> str:
    """Format a float as a USD currency string."""
    return f"${value:,.2f}"


def clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, value))


def age_in_year(birth_year: int, calendar_year: int) -> int:
    """Return age (in years) during a given calendar year."""
    return calendar_year - birth_year
