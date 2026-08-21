"""Lightweight JWT auth (stdlib only) — single admin, password-only login.
Ported from the gallery project's app/auth.py (itself ported from stock),
same PBKDF2 + HS256 JWT approach, adapted to h3-studio's flat single-process
layout (no DB - password hash / JWT secret / reset token live in a small
gitignored JSON file next to this module).
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

AUTH_CONFIG_PATH = Path(__file__).parent / "auth_config.json"
ALGORITHM = "HS256"
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 days
RESET_TOKEN_TTL_SECONDS = 60 * 30  # 30 minutes
ADMIN_EMAIL = "ajchen2017@gmail.com"

_bearer = HTTPBearer(auto_error=False)


def _load_config() -> dict:
    if AUTH_CONFIG_PATH.exists():
        with open(AUTH_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_config(cfg: dict):
    with open(AUTH_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)


def _get_or_create_secret(cfg: dict) -> str:
    if "jwt_secret" not in cfg:
        cfg["jwt_secret"] = secrets.token_hex(32)
        _save_config(cfg)
    return cfg["jwt_secret"]


# ── password hashing (PBKDF2-SHA256, 260k iterations) ──────────
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
    return f"pbkdf2$sha256$260000${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, algo, iters, salt, dk_stored = stored.split("$")
        dk = hashlib.pbkdf2_hmac(algo, password.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), dk_stored)
    except Exception:
        return False


# ── JWT helpers ──────────────────────────────────────────────
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def create_token(payload: dict, ttl: int = TOKEN_TTL) -> str:
    cfg = _load_config()
    secret = _get_or_create_secret(cfg)
    header = _b64url(json.dumps({"alg": ALGORITHM, "typ": "JWT"}).encode())
    claims = {**payload, "exp": int(time.time()) + ttl, "iat": int(time.time())}
    body = _b64url(json.dumps(claims).encode())
    sig_input = f"{header}.{body}".encode()
    sig = _b64url(hmac.new(secret.encode(), sig_input, hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def decode_token(token: str) -> Optional[dict]:
    try:
        cfg = _load_config()
        secret = _get_or_create_secret(cfg)
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, body_b64, sig_b64 = parts
        sig_input = f"{header_b64}.{body_b64}".encode()
        expected = _b64url(hmac.new(secret.encode(), sig_input, hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig_b64):
            return None
        claims = json.loads(_b64url_decode(body_b64))
        if claims.get("exp", 0) < time.time():
            return None
        return claims
    except Exception:
        return None


def get_admin_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> dict:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    claims = decode_token(creds.credentials)
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return claims


def get_admin_user_flexible(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer), token: Optional[str] = None
) -> dict:
    """Same check as get_admin_user, but also accepts the JWT as a `?token=`
    query param - native <video>/<audio>/<img> elements can't attach an
    Authorization header, so media-serving routes need this instead."""
    raw = creds.credentials if creds else token
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    claims = decode_token(raw)
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return claims


# ── password state ──────────────────────────────────────────
def is_password_set() -> bool:
    return bool(_load_config().get("password_hash"))


def check_password(candidate: str) -> bool:
    stored = _load_config().get("password_hash")
    if not stored:
        return False
    return verify_password(candidate, stored)


def set_password(new_password: str):
    cfg = _load_config()
    cfg["password_hash"] = hash_password(new_password)
    cfg.pop("reset_token", None)
    cfg.pop("reset_expires", None)
    _save_config(cfg)


def seed_password_if_missing(initial_password: str):
    """Called at startup from an env var so there's always a way in on a
    fresh install, without ever committing a real password to the repo."""
    if not is_password_set() and initial_password:
        set_password(initial_password)


# ── forgot-password reset tokens ────────────────────────────
def create_reset_token() -> str:
    cfg = _load_config()
    token = secrets.token_urlsafe(24)
    cfg["reset_token"] = token
    cfg["reset_expires"] = time.time() + RESET_TOKEN_TTL_SECONDS
    _save_config(cfg)
    return token


def verify_reset_token(token: str) -> bool:
    cfg = _load_config()
    stored = cfg.get("reset_token")
    expires = cfg.get("reset_expires", 0)
    if not stored or not secrets.compare_digest(stored, token):
        return False
    return time.time() <= expires
