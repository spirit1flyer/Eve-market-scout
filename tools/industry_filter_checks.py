"""Product-family regressions for the IDE's Ctrl+F12 offline runner.

Representative public SDE paths exercise the manager's metadata wiring without
opening Tk, reading production caches or making network requests.
"""

from types import SimpleNamespace

from gui.industry.gui_industry import IndustryTabManager


def _check_metadata(cases):
    # Each case: public product name, item group, market path, expected flags.
    infos, paths, expected = {}, {}, {}
    for tid, (name, group, path, flags) in enumerate(cases, start=1):
        infos[tid] = SimpleNamespace(name=name, group_id=group,
                                     market_group_id=path[-1], meta_group_id=1)
        paths[path[-1]] = path
        expected[tid] = flags
    names = SimpleNamespace(
        get_type_info_bulk=lambda ids: {tid: infos[tid] for tid in ids},
        get_type_names_bulk=lambda ids: {tid: infos[tid].name for tid in ids},
        get_market_group_ancestry=lambda gid: paths[gid],
        get_market_group_name=lambda gid: str(gid))
    manager = SimpleNamespace(names=names, sde=SimpleNamespace(
        get_blueprint_for_item=lambda tid: None))
    IndustryTabManager._build_metadata(manager, list(infos))
    for tid, flags in expected.items():
        actual = (manager.capital_map[tid], manager.upwell_map[tid], manager.pos_map[tid])
        if actual != flags:
            raise AssertionError(f"{infos[tid].name}: expected {flags}, got {actual}")


def industry_capital_families(directory):
    cap, ordinary = (True, False, False), (False, False, False)
    cases = [
        ("Scout Scoped Quad 3500mm Siege Artillery", 55, (9, 10, 87, 560, 775), cap),
        ("Anode Scoped Ion Siege Blaster", 74, (9, 10, 86, 556, 771), cap),
        ("Scourge XL Torpedo", 476, (11, 114, 505, 1193), cap),
        ("XL Torpedo Launcher I", 524, (9, 10, 140, 777), cap),
        ("Gallente Phenomena Generator", 1815, (9, 10, 912, 2269), cap),
        ("'Guillotine' Thermal Reaper", 588, (9, 10, 912, 2269), cap),
        ("Tactical Capsuleer Recloner", 815, (9, 779, 1642), cap),
        ("Strategos' Modified Siege Module", 515, (9, 10, 143, 801), cap),
        ("Triage Module II", 515, (9, 10, 143, 801), cap),
        ("Capital Industrial Core I", 515, (9, 10, 143, 801), cap),
        ("Jump Portal Generator I", 590, (9, 779, 1640), cap),
        ("Industrial Jump Portal Generator I", 4184, (9, 779, 1640), cap),
        ("Capital Shield Booster I", 40, (9, 554, 552, 778), cap),
        ("Networked Sensor Array", 1706, (9, 656, 671), cap),
        ("Fighter Support Unit I", 407, (9, 938), cap),
        ("Capital Emergency Hull Energizer I", 60, (9, 14, 615), cap),
        ("Capital Tractor Beam I", 650, (9, 656, 872), cap),
        # Shared size/role labels must not exclude valid subcapital equipment.
        ("X-Large Shield Booster I", 40, (9, 554, 552, 612), ordinary),
        ("Bastion Module I", 515, (9, 10, 143, 801), ordinary),
        ("Large Industrial Core I", 515, (9, 10, 143, 801), ordinary),
        ("Medium Industrial Core II", 515, (9, 10, 143, 801), ordinary),
        ("Covert Jump Portal Generator I", 4127, (9, 779, 1640), ordinary),
        ("Sensor Booster I", 212, (9, 656, 671), ordinary),
        ("Drone Damage Amplifier I", 645, (9, 938), ordinary),
        ("Damage Control I", 60, (9, 14, 615), ordinary),
        ("Small Tractor Beam I", 650, (9, 656, 872), ordinary),
        ("Large Core Defense Field Extender I", 774, (955, 1111, 965, 1236), ordinary),
    ]
    # Every current capital rig family, not only the shield example.
    for gid in (1730, 1731, 1732, 1733, 1734, 1735, 1736, 1737, 1739, 1740,
                1785, 1789, 1793):
        cases.append((f"Capital rig family {gid}", None, (955, 1111, gid), cap))
    _check_metadata(cases)


def industry_structure_families(directory):
    sov, pos, ordinary = (False, True, False), (False, False, True), (False, False, False)
    _check_metadata([
        ("Zydrine Prospecting Array 1", 4768, (477, 1272, 1276, 1283), sov),
        ("Workforce Mecha-Tooling 3", 4838, (477, 1272, 1276, 3736), sov),
        ("Plasma Stability Generator", 4839, (477, 1272, 1276, 3741), sov),
        ("Exploration Detector 1", 4768, (477, 1272, 1276, 3739), sov),
        ("Sovereignty Hub", None, (477, 1272, 1275), sov),
        ("Orbital Skyhook", 4736, (1320, 1410), sov),
        ("Standup manufacturing rig", None, (2203, 2340, 2349), sov),
        ("Standup service module", None, (2202, 2210, 2332), sov),
        ("Standup antisubcapital missile", None, (11, 114, 2196), sov),
        ("Standup anticapital missile", None, (11, 114, 2197), sov),
        ("Standup fighter", None, (157, 2236, 2409, 2411), sov),
        ("Control Tower", None, (477, 1285, 478), pos),
        ("POS weapon battery", None, (477, 1285, 480, 479), pos),
        ("POS assembly array", None, (477, 1285, 932), pos),
        ("Giant Secure Container", None, (477, 404, 379, 1651), ordinary),
        ("Nanofiber Internal Structure I", None, (9, 14, 135, 1196), ordinary),
    ])
