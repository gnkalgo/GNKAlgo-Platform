from app.market import instruments as instrument_importer
from app.market.instruments import import_dhan_instruments, parse_dhan_instrument
from app.models import Instrument
from tests.conftest import TestingSession


def test_parses_detailed_dhan_equity_row():
    parsed = parse_dhan_instrument({
        "EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": "1333", "INSTRUMENT": "EQUITY",
        "SYMBOL_NAME": "STATE BANK OF INDIA", "DISPLAY_NAME": "SBIN", "SERIES": "EQ",
        "LOT_SIZE": "1", "TICK_SIZE": "0.05",
    })
    assert parsed["feed_segment"] == "NSE_EQ"
    assert parsed["security_id"] == "1333"
    assert parsed["trading_symbol"] == "SBIN"
    assert parsed["lot_size"] == 1


def test_parses_compact_dhan_derivative_row():
    parsed = parse_dhan_instrument({
        "SEM_EXM_EXCH_ID": "NSE", "SEM_SEGMENT": "D", "SEM_SMST_SECURITY_ID": "49081",
        "SEM_INSTRUMENT_NAME": "OPTIDX", "SEM_CUSTOM_SYMBOL": "NIFTY 30 SEP 25000 CALL",
        "SM_SYMBOL_NAME": "NIFTY", "SEM_EXPIRY_DATE": "2026-09-30", "SEM_OPTION_TYPE": "CALL",
        "SEM_STRIKE_PRICE": "25000", "SEM_LOT_UNITS": "75", "SEM_TICK_SIZE": "0.05",
    })
    assert parsed["feed_segment"] == "NSE_FNO"
    assert parsed["option_type"] == "CE"
    assert parsed["strike"] == 25000.0
    assert parsed["expiry_at"].isoformat() == "2026-09-30T00:00:00+00:00"


def test_skips_unsupported_dhan_segment_instead_of_guessing():
    assert parse_dhan_instrument({
        "EXCH_ID": "NSE", "SEGMENT": "M", "SECURITY_ID": "1", "INSTRUMENT": "OPTFUT",
        "DISPLAY_NAME": "UNSUPPORTED",
    }) is None


def test_dhan_import_is_idempotent(tmp_path, db, monkeypatch):
    source = tmp_path / "dhan.csv"
    source.write_text(
        "EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,SYMBOL_NAME,DISPLAY_NAME,SERIES,LOT_SIZE,TICK_SIZE\n"
        "NSE,E,1333,EQUITY,STATE BANK OF INDIA,SBIN,EQ,1,0.05\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(instrument_importer, "SessionLocal", TestingSession)

    assert import_dhan_instruments(str(source)) == (1, 0, 0)
    assert import_dhan_instruments(str(source)) == (0, 1, 0)

    rows = db.query(Instrument).all()
    assert len(rows) == 1
    assert rows[0].broker_tokens["DHAN"] == {"security_id": "1333", "exchange_segment": "NSE_EQ"}
