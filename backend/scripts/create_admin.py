"""Create (or update) the first administrator account.

The password is NEVER hard-coded: it is read from the ADMIN_PASSWORD
environment variable (or --password). Run from the backend/ directory:

    ADMIN_EMAIL=admin@school.edu ADMIN_PASSWORD='strong-pass' \
        python -m scripts.create_admin

Or with explicit flags:

    python -m scripts.create_admin --email admin@school.edu --password 'strong-pass'

If an account with the email already exists it is promoted to admin and its
password reset, so this command is safe to re-run.

A SUPER ADMIN account is never touched: this command refuses (exit code 4)
before prompting for a password or changing anything, so it can never demote,
re-password or re-activate a super admin (nor bypass the last-active-super-admin
protection). There is deliberately no override flag; super admin accounts are
managed only through scripts/create_super_admin.py and the Super Admin UI.
"""
import argparse
import getpass
import sys

from app.core.config import get_settings
from app.core.constants import USER_ROLE_ADMIN, USER_ROLE_SUPER_ADMIN
from app.core.security import hash_password
from app.database.connection import get_session_factory
from app.repositories.user_repository import UserRepository


SUPER_ADMIN_REFUSAL = "Refusing to modify a Super Admin account with create_admin."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or update an admin user.")
    parser.add_argument("--email", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--full-name", default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    email = (args.email or settings.admin_email or "").strip().lower()
    password = args.password if args.password is not None else settings.admin_password
    full_name = args.full_name or settings.admin_full_name or "Administrator"

    if not email:
        email = input("Admin email: ").strip().lower()
    if not email:
        print("ERROR: both an email and a password are required.", file=sys.stderr)
        return 2

    factory = get_session_factory()
    db = factory()
    try:
        repo = UserRepository(db)
        existing = repo.get_by_email(email)
        # Checked FIRST: nothing below may run for a super admin.
        if existing is not None and existing.role == USER_ROLE_SUPER_ADMIN:
            print(f"ERROR: {SUPER_ADMIN_REFUSAL}", file=sys.stderr)
            return 4

        if not password:
            password = getpass.getpass("Admin password: ")
        if not password:
            print("ERROR: both an email and a password are required.", file=sys.stderr)
            return 2
        if len(password) < 8:
            print("ERROR: password must be at least 8 characters.", file=sys.stderr)
            return 2

        if existing is not None:
            existing.role = USER_ROLE_ADMIN
            existing.is_active = True
            # A normal (academic) admin; system administration requires the
            # separate super_admin role (scripts/create_super_admin.py).
            existing.password_hash = hash_password(password)
            if full_name:
                existing.full_name = full_name
            db.commit()
            print(f"Updated existing account '{email}' -> admin (password reset).")
        else:
            user = repo.create(
                email=email,
                password_hash=hash_password(password),
                full_name=full_name,
                role=USER_ROLE_ADMIN,
                student_id=None,
                is_active=True,
            )
            db.commit()
            print(f"Created admin account '{email}' (id={user.id}).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
