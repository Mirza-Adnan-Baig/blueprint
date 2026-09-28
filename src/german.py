"""
German-format number handling.

German business documents write numbers as 1.234,56 (dot = thousands
separator, comma = decimal separator), the opposite of pandas' default.
Parsed naively, "1.234" becomes 1.234 instead of 1234, and a column of
1.234 / 2.500 / 750 sums to 753.734 instead of 4484.

The format is decided per column, from the values that are unambiguous:

  German evidence: a decimal comma ("12,5", "1.234,56"), or more than one
                   thousands dot ("1.234.567")
  US evidence:     a decimal point not followed by exactly three digits
                   ("12.5", "1,234.56"), or more than one thousands comma

Values like "1.234" or "750" fit both. When a whole column is ambiguous
like that, NUMBER_FORMAT_DEFAULT decides (German by default, since these
are German documents). A column with evidence for BOTH formats is left
alone rather than guessed.

Only text is parsed. Values that are already numbers (Excel stores real
numbers as numbers) are kept exactly as they are.
"""

from __future__ import annotations

import re

import pandas as pd

import config

_STRIP = re.compile(r"[\s  €$£%]|EUR|Stk\.?|St\.", re.IGNORECASE)

_GERMAN_SHAPE = re.compile(r"^-?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?$")
_US_SHAPE = re.compile(r"^-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")

# A separator followed by exactly three digits ("1.234", "1,234") could be
# either format, so it is never evidence on its own.
_GERMAN_EVIDENCE = re.compile(r",(?:\d{1,2}|\d{4,})$|\.\d{3},\d+$|\.\d{3}\.\d{3}")
_US_EVIDENCE = re.compile(r"\.(?:\d{1,2}|\d{4,})$|,\d{3}\.\d+$|,\d{3},\d{3}")


def _clean_text(value: str) -> str:
    return _STRIP.sub("", value.strip())


def detect_number_format(values: pd.Series) -> str:
    """Return 'german', 'us', or 'unknown' for a column of number-like strings."""
    cleaned = values.dropna().astype(str).map(_clean_text)
    cleaned = cleaned[cleaned != ""]
    if cleaned.empty:
        return "unknown"

    fits_german = cleaned.str.match(_GERMAN_SHAPE)
    fits_us = cleaned.str.match(_US_SHAPE)
    german_evidence = (fits_german & cleaned.str.contains(_GERMAN_EVIDENCE)).any()
    us_evidence = (fits_us & cleaned.str.contains(_US_EVIDENCE)).any()

    if german_evidence and us_evidence:
        return "unknown"
    if german_evidence:
        return "german"
    if us_evidence:
        return "us"
    if (fits_german | fits_us).any():
        return "us" if config.NUMBER_FORMAT_DEFAULT == "us" else "german"
    return "unknown"


def _parse(value: str, fmt: str) -> float | None:
    text = _clean_text(value)
    if fmt == "german":
        text = text.replace(".", "").replace(",", ".")
    elif fmt == "us":
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def to_numeric_german_aware(series: pd.Series) -> pd.Series:
    """Convert a column to numbers. Real numbers are kept; text is parsed."""
    if pd.api.types.is_numeric_dtype(series):
        return series

    is_text = series.map(lambda v: isinstance(v, str))
    already_numbers = pd.to_numeric(series.where(~is_text), errors="coerce")

    text_values = series[is_text]
    fmt = detect_number_format(text_values)
    parsed = text_values.map(lambda v: _parse(v, fmt) if fmt != "unknown" else _parse(v, "none"))

    combined = already_numbers.copy()
    combined[is_text] = pd.to_numeric(parsed, errors="coerce")
    return combined


def convert_dataframe_numbers(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    """German-aware numeric conversion for text columns. Dates, numbers and
    booleans are never touched."""
    df = df.copy()
    candidates = columns if columns is not None else list(df.columns)

    for col in candidates:
        if col not in df.columns:
            continue
        if not (pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col])):
            continue
        filled = df[col].dropna()
        filled = filled[filled.astype(str).str.strip() != ""]
        if filled.empty:
            continue
        converted = to_numeric_german_aware(df[col])
        # A text column ("Beschreibung") must not be wiped out: only convert
        # when nearly every filled cell really is a number.
        if converted[filled.index].notna().mean() >= 0.9:
            df[col] = converted

    return df


def looks_like_identifier(series: pd.Series) -> bool:
    """Part numbers, EANs, IBANs: digits, but never to be summed or reformatted."""
    values = series.dropna().astype(str).str.strip()
    values = values[values != ""]
    if values.empty:
        return False

    has_leading_zero = values.str.match(r"^0\d+$").mean() > 0.3
    long_and_unique = (values.str.len() >= 8).mean() > 0.7 and values.nunique() / len(values) > 0.95

    return bool(has_leading_zero or long_and_unique)
