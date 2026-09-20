"""Dhan instrument-master normalization and idempotent database import."""

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..database import SessionLocal
from ..models import Instrument


def _value(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _number(value: str, cast: type[int] | type[float]) -> int | float | None:
    if not value or value.upper() in {"NA", "N/A", "NULL", "NAN"}:
        return None
    return cast(float(value)) if cast is int else cast(value)


def _expiry(value: str) -> datetime | None:
    if not value or value.startswith("0000"):
        return None
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_dhan_instrument(row: dict[str, str]) -> dict[str, Any] | None:
    """Normalize one compact or detailed Dhan master row; return None for unsupported segments."""
    exchange = _value(row, "EXCH_ID", "SEM_EXM_EXCH_ID").upper()
    segment_code = _value(row, "SEGMENT", "SEM_SEGMENT").upper()
    security_id = _value(row, "SECURITY_ID", "SEM_SMST_SECURITY_ID")
    instrument_type = _value(row, "INSTRUMENT", "SEM_INSTRUMENT_NAME").upper()
    if not exchange or not security_id or not instrument_type:
        return None

    if instrument_type == "INDEX":
        canonical_exchange, segment, feed_segment = "IDX", "INDEX", "IDX_I"
    else:
        mapping = {
            ("NSE", "E"): ("NSE", "EQ", "NSE_EQ"),
            ("NSE", "D"): ("NSE", "FNO", "NSE_FNO"),
            ("NSE", "C"): ("NSE", "CURRENCY", "NSE_CURRENCY"),
            ("BSE", "E"): ("BSE", "EQ", "BSE_EQ"),
            ("BSE", "D"): ("BSE", "FNO", "BSE_FNO"),
            ("BSE", "C"): ("BSE", "CURRENCY", "BSE_CURRENCY"),
            ("MCX", "M"): ("MCX", "COMM", "MCX_COMM"),
        }
        mapped = mapping.get((exchange, segment_code))
        if mapped is None:
            return None
        canonical_exchange, segment, feed_segment = mapped

    display_name = _value(row, "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL", "SEM_TRADING_SYMBOL", "SYMBOL_NAME", "SM_SYMBOL_NAME")
    symbol_name = _value(row, "SYMBOL_NAME", "SM_SYMBOL_NAME", "SEM_TRADING_SYMBOL", "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL")
    if not display_name:
        return None
    option_type = _value(row, "OPTION_TYPE", "SEM_OPTION_TYPE").upper() or None
    option_type = {"CALL": "CE", "PUT": "PE"}.get(option_type, option_type)
    return {
        "exchange": canonical_exchange,
        "segment": segment,
        "symbol": symbol_name or display_name,
        "trading_symbol": display_name,
        "name": symbol_name or display_name,
        "instrument_type": instrument_type,
        "expiry_at": _expiry(_value(row, "SM_EXPIRY_DATE", "SEM_EXPIRY_DATE")),
        "strike": _number(_value(row, "STRIKE_PRICE", "SEM_STRIKE_PRICE"), float),
        "option_type": option_type,
        "lot_size": _number(_value(row, "LOT_SIZE", "SEM_LOT_UNITS"), int),
        "tick_size": _number(_value(row, "TICK_SIZE", "SEM_TICK_SIZE"), float),
        "security_id": security_id,
        "feed_segment": feed_segment,
        "metadata": {
            "isin": _value(row, "ISIN") or None,
            "series": _value(row, "SERIES", "SEM_SERIES") or None,
            "underlying_symbol": _value(row, "UNDERLYING_SYMBOL") or None,
            "source": "DHAN_INSTRUMENT_MASTER",
        },
    }


def import_dhan_instruments(filename: str) -> tuple[int, int, int]:
    """Idempotently import Dhan's compact or detailed CSV into the canonical instrument table."""
    created = updated = skipped = 0
    path = Path(filename)
    with path.open("r", encoding="utf-8-sig", newline="") as handle, SessionLocal() as db:
        instruments = list(db.scalars(select(Instrument)).all())
        by_token: dict[tuple[str, str], Instrument] = {}
        legacy_by_security: dict[str, Instrument | None] = {}
        identities = {(item.exchange, item.segment, item.symbol): item for item in instruments}
        for item in instruments:
            token = (item.broker_tokens or {}).get("DHAN")
            if isinstance(token, dict) and token.get("security_id") and token.get("exchange_segment"):
                by_token[(str(token["exchange_segment"]), str(token["security_id"]))] = item
            elif token:
                by_token[("", str(token))] = item
                security_id = str(token)
                legacy_by_security[security_id] = item if security_id not in legacy_by_security else None

        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit("Dhan instrument CSV has no header")
        for row in reader:
            values = parse_dhan_instrument(row)
            if values is None:
                skipped += 1
                continue
            token_key = (values["feed_segment"], values["security_id"])
            instrument = by_token.get(token_key) or legacy_by_security.get(values["security_id"])
            if instrument is None:
                symbol = values["symbol"]
                identity = (values["exchange"], values["segment"], symbol)
                suffix = 0
                while identity in identities:
                    suffix += 1
                    symbol = f"{values['symbol']}:{values['security_id']}" + (f":{suffix}" if suffix > 1 else "")
                    identity = (values["exchange"], values["segment"], symbol)
                instrument = Instrument(exchange=values["exchange"], segment=values["segment"], symbol=symbol,
                                        trading_symbol=values["trading_symbol"], instrument_type=values["instrument_type"])
                db.add(instrument)
                identities[identity] = instrument
                by_token[token_key] = instrument
                created += 1
            else:
                updated += 1
            for field in ("trading_symbol", "name", "instrument_type", "expiry_at", "strike", "option_type",
                          "lot_size", "tick_size"):
                setattr(instrument, field, values[field])
            instrument.broker_tokens = {**(instrument.broker_tokens or {}), "DHAN": {
                "security_id": values["security_id"], "exchange_segment": values["feed_segment"]}}
            instrument.metadata_json = {**(instrument.metadata_json or {}), "dhan": values["metadata"]}
            instrument.is_active = True
        db.commit()
    return created, updated, skipped
