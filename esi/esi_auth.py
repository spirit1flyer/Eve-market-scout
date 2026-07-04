"""ESI OAuth2 Authentication for EVE Market Scout.

Uses PKCE flow for secure authentication without requiring a client secret.
Client ID is hardcoded - users just click login and go.

Supports two character slots:
- Seller (primary): Always used for selling, used for buying in same-station mode
- Buyer (secondary): Used for buying in cross-hub mode only
"""

import json
import os
import subprocess
import sys
import time
import webbrowser
import threading
import requests
import secrets
import hashlib
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from typing import Optional, Dict, Callable
from datetime import datetime, timedelta

from core.config import ESI_USER_AGENT
from core.sound_manager import get_data_dir

# File paths - use centralized data directory
_data_dir = get_data_dir()
AUTH_FILE = str(_data_dir / "esi_auth.json")

# Hardcoded Client ID - PKCE flow, no secret needed
CLIENT_ID = "8b21b2ec4d9a4dffaf0a1540edd3d5d3"
CALLBACK_PORT = 8888
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}/callback"

# ESI OAuth endpoints
AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
VERIFY_URL = "https://login.eveonline.com/v2/oauth/verify"

# Scopes for market trading
REQUIRED_SCOPES = [
    "esi-wallet.read_character_wallet.v1",       # Balance + journal + transactions
    "esi-markets.read_character_orders.v1",      # Active orders
    "esi-skills.read_skills.v1",                 # Character skills for fee calculation
    "esi-characters.read_standings.v1",          # Standings for broker fee calculation
    "esi-universe.read_structures.v1",           # Resolve structure name/system/type
    "esi-markets.structure_markets.v1",          # Read orders in player-owned structures
    "esi-search.search_structures.v1",           # Find structures by name (add-station dialog)
]


def generate_code_verifier() -> str:
    """Generate a cryptographically random code verifier for PKCE."""
    return secrets.token_urlsafe(32)


def generate_code_challenge(verifier: str) -> str:
    """Generate code challenge from verifier using S256 method."""
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _open_url_robust(url: str) -> bool:
    """Open url in the user's default browser, with fallbacks.

    Three strategies in order:
      1. webbrowser.open — works in most environments
      2. ShellExecuteW via ctypes — no child process, no PATH lookup, so it
         bypasses AV/ASR rules and job-object policies that block
         CreateProcess from python.exe (which is what kills both
         webbrowser.open's startfile path and subprocess.Popen)
      3. subprocess.Popen of cmd — last resort, only useful when
         ShellExecuteW is unavailable
    """
    try:
        if webbrowser.open(url):
            return True
    except Exception as e:
        print(f"[Auth] webbrowser.open raised: {e}")

    if sys.platform == "win32":
        try:
            import ctypes
            # SW_SHOWNORMAL = 1. Return value > 32 indicates success;
            # see Win32 ShellExecute docs for the historical HINSTANCE quirk.
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "open", url, None, None, 1
            )
            if rc > 32:
                return True
            print(f"[Auth] ShellExecuteW returned {rc} (failure code)")
        except Exception as e:
            print(f"[Auth] ShellExecuteW raised: {e}")

        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except Exception as e:
            print(f"[Auth] cmd-start fallback raised: {e}")

    return False


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback from EVE SSO."""

    def log_message(self, format, *args):
        pass  # Suppress HTTP logging

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)

        if "code" in query:
            self.server.auth_code = query["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="background:#1a1a2e;color:#eee;font-family:sans-serif;text-align:center;padding-top:50px;">
                <h1>Authorization Successful!</h1>
                <p>You can close this window and return to EVE Market Scout.</p>
                </body></html>
            """)
        else:
            error = query.get("error", ["Unknown error"])[0]
            self.server.auth_code = None
            self.server.auth_error = error
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(f"""
                <html><body style="background:#1a1a2e;color:#eee;font-family:sans-serif;text-align:center;padding-top:50px;">
                <h1>Authorization Failed</h1>
                <p>Error: {error}</p>
                </body></html>
            """.encode())


