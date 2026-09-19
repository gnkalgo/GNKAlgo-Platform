import argparse
import getpass
from sqlalchemy import select
from .database import SessionLocal
from .models import Role, User
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

def main():
    parser = argparse.ArgumentParser(description="GnKAlgo operator commands")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin"); admin.add_argument("email")
    args = parser.parse_args()
    if args.command == "create-admin": create_admin(args.email)

if __name__ == "__main__": main()
