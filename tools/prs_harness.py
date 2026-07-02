"""Manual smoke test for esi_structures.py.

Three modes:

    python prs_harness.py list [seller|buyer]
        Enumerate every player-structure location_id found in your active
        orders, resolve each to a name, print a picker table.

    python prs_harness.py search <name> [seller|buyer]
        Search for structures by name fragment via ESI character search.
        Requires esi-search.search_structures.v1 scope.

    python prs_harness.py <structure_id> [seller|buyer]
        Verify the chosen slot can read that specific structure.

Exits non-zero on any failure so it can be wired into a check later.
"""

# standalone script: make repo root importable when run directly
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import sys
import socket
from collections import Counter

# Workaround: on this machine socket.getaddrinfo fails when called from
# urllib3 even though it works standalone. socket.gethostbyname works
# reliably. Reroute getaddrinfo through gethostbyname and synthesize the
# tuple urllib3 expects. Must run before any HTTP library is imported.
_orig_getaddrinfo = socket.getaddrinfo

def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        ip = socket.gethostbyname(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, proto or 6, "", (ip, port))]
    except Exception:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = _patched_getaddrinfo

import urllib3.util.connection
urllib3.util.connection.HAS_IPV6 = False

from esi.esi_auth import ESIAuth, REQUIRED_SCOPES
from esi.esi_wallet import ESIWallet
from esi.esi_structures import (
    fetch_structure_info,
    fetch_structure_orders,
    search_structures_by_name,
    StructureAccessError,
)


STRUCTURE_ID_FLOOR = 1_000_000_000_000  # NPC stations are 8-digit; player structures are 13+


