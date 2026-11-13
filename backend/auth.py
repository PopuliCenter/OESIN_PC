"""
Authentication + RBAC (SDD §12).

  * Passwords: PBKDF2-HMAC-SHA256 via stdlib hashlib (no native deps).
  * Tokens: short-lived JWT (HS256) via PyJWT; secret persisted under data/.
  * Roles: admin | case_manager | investigator | location_analyst | auditor.

FastAPI dependencies (require_roles / current_user) protect endpoints. The
consent-agent endpoints (session start + point upload) stay UNauthenticated on
purpose: the *subject's* device posts them, gated by an active consent, not by
an operator login.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import database as db

PBKDF2_ITERATIONS = 200_000
TOKEN_TTL_MINUTES = 240
ALGORITHM = "HS256"
SECRET_PATH = Path(__file__).resolve().parent.parent / "data" / "jwt_secret.txt"

ROLES = ["admin", "case_manager", "investigator", "location_analyst", "auditor"]

# Dev seed accounts (change/remove for any real deployment).
SEED_USERS = [
    ("admin", "admin123", "admin"),
    ("investigator", "invest123", "investigator"),
    ("analyst", "analyst123", "location_analyst"),
    ("manager", "manager123", "case_manager"),
    ("auditor", "audit123", "auditor"),
]

bearer = HTTPBearer(auto_error=False)


# --- secret -----------------------------------------------------------------
def _get_secret() -> str:
    env = os.environ.get("JWT_SECRET")
    if env:
        return env
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        return SECRET_PATH.read_text().strip()
    secret = secrets.token_hex(32)
    SECRET_PATH.write_text(secret)
    return secret


# --- password hashing -------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2:{PBKDF2_ITERATIONS}:{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split(":")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# --- tokens -----------------------------------------------------------------
def create_token(user_id: str, username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id, "username": username, "role": role,
        "iat": now, "exp": now + timedelta(minutes=TOKEN_TTL_MINUTES),
    }
    return jwt.encode(payload, _get_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, _get_secret(), algorithms=[ALGORITHM])


# --- seeding ----------------------------------------------------------------
def seed_users() -> None:
    conn = db.get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if count:
            return
        for username, password, role in SEED_USERS:
            conn.execute(
                "INSERT INTO users (id, username, role, password_hash, status, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (db.new_id("usr"), username, role, hash_password(password),
                 "active", db.now_iso()),
            )
        conn.commit()
    finally:
        conn.close()


def authenticate(username: str, password: str) -> Optional[dict]:
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username=%s AND status='active'", (username,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


# --- FastAPI dependencies ---------------------------------------------------
def current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> dict:
    if creds is None:
        raise HTTPException(401, "Authentication required.")
    try:
        payload = decode_token(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired.")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid token.")
    return {"id": payload["sub"], "username": payload["username"], "role": payload["role"]}


def require_roles(*roles: str):
    """Dependency factory: allow only the given roles (admin always allowed)."""
    allowed = set(roles) | {"admin"}

    def checker(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in allowed:
            raise HTTPException(
                403, f"Role '{user['role']}' not permitted. Requires one of: {sorted(allowed)}")
        return user

    return checker