# Only one login flow at a time can hold CALLBACK_PORT. server.timeout is
# per-handle_request, not overall, so without a deadline an abandoned login
# (browser closed mid-SSO) listens forever and wedges the port until app
# restart. The helper below is the single place that binds/releases the port;
# both the trading login and the industry roster login go through it.

SSO_FLOW_TIMEOUT = 300   # seconds a login may sit unanswered before giving up
_SSO_POLL_SECONDS = 2    # per-handle_request timeout: cancel/deadline latency
_SSO_BIND_WAIT = 15      # seconds to wait for a cancelled predecessor to let go

_pending_sso_server = None
_pending_sso_lock = threading.Lock()


def perform_sso_callback_flow(auth_url: str):
    """Bind the callback port, open the SSO page, wait for the redirect.

    Returns (auth_code, error) — exactly one is set. The port is released on
    every exit path, and a new call cancels a still-listening previous attempt
    instead of failing with "Address already in use".
    """
    global _pending_sso_server

    with _pending_sso_lock:
        prev = _pending_sso_server
    if prev is not None:
        prev.sso_cancelled = True  # its thread notices within _SSO_POLL_SECONDS

    server = None
    bind_deadline = time.monotonic() + _SSO_BIND_WAIT
    while server is None:
        try:
            server = HTTPServer(("localhost", CALLBACK_PORT), OAuthCallbackHandler)
        except OSError as e:
            if time.monotonic() >= bind_deadline:
                return None, (f"Port {CALLBACK_PORT} is in use "
                              f"(maybe another program): {e}")
            time.sleep(0.5)

    server.auth_code = None
    server.auth_error = None
    server.sso_cancelled = False
    server.timeout = _SSO_POLL_SECONDS

    with _pending_sso_lock:
        _pending_sso_server = server

    try:
        opened = _open_url_robust(auth_url)

        # Surface URL if auto-open failed, but keep listening: EVE redirects
        # to localhost:8888 regardless of how the auth page was reached, so
        # paste-manually completes the same flow.
        if not opened:
            print("=" * 70)
            print("BROWSER DID NOT OPEN AUTOMATICALLY.")
            print("Paste this URL into any browser to continue login:")
            print(auth_url)
            print("=" * 70)

        deadline = time.monotonic() + SSO_FLOW_TIMEOUT
        while server.auth_code is None and server.auth_error is None:
            if server.sso_cancelled:
                return None, "Login attempt replaced by a newer one"
            if time.monotonic() >= deadline:
                return None, (f"Login timed out after {SSO_FLOW_TIMEOUT // 60} "
                              "minutes - click login again to retry")
            server.handle_request()
        return server.auth_code, server.auth_error
    finally:
        with _pending_sso_lock:
            if _pending_sso_server is server:
                _pending_sso_server = None
        try:
            server.server_close()
        except Exception:
            pass


class CharacterAuth:
    """Authentication data for a single character."""

    def __init__(self, data: dict = None):
        if data:
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            expiry_str = data.get("token_expiry")
            self.token_expiry = datetime.fromisoformat(expiry_str) if expiry_str else None
            self.character_id = data.get("character_id")
            self.character_name = data.get("character_name")
        else:
            self.access_token = None
            self.refresh_token = None
            self.token_expiry = None
            self.character_id = None
            self.character_name = None

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_expiry": self.token_expiry.isoformat() if self.token_expiry else None,
            "character_id": self.character_id,
            "character_name": self.character_name,
        }

    @property
    def is_expired(self) -> bool:
        if not self.token_expiry:
            return True
        # Refresh 60s before actual expiry
        return datetime.now() >= (self.token_expiry - timedelta(seconds=60))


