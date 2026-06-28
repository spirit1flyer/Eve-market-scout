"""Industry character roster (Phase 2.1 of the Industry tab).

A small, self-contained ESI auth roster for the Industry tab, kept SEPARATE
from the seller/buyer trading auth in `esi_auth.py` (that module is untouched).
Up to 10 characters can be logged in; each record carries the standard
`CharacterAuth` token data plus write-in implant percentages (manufacturing-time
%, ME-research-time %, TE-research-time %). Implants only ever affect TIME, never
cost (see PLAN_industry_tab.md S1), so they are a free-form advanced field that
defaults to 0.

Reuses the PKCE helpers, callback handler, OAuth endpoints, CLIENT_ID and
`CharacterAuth` from `esi_auth` by import — one app to CCP (one client_id), so a
second grant of the same app is normal and never looks like two apps. The only
thing that differs here is the SCOPE SET and the persistence file.

Persistence: `industry_characters.json` in the shared data dir (list of records).
"""

import json
import os
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Callable

import requests

from sound_manager import get_data_dir
from esi_auth import (
    CharacterAuth,
    OAuthCallbackHandler,
    generate_code_verifier,
    generate_code_challenge,
    _open_url_robust,
    CLIENT_ID,
    CALLBACK_PORT,
    CALLBACK_URL,
    AUTH_URL,
    TOKEN_URL,
    VERIFY_URL,
)
from http.server import HTTPServer

# File path - shared data directory, separate from esi_auth.json
ROSTER_FILE = str(get_data_dir() / "industry_characters.json")

MAX_CHARACTERS = 10

# Industry roster scope set (deliberately NOT read_clones - implants are
# write-in instead, see PLAN_industry_tab.md S2).
INDUSTRY_SCOPES = [
    "esi-skills.read_skills.v1",              # skills -> build/research TIME
    "esi-characters.read_standings.v1",       # kept for future; no T1 effect
    "esi-characters.read_blueprints.v1",      # owned ME/TE/runs/location/ownership
    "esi-universe.read_structures.v1",        # structure names/locations (R8)
]


class RosterCharacter(CharacterAuth):
    """A roster character: standard token data + write-in implant percentages.

    Implant fields are percentages (e.g. 4.0 means 4%); all affect TIME only.
    """

    def __init__(self, data: dict = None):
        super().__init__(data)
        data = data or {}
        self.implant_mfg_pct = float(data.get("implant_mfg_pct", 0.0))
        self.implant_me_pct = float(data.get("implant_me_pct", 0.0))
        self.implant_te_pct = float(data.get("implant_te_pct", 0.0))

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["implant_mfg_pct"] = self.implant_mfg_pct
        d["implant_me_pct"] = self.implant_me_pct
        d["implant_te_pct"] = self.implant_te_pct
        return d


