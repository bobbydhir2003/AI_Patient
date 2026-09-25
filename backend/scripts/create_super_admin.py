"""Create the first Super Admin account (or promote an existing account).

super_admin is the ONLY way to reach system administration (survey resets,
system dashboard, runtime config/credentials, traffic, load & capacity testing,
AI usage & cost). It can never be granted through the User Accounts API, so this
server-side command is the bootstrap AND the recovery path.

Credentials are NEVER hard-coded. The password is read ONLY from an
interactive, hidden prompt (asked twice): there is deliberately no --password
flag and no password environment variable, so it can never land in shell
history, process listings, CI logs or this command's output. The command
refuses to run without an interactive terminal.

Run from the backend/ directory:

    python -m scripts.create_super_admin --email superadmin@school.edu \
        --full-name "Super Administrator"

If the email already belongs to an account, nothing changes unless
--promote-existing is passed (so a typo can never silently elevate someone):

    python -m scripts.create_super_admin --email superadmin@school.edu --promote-existing

Promotion keeps the account's id, sessions and linked student profile and its
existing password; add --reset-password to set a new one (prompted).
"""
import argparse
import getpass
import re
import sys

from app.core.constants import (
    ACCOUNT_STATUS_ACTIVE,
    AUDIT_RECORD_SUPER_ADMIN_USER,
    USER_ROLE_SUPER_ADMIN,
)
from app.core.security import hash_password
from app.database.connection import get_session_factory
from app.repositories.audit_repository import AuditRepository
from app.repositories.user_repository import UserRepository

MIN_PASSWORD_LENGTH = 12
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _interactive() -> bool:
    return sys.stdin.isatty()


def _read_password() -> str:
    """Hidden prompt, asked twice. Never echoed, printed or logged."""
    first = getpass.getpass("Super Admin password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        print("ERROR: passwords do not match.", file=sys.stderr)
        return ""
    return first


def _validate_password(password: str | None) -> str | None:
    if not password:
        return "a password is required."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or promote a Super Admin account.")
    parser.add_argument("--email", default=None)
    parser.add_argument("--full-name", default=None)
    parser.add_argument(
        "--promote-existing", action="store_true",
        help="Allow promoting an account that already exists with this email.",
    )
    parser.add_argument(
        "--reset-password", action="store_true",
        help="With --promote-existing: also replace the existing password.",
    )
    args = parser.parse_args(argv)

    email = (args.email or "").strip().lower()
    if not email:
        email = input("Super Admin email: ").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 255:
        print("ERROR: a valid email address is required.", file=sys.stderr)
        return 2

    db = get_session_factory()()
    try:
        repo = UserRepository(db)
        existing = repo.get_by_email(email)

        if existing is not None and not args.promote_existing:
            print(
                f"ERROR: an account with email '{email}' already exists "
                f"(role={existing.role}). Re-run with --promote-existing to promote it.",
                file=sys.stderr,
            )
            return 3

        needs_password = existing is None or args.reset_password
        password = None
        if needs_password:
            if not _interactive():
                print(
                    "ERROR: run this command in an interactive terminal; the password "
                    "is only accepted from the hidden prompt.",
                    file=sys.stderr,
                )
                return 2
            password = _read_password()
            problem = _validate_password(password)
            if problem:
                print(f"ERROR: {problem}", file=sys.stderr)
                return 2

        if existing is not None:
            old_role = existing.role
            existing.role = USER_ROLE_SUPER_ADMIN
            existing.account_status = ACCOUNT_STATUS_ACTIVE
            existing.is_active = True
            if args.reset_password:
                existing.password_hash = hash_password(password)
            if args.full_name:
                existing.full_name = args.full_name.strip()
            user = existing
            description = f"{email}: {old_role} -> {USER_ROLE_SUPER_ADMIN} (bootstrap command)."
            verb = "Promoted existing account"
        else:
            user = repo.create(
                email=email,
                password_hash=hash_password(password),
                full_name=(args.full_name or "Super Administrator").strip(),
                role=USER_ROLE_SUPER_ADMIN,
                student_id=None,
                is_active=True,
            )
            user.account_status = ACCOUNT_STATUS_ACTIVE
            description = f"{email}: created as {USER_ROLE_SUPER_ADMIN} (bootstrap command)."
            verb = "Created Super Admin account"

        db.flush()
        AuditRepository(db).record(
            admin_user_id=None,
            admin_email="system:create_super_admin",
            action_type="ROLE_CHANGED",
            record_type=AUDIT_RECORD_SUPER_ADMIN_USER,  # super-admin-only visibility
            record_id=user.id,
            description=description,
        )
        db.commit()
        print(f"{verb} '{email}' (id={user.id}).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