class ESIAuth:
    """Handles EVE SSO OAuth2 authentication using PKCE flow."""

    # Shared-instance plumbing: EVE SSO may rotate refresh tokens on use
    # (announced for native/PKCE apps), so separate instances holding
    # independent token copies can invalidate each other, and any instance's
    # _save_auth() overwrites esi_auth.json with its own possibly-stale
    # snapshot. All app code should go through ESIAuth.singleton().
    _singleton: Optional["ESIAuth"] = None
    _singleton_lock = threading.Lock()

    @classmethod
    def singleton(cls) -> "ESIAuth":
        """Return the shared process-wide ESIAuth instance."""
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls()
            return cls._singleton

    def __init__(self):
        # Two character slots
        self.seller: Optional[CharacterAuth] = None  # Primary - always used for selling
        self.buyer: Optional[CharacterAuth] = None   # Secondary - used for buying in cross-hub

        # Track which slot we're authenticating
        self._pending_slot: str = "seller"

        # PKCE: store code_verifier during auth flow
        self._code_verifier: Optional[str] = None

        # Serializes token refresh + auth-file writes (RLock: refresh holds
        # it while calling _save_auth). Concurrent refreshes with the same
        # refresh token would deauth the app once SSO token rotation is live.
        self._refresh_lock = threading.RLock()

        self._load_auth()

    # =========================================================================
    # LEGACY COMPATIBILITY - 'character' property maps to seller
    # =========================================================================
    
    @property
    def character(self) -> Optional[CharacterAuth]:
        """Legacy compatibility: returns seller character."""
        return self.seller
    
    @character.setter
    def character(self, value: Optional[CharacterAuth]):
        """Legacy compatibility: sets seller character."""
        self.seller = value

    # =========================================================================
    # LEGACY COMPATIBILITY - is_configured always True with hardcoded client
    # =========================================================================
    
    @property
    def is_configured(self) -> bool:
        """Always configured - Client ID is hardcoded."""
        return True
    
    @property
    def client_id(self) -> str:
        """Return hardcoded client ID."""
        return CLIENT_ID
    
    @property
    def callback_port(self) -> int:
        """Return callback port."""
        return CALLBACK_PORT
    
    @property
    def callback_url(self) -> str:
        """Return callback URL."""
        return CALLBACK_URL

    # =========================================================================
    # AUTH PERSISTENCE
    # =========================================================================

    def _load_auth(self):
        """Load saved auth from file."""
        if os.path.exists(AUTH_FILE):
            try:
                with open(AUTH_FILE, 'r') as f:
                    data = json.load(f)
                
                # New format: separate seller/buyer
                if "seller" in data:
                    if data["seller"]:
                        self.seller = CharacterAuth(data["seller"])
                    if data.get("buyer"):
                        self.buyer = CharacterAuth(data["buyer"])
                # Legacy format: single character -> seller
                elif "character_id" in data:
                    self.seller = CharacterAuth(data)
                    
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading ESI auth: {e}")

    def _save_auth(self):
        """Save auth to file."""
        with self._refresh_lock:
            try:
                data = {
                    "seller": self.seller.to_dict() if self.seller else None,
                    "buyer": self.buyer.to_dict() if self.buyer else None,
                }
                with open(AUTH_FILE, 'w') as f:
                    json.dump(data, f, indent=2)
            except IOError as e:
                print(f"Error saving ESI auth: {e}")

    def _adopt_saved_tokens(self, char: CharacterAuth):
        """Re-read AUTH_FILE and copy newer tokens for this character into char.

        Another running checkout (all copies share esi_auth.json) may have
        refreshed since we loaded. With SSO refresh-token rotation our
        in-memory refresh token would then be dead — adopt the saved one
        instead of burning it. No-op if the file has nothing newer.
        """
        if not char.character_id or not os.path.exists(AUTH_FILE):
            return
        try:
            with open(AUTH_FILE, 'r') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return
        for slot in ("seller", "buyer"):
            entry = data.get(slot) if isinstance(data, dict) else None
            if not entry or entry.get("character_id") != char.character_id:
                continue
            saved = CharacterAuth(entry)
            if saved.access_token and not saved.is_expired:
                # Someone else already refreshed — take the whole token set
                char.access_token = saved.access_token
                char.refresh_token = saved.refresh_token or char.refresh_token
                char.token_expiry = saved.token_expiry
            elif saved.refresh_token and saved.refresh_token != char.refresh_token:
                # Access token expired too, but the saved refresh token is
                # newer than ours — refresh with that one
                char.refresh_token = saved.refresh_token
            return

    # =========================================================================
    # CHARACTER SLOT MANAGEMENT
    # =========================================================================

    def swap_characters(self):
        """Swap seller and buyer characters."""
        self.seller, self.buyer = self.buyer, self.seller
        self._save_auth()
        print(f"Swapped characters - Seller: {self.seller_name}, Buyer: {self.buyer_name}")

    def get_character(self, slot: str) -> Optional[CharacterAuth]:
        """Get character for specified slot."""
        if slot == "buyer":
            return self.buyer
        return self.seller
    
    def logout(self, slot: str = "seller"):
        """Log out a character slot."""
        if slot == "buyer":
            self.buyer = None
        else:
            self.seller = None
        self._save_auth()

    def logout_all(self):
        """Log out all characters."""
        self.seller = None
        self.buyer = None
        self._save_auth()

    @property
    def is_authenticated(self) -> bool:
        """Check if seller (primary) is authenticated."""
        return self.seller is not None and self.seller.access_token is not None
    
    @property
    def seller_authenticated(self) -> bool:
        return self.seller is not None and self.seller.access_token is not None
    
    @property
    def buyer_authenticated(self) -> bool:
        return self.buyer is not None and self.buyer.access_token is not None
    
    @property
    def has_buyer(self) -> bool:
        """Alias for buyer_authenticated - used by GUI."""
        return self.buyer_authenticated

    @property
    def character_name(self) -> str:
        """Legacy: returns seller name."""
        return self.seller.character_name if self.seller else ""
    
    @property
    def seller_name(self) -> str:
        return self.seller.character_name if self.seller else "(not logged in)"
    
    @property
    def buyer_name(self) -> str:
        return self.buyer.character_name if self.buyer else "(not logged in)"

    @property
    def character_id(self) -> int:
        """Legacy: returns seller ID."""
        return self.seller.character_id if self.seller else 0
    
    @property
    def seller_id(self) -> int:
        return self.seller.character_id if self.seller else 0
    
    @property
    def buyer_id(self) -> int:
        return self.buyer.character_id if self.buyer else 0

    # =========================================================================
    # OAUTH FLOW (PKCE)
    # =========================================================================

    def get_auth_url(self) -> str:
        """Generate the authorization URL with PKCE challenge."""
        # Generate new PKCE verifier for this auth attempt
        self._code_verifier = generate_code_verifier()
        code_challenge = generate_code_challenge(self._code_verifier)

        scopes = " ".join(REQUIRED_SCOPES)
        params = {
            "response_type": "code",
            "redirect_uri": CALLBACK_URL,
            "client_id": CLIENT_ID,
            "scope": scopes,
            "state": "eve_market_scout",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256"
        }

        query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
        return f"{AUTH_URL}?{query}"

    def start_auth_flow(self, callback: Optional[Callable[[bool, str], None]] = None, slot: str = "seller"):
        """
        Start OAuth flow in background thread.
        
        Args:
            callback: Function(success: bool, message: str) called when complete
            slot: Which character slot to authenticate ("seller" or "buyer")
        """
        self._pending_slot = slot
        
        def do_auth():
            try:
                code, error = perform_sso_callback_flow(self.get_auth_url())

                if code:
                    success, error_detail = self._exchange_code(code)
                    if success:
                        char = self.get_character(self._pending_slot)
                        if callback:
                            callback(True, f"Logged in as {char.character_name} ({self._pending_slot})")
                    elif callback:
                        callback(False, f"Failed to exchange code: {error_detail}")
                else:
                    if callback:
                        callback(False, f"Authorization failed: {error}")

            except Exception as e:
                if callback:
                    callback(False, str(e))

        thread = threading.Thread(target=do_auth, daemon=True)
        thread.start()

    def _exchange_code(self, code: str):
        """Exchange authorization code for tokens using PKCE.
        
        Returns:
            (True, "") on success, (False, error_detail) on failure.
        """
        if not self._code_verifier:
            print("Error: No code verifier available")
            return False, "No PKCE code verifier (auth flow state lost)"
            
        try:
            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": CLIENT_ID,
                    "code_verifier": self._code_verifier
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": ESI_USER_AGENT,
                },
                timeout=30
            )
            
            if response.status_code != 200:
                body = response.text[:300]
                msg = f"Token endpoint HTTP {response.status_code}: {body}"
                print(f"Error exchanging code: {msg}")
                return False, msg
            
            data = response.json()

            new_char = CharacterAuth()
            new_char.access_token = data["access_token"]
            new_char.refresh_token = data["refresh_token"]
            expires_in = data.get("expires_in", 1200)
            new_char.token_expiry = datetime.now() + timedelta(seconds=expires_in)

            # Verify and get character info
            if self._verify_token(new_char):
                # Assign to correct slot
                if self._pending_slot == "buyer":
                    self.buyer = new_char
                else:
                    self.seller = new_char
                self._save_auth()
                return True, ""

            return False, "Token verify failed (could not get character info)"

        except requests.RequestException as e:
            print(f"Error exchanging code: {e}")
            return False, f"Network error: {e}"
        finally:
            # Clear verifier after use
            self._code_verifier = None

    def _verify_token(self, char: CharacterAuth) -> bool:
        """Verify token and get character info."""
        try:
            response = requests.get(
                VERIFY_URL,
                headers={
                    "Authorization": f"Bearer {char.access_token}",
                    "User-Agent": ESI_USER_AGENT,
                },
                timeout=30
            )
            if response.status_code != 200:
                print(f"Token verify HTTP {response.status_code}: {response.text[:200]}")
                return False
            data = response.json()

            char.character_id = data.get("CharacterID")
            char.character_name = data.get("CharacterName")
            return True

        except requests.RequestException as e:
            print(f"Error verifying token: {e}")
            return False

    def _refresh_token_for(self, char: CharacterAuth) -> bool:
        """Refresh the access token for a specific character.

        Serialized by _refresh_lock. Inside the lock we re-check expiry
        (another thread may have refreshed while we waited) and re-read the
        auth file (another running checkout may have rotated the token).
        """
        if not char or not char.refresh_token:
            return False

        with self._refresh_lock:
            if char.access_token and not char.is_expired:
                return True

            self._adopt_saved_tokens(char)
            if char.access_token and not char.is_expired:
                return True

            try:
                # PKCE refresh doesn't need code_verifier, just client_id
                response = requests.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": char.refresh_token,
                        "client_id": CLIENT_ID
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": ESI_USER_AGENT,
                    },
                    timeout=30
                )

                if response.status_code != 200:
                    print(f"Token refresh HTTP {response.status_code}: {response.text[:300]}")
                    return False

                data = response.json()

                char.access_token = data["access_token"]
                char.refresh_token = data.get("refresh_token", char.refresh_token)
                expires_in = data.get("expires_in", 1200)
                char.token_expiry = datetime.now() + timedelta(seconds=expires_in)

                self._save_auth()
                return True

            except requests.RequestException as e:
                print(f"Error refreshing token: {e}")
                return False

    def refresh_token(self) -> bool:
        """Legacy: refresh seller token."""
        return self._refresh_token_for(self.seller)

    # =========================================================================
    # TOKEN ACCESS
    # =========================================================================

    def get_valid_token(self, slot: str = "seller") -> Optional[str]:
        """Get a valid access token for specified slot, refreshing if needed."""
        char = self.get_character(slot)
        if not char or not char.access_token:
            return None

        if char.is_expired:
            if not self._refresh_token_for(char):
                return None

        return char.access_token

    def get_auth_headers(self, slot: str = "seller") -> Dict[str, str]:
        """Get headers with valid auth token for API requests."""
        token = self.get_valid_token(slot)
        if not token:
            return {}
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": ESI_USER_AGENT,
        }

    def get_seller_headers(self) -> Dict[str, str]:
        """Get auth headers for seller character."""
        return self.get_auth_headers("seller")
    
    def get_buyer_headers(self) -> Dict[str, str]:
        """Get auth headers for buyer character."""
        return self.get_auth_headers("buyer")