class IndustryRoster:
    """Up to 10 industry characters, own JSON, own scope set.

    Use `IndustryRoster.singleton()` for the app-wide instance.
    """

    _instance: Optional["IndustryRoster"] = None

    def __init__(self):
        self.characters: List[RosterCharacter] = []
        self._code_verifier: Optional[str] = None
        self._load()

    @classmethod
    def singleton(cls) -> "IndustryRoster":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # =========================================================================
    # PERSISTENCE
    # =========================================================================

    def _load(self):
        if not os.path.exists(ROSTER_FILE):
            return
        try:
            with open(ROSTER_FILE, "r") as f:
                data = json.load(f)
            records = data.get("characters", []) if isinstance(data, dict) else data
            self.characters = [
                RosterCharacter(rec) for rec in records if rec and rec.get("character_id")
            ]
        except (json.JSONDecodeError, IOError) as e:
            print(f"[IndustryRoster] Error loading roster: {e}")

    def _save(self):
        try:
            data = {"characters": [c.to_dict() for c in self.characters]}
            with open(ROSTER_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            print(f"[IndustryRoster] Error saving roster: {e}")

    # =========================================================================
    # ROSTER MANAGEMENT
    # =========================================================================

    def get(self, character_id: int) -> Optional[RosterCharacter]:
        for c in self.characters:
            if c.character_id == character_id:
                return c
        return None

    @property
    def is_full(self) -> bool:
        return len(self.characters) >= MAX_CHARACTERS

    def remove(self, character_id: int):
        self.characters = [c for c in self.characters if c.character_id != character_id]
        self._save()

    def set_implants(self, character_id: int, mfg_pct: float = None,
                     me_pct: float = None, te_pct: float = None):
        """Update write-in implant percentages for a character."""
        char = self.get(character_id)
        if not char:
            return
        if mfg_pct is not None:
            char.implant_mfg_pct = float(mfg_pct)
        if me_pct is not None:
            char.implant_me_pct = float(me_pct)
        if te_pct is not None:
            char.implant_te_pct = float(te_pct)
        self._save()

    def _upsert(self, new_char: RosterCharacter):
        """Add a freshly-authed character, or refresh tokens on an existing one
        (preserving its write-in implant fields)."""
        existing = self.get(new_char.character_id)
        if existing:
            existing.access_token = new_char.access_token
            existing.refresh_token = new_char.refresh_token
            existing.token_expiry = new_char.token_expiry
            existing.character_name = new_char.character_name
        else:
            self.characters.append(new_char)
        self._save()

    # =========================================================================
    # OAUTH FLOW (PKCE) - mirrors ESIAuth but with INDUSTRY_SCOPES
    # =========================================================================

    def _get_auth_url(self) -> str:
        self._code_verifier = generate_code_verifier()
        code_challenge = generate_code_challenge(self._code_verifier)
        scopes = " ".join(INDUSTRY_SCOPES)
        params = {
            "response_type": "code",
            "redirect_uri": CALLBACK_URL,
            "client_id": CLIENT_ID,
            "scope": scopes,
            "state": "eve_market_scout_industry",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        query = "&".join(
            f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()
        )
        return f"{AUTH_URL}?{query}"

    def start_auth_flow(self, callback: Optional[Callable[[bool, str], None]] = None):
        """Start OAuth flow in a background thread to add/refresh a roster char.

        callback(success: bool, message: str) is called when complete.
        """
        if self.is_full:
            if callback:
                callback(False, f"Roster full ({MAX_CHARACTERS} characters max)")
            return

        def do_auth():
            try:
                server = HTTPServer(("localhost", CALLBACK_PORT), OAuthCallbackHandler)
                server.auth_code = None
                server.auth_error = None
                server.timeout = 120

                auth_url = self._get_auth_url()
                opened = _open_url_robust(auth_url)
                if not opened:
                    print("=" * 70)
                    print("BROWSER DID NOT OPEN AUTOMATICALLY.")
                    print("Paste this URL into any browser to continue login:")
                    print(auth_url)
                    print("=" * 70)

                while server.auth_code is None and server.auth_error is None:
                    server.handle_request()

                if server.auth_code:
                    success, detail = self._exchange_code(server.auth_code)
                    if success and callback:
                        callback(True, detail)
                    elif callback:
                        callback(False, f"Failed to exchange code: {detail}")
                elif callback:
                    callback(False, f"Authorization failed: {server.auth_error}")

            except Exception as e:
                if callback:
                    callback(False, str(e))

        threading.Thread(target=do_auth, daemon=True).start()

    def _exchange_code(self, code: str):
        """Exchange the auth code for tokens. Returns (success, message)."""
        if not self._code_verifier:
            return False, "No PKCE code verifier (auth flow state lost)"
        try:
            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": CLIENT_ID,
                    "code_verifier": self._code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            if response.status_code != 200:
                return False, f"Token endpoint HTTP {response.status_code}: {response.text[:200]}"

            data = response.json()
            new_char = RosterCharacter()
            new_char.access_token = data["access_token"]
            new_char.refresh_token = data["refresh_token"]
            expires_in = data.get("expires_in", 1200)
            new_char.token_expiry = datetime.now() + timedelta(seconds=expires_in)

            if self._verify_token(new_char):
                self._upsert(new_char)
                return True, f"Logged in as {new_char.character_name}"
            return False, "Token verify failed (could not get character info)"

        except requests.RequestException as e:
            return False, f"Network error: {e}"
        finally:
            self._code_verifier = None

    def _verify_token(self, char: RosterCharacter) -> bool:
        try:
            response = requests.get(
                VERIFY_URL,
                headers={"Authorization": f"Bearer {char.access_token}"},
                timeout=30,
            )
            if response.status_code != 200:
                print(f"[IndustryRoster] Token verify HTTP {response.status_code}: {response.text[:200]}")
                return False
            data = response.json()
            char.character_id = data.get("CharacterID")
            char.character_name = data.get("CharacterName")
            return True
        except requests.RequestException as e:
            print(f"[IndustryRoster] Error verifying token: {e}")
            return False

    def _refresh_token_for(self, char: RosterCharacter) -> bool:
        if not char or not char.refresh_token:
            return False
        try:
            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": char.refresh_token,
                    "client_id": CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            if response.status_code != 200:
                print(f"[IndustryRoster] Token refresh HTTP {response.status_code}: {response.text[:200]}")
                return False
            data = response.json()
            char.access_token = data["access_token"]
            char.refresh_token = data.get("refresh_token", char.refresh_token)
            expires_in = data.get("expires_in", 1200)
            char.token_expiry = datetime.now() + timedelta(seconds=expires_in)
            self._save()
            return True
        except requests.RequestException as e:
            print(f"[IndustryRoster] Error refreshing token: {e}")
            return False

    # =========================================================================
    # TOKEN ACCESS
    # =========================================================================

    def get_valid_token(self, character_id: int) -> Optional[str]:
        """Get a valid access token for a roster character, refreshing if needed."""
        char = self.get(character_id)
        if not char or not char.access_token:
            return None
        if char.is_expired:
            if not self._refresh_token_for(char):
                return None
        return char.access_token

    def get_auth_headers(self, character_id: int) -> dict:
        token = self.get_valid_token(character_id)
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}
