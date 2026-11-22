"""One-off: fill in Box Number and Pallet Number for request lines saved before those columns existed.

Usage (from the repo root, with GOOGLE_CREDENTIALS_JSON or the service-account key set up as for
the site):
    python scripts/backfill_request_locations.py            # preview only: writes nothing
    python scripts/backfill_request_locations.py --apply    # write the cells

Optional: --sheet <spreadsheet id> (default: the live inventory spreadsheet) and
--tab <requests tab> (default: "Local Shipment Tracker").

For each line, the box and pallet come from, in order:
  1. the "[Lot X, Box Y, Pallet Z]" at the end of its Item Name (every request since the site
     started adding it);
  2. the "Requested Supplies" tab, which keeps a full copy of the inventory row each line was
     taken from (same Request ID and Item Name);
  3. the inventory tab, only when every inventory row with that item's name sits in the same box
     and pallet. Anything less certain is left blank and listed, for someone to check by hand.

Only empty cells are written, so it never overwrites a number someone typed, and running it twice
changes nothing the second time. The two column headers are added if the tab doesn't have them.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import gspread  # noqa: E402
from gspread.utils import rowcol_to_a1  # noqa: E402

import main  # noqa: E402
from app import requested_supplies  # noqa: E402

BOX, PALLET = main.LOCATION_HEADERS


def _cell(row: list, j) -> str:
    return str(row[j]).strip() if j is not None and j < len(row) else ""


def _blank(v: str) -> str:
    return "" if v in ("—", "-") else v


def _copied_locations(values: list) -> dict:
    """(Request ID, Request Item) -> (box, pallet) from the Requested Supplies tab."""
    if not values:
        return {}
    col = {str(h).strip(): j for j, h in enumerate(values[0])}
    out = {}
    for r in values[1:]:
        key = (_cell(r, col.get("Request ID")), _cell(r, col.get("Request Item")))
        loc = (_blank(_cell(r, col.get("Box Number"))), _blank(_cell(r, col.get("Pallet Number"))))
        if key[0] and any(loc):
            out.setdefault(key, loc)
    return out


def _inventory_locations(values: list) -> dict:
    """Name key -> set of (box, pallet) in the inventory tab, under every name the site shows."""
    if not values:
        return {}
    header = main._clean_header(values[0])
    col = {name: j for j, name in enumerate(header)}
    out: dict = {}
    for r in values[1:]:
        shown = main._redact_row({"Name": _cell(r, col.get("Name")), "General": _cell(r, col.get("General")),
                                  "Maker": _cell(r, col.get("Manufacturer Name"))})
        names = {main._name_key(shown["Name"]), main._name_key(shown["General"])} - {""}
        maker = main._name_key(shown["Maker"])
        keys = names | ({n + maker for n in names} if maker else set())
        loc = (_blank(_cell(r, col.get("Box Number"))), _blank(_cell(r, col.get("Pallet Number"))))
        for k in keys:
            out.setdefault(k, set()).add(loc)
    return out


def plan(request_values: list, copied_values: list, inventory_values: list) -> tuple[list, list, list]:
    """(header cells to add, cell updates, lines left blank) for the requests tab.

    Header cells and updates are {"range", "values"} dicts for batch_update; each blank line is
    (sheet row, Request ID, Item Name, reason)."""
    if not request_values:
        return [], [], []
    names = [str(h).strip() for h in request_values[0]]
    cols = {h: names.index(h) for h in (BOX, PALLET) if h in names}
    header_cells = []
    nxt = max(max((j for j, h in enumerate(names) if h), default=-1) + 1, len(main.ORG_REQ_HEADERS))
    for h in (BOX, PALLET):
        if h not in cols:
            cols[h] = nxt
            header_cells.append({"range": rowcol_to_a1(1, nxt + 1), "values": [[h]]})
            nxt += 1

    rid_j, item_j = names.index("Request ID"), names.index("Item Name")
    copied = _copied_locations(copied_values)
    inventory = _inventory_locations(inventory_values)
    updates, blank = [], []
    for i, row in enumerate(request_values[1:], start=2):
        rid, item = _cell(row, rid_j), _cell(row, item_j)
        if not rid and not item:
            continue
        have = {h: _cell(row, cols[h]) for h in (BOX, PALLET)}
        if all(have.values()):
            continue
        loc, reason = None, ""
        if main.LOT_PARTS_RE.search(item):
            loc = main._box_and_pallet(item)
        elif (rid, item) in copied:
            loc = copied[(rid, item)]
        else:
            found = inventory.get(main._name_key(main._base_item_name(item)), set())
            if len(found) == 1:
                loc = next(iter(found))
            else:
                reason = "not in inventory any more" if not found else f"in {len(found)} different boxes/pallets"
        if loc is None or not any(loc):
            blank.append((i, rid, item, reason or "no box or pallet recorded"))
            continue
        for h, v in zip((BOX, PALLET), loc):
            if v and not have[h]:
                updates.append({"range": rowcol_to_a1(i, cols[h] + 1), "values": [[main._sheet_safe(v)]]})
    return header_cells, updates, blank


def _values(spreadsheet, title: str) -> list:
    try:
        return spreadsheet.worksheet(title).get_all_values()
    except gspread.WorksheetNotFound:
        return []


def run() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="write the cells (default: preview only)")
    p.add_argument("--sheet", default=main.LIVE_INVENTORY_SHEET_ID)
    p.add_argument("--tab", default=main.LIVE_REQUESTS_TAB)
    args = p.parse_args()

    spreadsheet = main._gspread_client().open_by_key(args.sheet)
    ws = spreadsheet.worksheet(args.tab)
    request_values = ws.get_all_values()
    header_cells, updates, blank = plan(request_values, _values(spreadsheet, requested_supplies.TAB),
                                        _values(spreadsheet, main.INVENTORY_TAB))

    for h in header_cells:
        print(f"add header {h['values'][0][0]!r} at {h['range']}")
    print(f"{len(updates)} cells to fill on '{args.tab}'.")
    if blank:
        print(f"\n{len(blank)} lines left blank (check these by hand):")
        for row, rid, item, reason in blank:
            print(f"  row {row}  {rid}  {item}  ({reason})")
    if not args.apply:
        print("\nPreview only. Run again with --apply to write.")
        return
    if header_cells or updates:
        ws.batch_update(header_cells + updates, value_input_option="RAW")
    print("Done.")


if __name__ == "__main__":
    run()
