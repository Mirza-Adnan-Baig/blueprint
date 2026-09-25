"""
German-format number and date handling.

German business documents write numbers as 1.234,56 (period = thousands
separator, comma = decimal separator) -- the opposite of pandas' default
en-US parsing. If you feed a German-formatted column straight into
pd.to_numeric, "1.234" silently becomes 1.234 instead of 1234, and a
column of 1.234 / 2.500 / 750 sums to 753.734 instead of 4.484. This
module detects the format per column (never assume, columns in the same
file can differ) and converts it before anything touches the data.
"""

from __future__ import annotations

import re

import pandas as pd

_GERMAN_NUMBER = re.compile(r"^-?\d{1,3}(\.\d{3})*(,\d+)?$|^-?\d+(,\d+)?$")
_US_NUMBER = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")


def detect_number_format(series: pd.Series, sample_size: int = 50) -> str:
    """Return 'german', 'us', or 'unknown' for a column of string-like numbers."""
    values = series.dropna().astype(str).str.strip()
    values = values[values != ""]
    if values.empty:
        return "unknown"

    sample = values.head(sample_size)
    german_votes = sample.str.match(_GERMAN_NUMBER).sum()
    us_votes = sample.str.match(_US_NUMBER).sum()

    # A value like "1.234" matches both patterns (ambiguous on its own), so
    # the deciding signal is values that are UNAMBIGUOUS for one format:
    # anything with a comma decimal (German) or a period decimal with a
    # thousands comma (US).
    unambiguous_german = sample.str.contains(r",\d{1,2}$").sum()
    unambiguous_us = sample.str.contains(r"\.\d{1,2}$").sum() & sample.str.contains(",").sum()

    if unambiguous_german > 0 and unambiguous_us == 0:
        return "german"
    if unambiguous_us > 0 and unambiguous_german == 0:
        return "us"
    if german_votes > us_votes:
        return "german"
    if us_votes > german_votes:
        return "us"
    return "unknown"


def to_numeric_german_aware(series: pd.Series) -> pd.Series:
    """Convert a column to numeric, auto-detecting German vs. US formatting."""
    if pd.api.types.is_numeric_dtype(series):
        return series

    fmt = detect_number_format(series)
    cleaned = series.astype(str).str.strip()

    if fmt == "german":
        cleaned = cleaned.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    elif fmt == "us":
        cleaned = cleaned.str.replace(",", "", regex=False)
    # 'unknown' -- leave as-is, let pd.to_numeric raise/NaN rather than guess wrong

    return pd.to_numeric(cleaned, errors="coerce")


def convert_dataframe_numbers(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    """Apply German-aware numeric conversion to the given columns (or all object columns)."""
    df = df.copy()
    target_columns = columns or df.select_dtypes(include="object").columns.tolist()

    for col in target_columns:
        if col not in df.columns:
            continue
        converted = to_numeric_german_aware(df[col])
        # Only replace the column if conversion actually produced mostly-numeric
        # values -- a text column ("Description") shouldn't be nuked to NaN.
        if converted.notna().mean() >= 0.7:
            df[col] = converted

    return df


# German locale identifiers that look numeric but are IDs, not quantities
# (e.g. a leading-zero part number "0012345" or a long unique digit string
# like an EAN/IBAN). These should never be run through numeric conversion
# or summed even if every value in the column looks like a number.
def looks_like_identifier(series: pd.Series) -> bool:
    values = series.dropna().astype(str).str.strip()
    values = values[values != ""]
    if values.empty:
        return False

    has_leading_zero = values.str.match(r"^0\d+$").mean() > 0.3
    long_and_unique = (values.str.len() >= 8).mean() > 0.7 and values.nunique() / len(values) > 0.95

    return bool(has_leading_zero or long_and_unique)
