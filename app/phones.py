"""Phone numbers: Fernet-encrypted for sending, salted hash for lookups.
Never log the plain number."""
from __future__ import annotations

import hashlib
import hmac
import re

from cryptography.fernet import Fernet


def normalize_phone(phone: str) -> str:
    p = re.sub(r"[\s\-()]", "", phone)
    if p.startswith("00"):
        p = "+" + p[2:]
    if p.startswith("0") and len(p) == 10:  # Kenyan local format
        p = "+254" + p[1:]
    if not re.fullmatch(r"\+\d{8,15}", p):
        raise ValueError("invalid phone number")
    return p


def hash_phone(phone: str, salt: str) -> str:
    return hmac.new(salt.encode(), normalize_phone(phone).encode(),
                    hashlib.sha256).hexdigest()


def encrypt_phone(phone: str, key: str) -> str:
    return Fernet(key.encode()).encrypt(normalize_phone(phone).encode()).decode()


def decrypt_phone(token: str, key: str) -> str:
    return Fernet(key.encode()).decrypt(token.encode()).decode()