def cmd_list(auth: "ESIAuth", slot: str) -> int:
    wallet = ESIWallet(auth)
    # ESIWallet uses the seller slot internally via auth.character_id; rebind
    # if the caller asked for buyer so we list orders for the right character.
    if slot == "buyer":
        # Tiny shim: ESIWallet reads auth.character_id (a property pointing at
        # seller). For buyer enumeration, query the orders endpoint directly.
        import requests
        from esi.esi_wallet import BASE_URL
        headers = auth.get_buyer_headers()
        char_id = auth.buyer_id
        if not headers or not char_id:
            print(f"[FAIL] buyer not authenticated")
            return 1
        try:
            resp = requests.get(f"{BASE_URL}/characters/{char_id}/orders/", headers=headers, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[FAIL] could not fetch buyer orders: {e}")
            return 1
        location_ids = {o["location_id"] for o in resp.json()}
    else:
        wallet.fetch_orders()
        location_ids = {o.location_id for o in wallet.orders}

    structure_ids = sorted(lid for lid in location_ids if lid >= STRUCTURE_ID_FLOOR)
    if not structure_ids:
        print(f"No player-structure orders found for {slot}. Try the other slot,")
        print("or use option 1 from the chat (in-game drag-to-chat link).")
        return 0

    print(f"Found {len(structure_ids)} player structure(s) in {slot}'s active orders:\n")
    print(f"  {'structure_id':>15}  {'system':>10}  name")
    print(f"  {'-' * 15}  {'-' * 10}  {'-' * 40}")
    for sid in structure_ids:
        try:
            info = fetch_structure_info(sid, auth, slot=slot)
            print(f"  {sid:>15}  {info.solar_system_id:>10}  {info.name}")
        except StructureAccessError as e:
            print(f"  {sid:>15}  {'?':>10}  <unreachable: HTTP {e.status}>")
    print(f"\nRun: python prs_harness.py <structure_id> {slot}")
    return 0


def cmd_search(query: str, auth: "ESIAuth", slot: str) -> int:
    try:
        ids = search_structures_by_name(query, auth, slot=slot)
    except StructureAccessError as e:
        print(f"[FAIL] search: {e}")
        if e.status in (401, 403):
            print("       Likely cause: token lacks esi-search.search_structures.v1.")
            print("       Re-log this slot so the new scope is granted.")
        return 1

    if not ids:
        print(f"No structures found matching '{query}'.")
        return 0

    print(f"Found {len(ids)} structure(s) matching '{query}':\n")
    print(f"  {'structure_id':>15}  {'system':>10}  name")
    print(f"  {'-' * 15}  {'-' * 10}  {'-' * 40}")
    for sid in ids:
        try:
            info = fetch_structure_info(sid, auth, slot=slot)
            print(f"  {sid:>15}  {info.solar_system_id:>10}  {info.name}")
        except StructureAccessError as e:
            print(f"  {sid:>15}  {'?':>10}  <unreachable: HTTP {e.status}>")
    return 0


REQUIRED_FOR_STRUCTURES = {
    "esi-universe.read_structures.v1",
    "esi-markets.structure_markets.v1",
}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage:")
        print("  python prs_harness.py list [seller|buyer]")
        print("  python prs_harness.py search <name> [seller|buyer]")
        print("  python prs_harness.py <structure_id> [seller|buyer]")
        return 2

    first = sys.argv[1]

    if first == "search":
        if len(sys.argv) < 3:
            print("usage: python prs_harness.py search <name> [seller|buyer]")
            return 2
        query = sys.argv[2]
        slot = sys.argv[3] if len(sys.argv) >= 4 else "seller"
    else:
        slot = sys.argv[2] if len(sys.argv) >= 3 else "seller"

    if slot not in ("seller", "buyer"):
        print(f"slot must be 'seller' or 'buyer', got {slot!r}")
        return 2

    auth = ESIAuth()
    char = auth.get_character(slot)
    if not char or not char.access_token:
        print(f"[FAIL] {slot} slot is not authenticated — log in via the app first.")
        return 1
    print(f"[OK]   {slot} = {char.character_name} (id={char.character_id})")

    if first == "list":
        return cmd_list(auth, slot)
    if first == "search":
        return cmd_search(query, auth, slot)

    try:
        structure_id = int(first)
    except ValueError:
        print(f"structure_id must be an integer, 'list', or 'search', got {first!r}")
        return 2

    # Sanity-check the constant — if the scopes were dropped from REQUIRED_SCOPES
    # the harness would silently pass on stale tokens until re-auth.
    missing_in_constant = REQUIRED_FOR_STRUCTURES - set(REQUIRED_SCOPES)
    if missing_in_constant:
        print(f"[WARN] REQUIRED_SCOPES is missing: {missing_in_constant}")
        print("       New logins will not request these — fetcher will 403.")

    print(f"\n--- fetch_structure_info({structure_id}) ---")
    try:
        info = fetch_structure_info(structure_id, auth, slot=slot)
    except StructureAccessError as e:
        print(f"[FAIL] {e}")
        if e.status == 403:
            print("       Likely cause: token lacks esi-universe.read_structures.v1,")
            print("       or character has no docking access to this structure.")
            print("       Fix: re-log this slot so new scopes are granted.")
        elif e.status == 404:
            print("       Structure not found — wrong ID, or it has been destroyed.")
        return 1
    print(f"[OK]   name={info.name!r}")
    print(f"       solar_system_id={info.solar_system_id}  type_id={info.type_id}")
    print(f"       owner_id={info.owner_id}")

    print(f"\n--- fetch_structure_orders({structure_id}) ---")
    try:
        orders, expires = fetch_structure_orders(structure_id, auth, slot=slot)
    except StructureAccessError as e:
        print(f"[FAIL] {e}")
        if e.status == 403:
            print("       Likely cause: token lacks esi-markets.structure_markets.v1.")
            print("       Universe read worked but market read did not — re-log to grant.")
        return 1

    buys = sum(1 for o in orders if o.get("is_buy_order"))
    sells = len(orders) - buys
    unique_types = len({o.get("type_id") for o in orders})
    print(f"[OK]   {len(orders)} orders ({buys} buy / {sells} sell) "
          f"across {unique_types} item types")
    print(f"       cache expires: {expires}")

    if orders:
        sample = orders[0]
        keys = sorted(sample.keys())
        print(f"       sample order keys: {keys}")
        common = Counter(o.get("type_id") for o in orders).most_common(3)
        print(f"       top type_ids by order count: {common}")

    print("\n[PASS] structure is reachable and readable for this slot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
