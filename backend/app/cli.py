import argparse
import csv
import getpass
from pathlib import Path
from sqlalchemy import select
from .database import SessionLocal
from .models import Instrument, Role, User
from .security import hash_password, validate_password

def create_admin(email: str) -> None:
    password = getpass.getpass("Admin password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation: raise SystemExit("Passwords do not match")
    try: validate_password(password)
    except ValueError as exc: raise SystemExit(str(exc))
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.email == email.lower()))
        if existing: raise SystemExit("User already exists")
        db.add(User(email=email.lower(), password_hash=hash_password(password), role=Role.ADMIN, is_verified=True))
        db.commit()
    print("Admin created")

def import_instruments(filename: str) -> None:
    """Import the canonical security master from a UTF-8 CSV file."""
    path = Path(filename)
    required = {"exchange", "segment", "symbol", "trading_symbol", "instrument_type"}
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle, SessionLocal() as db:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not required <= set(reader.fieldnames):
            raise SystemExit(f"CSV must contain: {', '.join(sorted(required))}")
        for row in reader:
            values = {key: (row.get(key) or "").strip() for key in row}
            instrument = db.scalar(select(Instrument).where(Instrument.exchange == values["exchange"].upper(),
                Instrument.segment == values["segment"].upper(), Instrument.symbol == values["symbol"]))
            if instrument is None:
                instrument = Instrument(exchange=values["exchange"].upper(), segment=values["segment"].upper(),
                    symbol=values["symbol"], trading_symbol=values["trading_symbol"],
                    instrument_type=values["instrument_type"].upper())
                db.add(instrument)
            instrument.trading_symbol = values["trading_symbol"]
            instrument.name = values.get("name") or None
            instrument.instrument_type = values["instrument_type"].upper()
            instrument.lot_size = int(values["lot_size"]) if values.get("lot_size") else None
            instrument.tick_size = float(values["tick_size"]) if values.get("tick_size") else None
            instrument.option_type = values.get("option_type") or None
            instrument.strike = float(values["strike"]) if values.get("strike") else None
            instrument.broker_tokens = {name: values[column] for name, column in
                (("DHAN", "dhan_token"), ("FYERS", "fyers_token"), ("UPSTOX", "upstox_token")) if values.get(column)}
            count += 1
        db.commit()
    print(f"Imported {count} instruments")

def main():
    parser = argparse.ArgumentParser(description="GnKAlgo operator commands")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin"); admin.add_argument("email")
    instruments = sub.add_parser("import-instruments"); instruments.add_argument("csv_file")
    args = parser.parse_args()
    if args.command == "create-admin": create_admin(args.email)
    elif args.command == "import-instruments": import_instruments(args.csv_file)

if __name__ == "__main__": main()
