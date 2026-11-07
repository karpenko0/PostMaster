"""Validation and canonical comparison of Telegram channel identifiers."""

import re


def normalize_target(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value, flags=re.ASCII):
        return value.lower()
    if re.fullmatch(r"-?[0-9]+", value, flags=re.ASCII) and int(value) != 0:
        return str(int(value))
    raise ValueError("invalid Telegram channel identifier")


def parse_allowlist(value: str) -> frozenset[str]:
    return frozenset(normalize_target(item) for item in value.split(",") if item.strip())
