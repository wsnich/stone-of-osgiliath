"""
Per-retailer account manager for automated checkout.

Each Account is permanently pinned to one proxy URL — never rotate.
Each Account has its own browser session file (cookies/localStorage) so
multiple accounts on the same retailer can run side-by-side without
contaminating each other's logged-in state.

Retailer-specific flows (login, ATC, checkout) live in monitors/retailers/.
This module is responsible only for:
  - Account CRUD (in-memory + persistence to config.json)
  - Session file paths
  - Health-check status broadcasts

The browser launch / login automation is invoked from web/app.py endpoints.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# Directory for per-account session JSON files. Set at app startup via
# set_data_dir(); falls back to <project>/accounts/.
_ACCOUNTS_DIR: Path = Path(__file__).parent.parent / "accounts"


def set_data_dir(data_dir: Path) -> None:
    global _ACCOUNTS_DIR
    _ACCOUNTS_DIR = Path(data_dir) / "accounts"
    _ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)


def session_path_for(account_id: str) -> Path:
    _ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    return _ACCOUNTS_DIR / f"{account_id}_account_session.json"


# ── Status values broadcast over WS so the UI can color-code ────────────────
STATUS_UNKNOWN  = "unknown"
STATUS_OK       = "ok"        # logged in and verified
STATUS_LOGGING  = "logging_in"
STATUS_AWAITING = "awaiting_login"  # visible browser open, user needs to act
STATUS_EXPIRED  = "expired"   # session file present but invalid
STATUS_ERROR    = "error"


@dataclass
class Account:
    id: str
    retailer: str           # 'amazon', 'walmart', 'target', 'bestbuy'
    name: str               # human label, e.g. "Amazon Primary"
    email: str
    password: str           # plaintext — config.json is gitignored, local only
    proxy: str              # host:port:user:pass — pinned permanently
    enabled: bool = True

    # Runtime state — not persisted to config.json (lives in app_state)
    status: str = STATUS_UNKNOWN
    last_login: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self, include_secrets: bool = False) -> dict:
        d = asdict(self)
        if not include_secrets:
            d.pop("password", None)
            # Mask proxy credentials but leave host:port visible
            if self.proxy:
                parts = self.proxy.split(":")
                if len(parts) == 4:
                    d["proxy"] = f"{parts[0]}:{parts[1]}:***:***"
        d["session_file"] = str(session_path_for(self.id).name)
        d["session_exists"] = session_path_for(self.id).exists()
        return d

    @classmethod
    def from_config_dict(cls, d: dict) -> "Account":
        return cls(
            id=d["id"],
            retailer=d["retailer"],
            name=d.get("name", d["id"]),
            email=d.get("email", ""),
            password=d.get("password", ""),
            proxy=d.get("proxy", ""),
            enabled=d.get("enabled", True),
        )

    def to_config_dict(self) -> dict:
        """Strip runtime fields before persisting to config.json."""
        return {
            "id":       self.id,
            "retailer": self.retailer,
            "name":     self.name,
            "email":    self.email,
            "password": self.password,
            "proxy":    self.proxy,
            "enabled":  self.enabled,
        }


class AccountManager:
    def __init__(self):
        self.accounts: list[Account] = []
        self._lock = asyncio.Lock()

    def load_from_config(self, config: dict) -> None:
        raw = config.get("accounts") or []
        existing_runtime = {a.id: (a.status, a.last_login, a.last_error)
                            for a in self.accounts}
        self.accounts = []
        for d in raw:
            try:
                acc = Account.from_config_dict(d)
                # Restore runtime state if this account already existed
                if acc.id in existing_runtime:
                    s, ll, le = existing_runtime[acc.id]
                    acc.status, acc.last_login, acc.last_error = s, ll, le
                else:
                    # Default status: ok if session exists, else unknown
                    acc.status = STATUS_OK if session_path_for(acc.id).exists() else STATUS_UNKNOWN
                self.accounts.append(acc)
            except KeyError:
                continue

    def find(self, account_id: str) -> Optional[Account]:
        for a in self.accounts:
            if a.id == account_id:
                return a
        return None

    def add(self, retailer: str, name: str, email: str, password: str,
            proxy: str, enabled: bool = True) -> Account:
        new_id = f"{retailer}-{uuid.uuid4().hex[:6]}"
        acc = Account(id=new_id, retailer=retailer, name=name, email=email,
                      password=password, proxy=proxy, enabled=enabled)
        self.accounts.append(acc)
        return acc

    def remove(self, account_id: str) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.id != account_id]
        # Also delete the session file
        sp = session_path_for(account_id)
        if sp.exists():
            try: sp.unlink()
            except Exception: pass
        return len(self.accounts) < before

    def to_config_list(self) -> list[dict]:
        return [a.to_config_dict() for a in self.accounts]

    def to_dict_list(self, include_secrets: bool = False) -> list[dict]:
        return [a.to_dict(include_secrets=include_secrets) for a in self.accounts]


# Module-level singleton — same pattern as deal_tracker / product_hub
account_manager = AccountManager()
