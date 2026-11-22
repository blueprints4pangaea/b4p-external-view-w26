"""scripts/backfill_request_locations.py: planning only, no sheet is touched."""
import importlib.util
from pathlib import Path

import main

spec = importlib.util.spec_from_file_location(
    "backfill", Path(__file__).resolve().parent.parent / "scripts" / "backfill_request_locations.py")
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)

H = list(main.ORG_REQ_HEADERS)


def line(rid, item, *extra):
    return [rid, "Clinic", "c@x.org", item, "C", "1", "Requested", "t", "FALSE", *extra]


def test_plan_fills_from_name_then_copy_then_unique_inventory():
    requests_tab = [H,
                    line("REQ-1", "Gauze [Lot L1, Box 4, Pallet 2]"),
                    line("REQ-2", "Tape"),            # in Requested Supplies
                    line("REQ-3", "Gloves"),          # one place in inventory
                    line("REQ-4", "Syringe"),         # two places: left blank
                    line("REQ-5", "Mask"),            # gone from inventory
                    line("REQ-6", "Gauze [Lot —, Box —, Pallet 9]")]
    copied = [["Request ID", "Request Item", "Quantity Taken", "Taken At", "Name", "Box Number", "Pallet Number"],
              ["REQ-2", "Tape", "1", "t", "Tape", "7", "3"]]
    inventory = [["Name", "Box Number", "Pallet Number", "Quantity"],
                 ["Gloves", "5", "1", "10"], ["Gloves", "5", "1", "4"],
                 ["Syringe", "1", "1", "3"], ["Syringe", "2", "1", "3"]]
    header_cells, updates, blank = backfill.plan(requests_tab, copied, inventory)
    assert [(h["range"], h["values"][0][0]) for h in header_cells] == [("J1", "Box Number"), ("K1", "Pallet Number")]
    got = {u["range"]: u["values"][0][0] for u in updates}
    assert got == {"J2": "4", "K2": "2", "J3": "7", "K3": "3", "J4": "5", "K4": "1", "K7": "9"}
    assert [(r, reason) for r, _, _, reason in blank] == [
        (5, "in 2 different boxes/pallets"), (6, "not in inventory any more")]


def test_plan_never_overwrites_and_is_idempotent():
    requests_tab = [H + ["Box Number", "Pallet Number"],
                    line("REQ-1", "Gauze [Lot L1, Box 4, Pallet 2]", "99", ""),
                    line("REQ-2", "Gauze [Lot L1, Box 4, Pallet 2]", "4", "2")]
    header_cells, updates, blank = backfill.plan(requests_tab, [], [])
    assert header_cells == [] and blank == []
    assert [(u["range"], u["values"][0][0]) for u in updates] == [("K2", "2")]
