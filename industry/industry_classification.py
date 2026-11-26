"""Industry product exclusions based on the SDE's product market ancestry.

IDs below describe products, not their blueprint market branches. Keep mixed
market branches narrow: XL shield boosters, Bastion, medium/large industrial
cores and covert jump portals also serve subcapital ships.
"""

# Verified against the local SDE on 2026-11-16; descendants are included.
CAPITAL_MARKET_GROUPS = frozenset({
    1381,  # Capital ships
    781, 1883,  # Standard / advanced capital components
    600, 778, 910, 1052, 1056, 1063,  # Capital repair / capacitor transfer
    2241, 2242, 2243, 2244, 2250, 2251,  # Capital tank / capacitor / energy warfare
    1730, 1731, 1732, 1733, 1734, 1735, 1736, 1737, 1739, 1740,
    1785, 1789, 1793,  # All capital rig families
    771, 772, 773, 774, 775, 776, 777, 2691,  # XL turrets / launchers
    502, 503, 504, 1004, 1006, 1007,  # Standard / faction XL ammunition
    2252, 2253, 2254, 2255, 2256, 2257, 2692, 3851,  # Advanced XL ammunition
    505, 1316,  # XL torpedoes / cruise missiles (all variants)
    912,  # Superweapons, including phenomena generators and Reapers
    1642,  # Clone vat bays / Tactical Capsuleer Recloner
    2249,  # Supercarrier burst projectors
    2410,  # Carrier-based fighters
})

UPWELL_MARKET_GROUPS = frozenset({
    2199, 2324, 2327, 3624,  # Citadels / engineering complexes / refineries / FLEX
    2202, 2203,  # Structure equipment and rigs
    1272,  # Sovereignty structures and every infrastructure upgrade
    1410,  # Orbital infrastructure, including skyhooks
    2196, 2197, 2198, 2820,  # Structure missiles / bombs / area-denial ammunition
    2409,  # Structure-based fighters
})
POS_MARKET_GROUPS = frozenset({1285})  # Towers and every POS module
# Cargo Containers (379, under Deployable Structures 404) belong to neither.
CAPITAL_TYPE_GROUPS = frozenset({
    407,  # Fighter Support Units (shared Drone Upgrades market branch)
    1706,  # Networked Sensor Array (shared Sensor Boosters market branch)
})


def classify_product(market_path, name="", group_id=None):
    """Return (capital, Upwell/sov, POS) flags for one manufactured product."""
    ancestors = frozenset(market_path)
    capital = (bool(CAPITAL_MARKET_GROUPS.intersection(ancestors))
               or group_id in CAPITAL_TYPE_GROUPS)
    lowered = name.lower()
    # Capital tractor beams / emergency hull energizers share ordinary branches.
    if 9 in ancestors:
        capital |= "capital" in lowered.split()
    # Siege Modules is shared with subcapital Bastion and medium/large cores.
    if 801 in ancestors:
        capital |= any(term in lowered for term in (
            "siege module", "triage module", "capital industrial core"))
    # Titan and Rorqual portals are capital; covert portals are for Black Ops.
    # Rorqual: eveonline.com/news/view/patch-notes-version-20-04
    if 1640 in ancestors:
        capital |= group_id in (590, 4184)
    return (capital, bool(UPWELL_MARKET_GROUPS.intersection(ancestors)),
            bool(POS_MARKET_GROUPS.intersection(ancestors)))
