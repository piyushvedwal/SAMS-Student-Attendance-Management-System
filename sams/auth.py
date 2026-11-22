"""Authentication, role-based access, password policy and account lockout
(SRS 3.1.1 and section 6)."""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from . import clock, config, db

ITERATIONS = 200_000
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 5
ROLES = ("admin", "teacher", "student", "parent")
ROLE_LABELS = {"admin": "Administrator", "teacher": "Teacher", "student": "Student", "parent": "Parent"}


class AuthError(Exception):
    """Raised with a message that is safe to show to the user."""


def is_protected(username: str) -> bool:
    """Shared demo accounts cannot be locked out or have their password changed."""
    return config.DEMO_MODE and username in config.DEMO_ACCOUNTS.values()


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return salt, digest.hex()


def verify_password(password: str, salt: str, expected: str) -> bool:
    return hmac.compare_digest(hash_password(password, salt)[1], expected)


def password_problems(password: str) -> list[str]:
    """Return the policy rules a password breaks (empty list = acceptable)."""
    problems = []
    if len(password) < 8:
        problems.append("at least 8 characters")
    if not re.search(r"[a-z]", password):
        problems.append("a lowercase letter")
    if not re.search(r"[A-Z]", password):
        problems.append("an uppercase letter")
    if not re.search(r"\d", password):
        problems.append("a digit")
    if not re.search(r"[^A-Za-z0-9]", password):
        problems.append("a special character")
    return problems


def get_user(user_id: int) -> dict | None:
    """User record plus the profile ids the views need."""
    row = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    if row is None:
        return None
    user = {k: row[k] for k in ("id", "username", "full_name", "email", "role", "is_active", "last_login")}
    if user["role"] == "teacher":
        t = db.one("SELECT id FROM teachers WHERE user_id = ?", (user_id,))
        user["teacher_id"] = t["id"] if t else None
    elif user["role"] == "student":
        s = db.one("SELECT id FROM students WHERE user_id = ?", (user_id,))
        user["student_id"] = s["id"] if s else None
    elif user["role"] == "parent":
        kids = db.query("SELECT id FROM students WHERE parent_user_id = ? ORDER BY roll_no", (user_id,))
        user["children"] = [k["id"] for k in kids]
    return user


def authenticate(username: str, password: str) -> dict:
    username = (username or "").strip().lower()
    row = db.one("SELECT * FROM users WHERE username = ?", (username,))
    generic = "Incorrect username or password."
    if row is None:
        db.audit("login_failed", f"unknown user '{username[:40]}'")
        raise AuthError(generic)
    now = clock.now()
    if row["locked_until"] and datetime.strptime(row["locked_until"], clock.DATETIME_FMT) > now:
        raise AuthError(f"Too many failed attempts. Try again after {row['locked_until'][11:16]}.")
    if not row["is_active"]:
        raise AuthError("This account is deactivated. Contact the administrator.")
    if not verify_password(password or "", row["salt"], row["password_hash"]):
        failed = row["failed_attempts"] + 1
        locked = None
        if failed >= MAX_FAILED_ATTEMPTS and not is_protected(username):
            locked = (now + timedelta(minutes=LOCK_MINUTES)).strftime(clock.DATETIME_FMT)
            failed = 0
        db.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?", (failed, locked, row["id"]))
        db.audit("login_failed", "wrong password" + (" - account locked" if locked else ""), dict(row))
        if locked:
            raise AuthError(f"Too many failed attempts. The account is locked for {LOCK_MINUTES} minutes.")
        raise AuthError(generic)
    db.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login = ? WHERE id = ?",
        (clock.stamp(), row["id"]),
    )
    user = get_user(row["id"])
    db.audit("login", "", user)
    return user


def set_password(user_id: int, new_password: str, actor: dict | None = None) -> None:
    target = db.one("SELECT username FROM users WHERE id = ?", (user_id,))
    if target and is_protected(target["username"]):
        raise AuthError("This is a shared demo account, so its password cannot be changed.")
    problems = password_problems(new_password)
    if problems:
        raise AuthError("Password needs " + ", ".join(problems) + ".")
    salt, digest = hash_password(new_password)
    db.execute(
        "UPDATE users SET salt = ?, password_hash = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
        (salt, digest, user_id),
    )
    db.audit("password_changed", f"for {target['username']}" if target else "", actor)


def change_password(user: dict, current: str, new_password: str) -> None:
    row = db.one("SELECT salt, password_hash FROM users WHERE id = ?", (user["id"],))
    if not verify_password(current or "", row["salt"], row["password_hash"]):
        raise AuthError("Current password is incorrect.")
    if current == new_password:
        raise AuthError("The new password must be different from the current one.")
    set_password(user["id"], new_password, user)


def create_user(username, full_name, role, password, email=None, actor=None, **profile) -> int:
    username = (username or "").strip().lower()
    full_name = (full_name or "").strip()
    if role not in ROLES:
        raise AuthError("Unknown role.")
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        raise AuthError("Username must be 3-40 characters: lowercase letters, digits, dot, dash or underscore.")
    if not full_name:
        raise AuthError("Full name is required.")
    if db.one("SELECT 1 FROM users WHERE username = ?", (username,)):
        raise AuthError("That username is already taken.")
    problems = password_problems(password)
    if problems:
        raise AuthError("Password needs " + ", ".join(problems) + ".")
    salt, digest = hash_password(password)
    with db.tx() as conn:
        user_id = conn.execute(
            "INSERT INTO users(username, full_name, email, role, salt, password_hash, created_at) VALUES(?,?,?,?,?,?,?)",
            (username, full_name, email or None, role, salt, digest, clock.stamp()),
        ).lastrowid
        if role == "teacher":
            conn.execute(
                "INSERT INTO teachers(user_id, employee_code, department) VALUES(?,?,?)",
                (user_id, profile.get("employee_code"), profile.get("department")),
            )
        elif role == "student":
            conn.execute(
                "INSERT INTO students(user_id, roll_no, program, section, semester, parent_user_id) VALUES(?,?,?,?,?,?)",
                (user_id, profile.get("roll_no") or None, profile.get("program"), profile.get("section"),
                 profile.get("semester"), profile.get("parent_user_id")),
            )
        db.audit("user_created", f"{username} ({role})", actor, conn)
    return user_id


def set_active(user_id: int, active: bool, actor: dict) -> None:
    if user_id == actor["id"] and not active:
        raise AuthError("You cannot deactivate your own account.")
    target = db.one("SELECT username FROM users WHERE id = ?", (user_id,))
    if not active and is_protected(target["username"]):
        raise AuthError("This is a shared demo account, so it cannot be deactivated.")
    db.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
    db.audit("user_activated" if active else "user_deactivated", target["username"], actor)
