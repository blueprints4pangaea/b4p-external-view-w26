"""Requested supplies come off inventory the moment a clinic asks for them (aak, 20 Nov 2026).

When a request is saved to the Local Shipment Tracker, each line's units are taken off the
"CLASSIFIED Inventorying Responses" tab and recorded in the "Requested Supplies" tab of the same
spreadsheet: one row per line, with a full copy of the inventory row it came from. A row taken
whole is deleted from inventory; a row taken in part has its Quantity lowered.

So the inventory tab (and the Hub dashboard's Total Inventory) only ever shows what is free, and
this site no longer needs to subtract open requests itself for those lines (see pulled_keys()).
The Hub dashboard puts a line's units back when a supply or request is deleted there: it adds
Quantity Taken back to the row with the same Image URL, or re-adds the copied row.

Requested Supplies columns: LEAD, then the inventory tab's own headers (the copied row).
"""
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import gspread
from gspread.utils import rowcol_to_a1

logger = logging.getLogger(__name__)

TAB = "Requested Supplies"
# Requested By and Contact: the clinic and its email, so the tab reads on its own (aak, 22 Nov 2026).
# Not "Name": that is an inventory column, copied in after these.
LEAD = ["Request ID", "Requested By", "Contact", "Request Item", "Quantity Taken", "Taken At"]

# find_row(values, item_name, sheet_row, left) -> index into values (1 = first data row) or None
FindRow = Callable[[list, str, Optional[int], dict], Optional[int]]


def to_int(value) -> Optional[int]:
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (ValueError, OverflowError):
        return None


def pulled_keys(spreadsheet) -> set:
    """(Request ID, Request Item) of every line already taken off inventory."""
    try:
        ws = spreadsheet.worksheet(TAB)
    except gspread.WorksheetNotFound:
        return set()
    values = ws.get_all_values()
    if not values or "Request ID" not in values[0] or "Request Item" not in values[0]:
        return set()
    i, j = values[0].index("Request ID"), values[0].index("Request Item")
    return {(r[i].strip(), r[j].strip()) for r in values[1:] if len(r) > j and r[i].strip()}


def _tab(spreadsheet, inventory_header: list) -> tuple:
    try:
        ws = spreadsheet.worksheet(TAB)
        header = ws.row_values(1)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=TAB, rows=1000, cols=len(LEAD) + len(inventory_header))
        header = []
    if not header:
        header = LEAD + [h for h in inventory_header if str(h).strip()]
        ws.update([header], "A1", value_input_option="RAW")
    return ws, header


def plan(values: list, lines: list, find_row: FindRow) -> tuple:
    """What to take for these lines [(item_name, quantity, sheet_row)].

    Returns (taken [(line index, inventory index, units)], new quantities {inventory index: qty}).
    A line that matches no row, or a row with no count or nothing left, takes nothing.
    """
    header = values[0] if values else []
    if "Quantity" not in header:
        return [], {}
    qcol = header.index("Quantity")
    left: dict = {}
    taken = []
    for n, (item_name, quantity, sheet_row) in enumerate(lines):
        idx = find_row(values, item_name, sheet_row, left)
        if idx is None:
            continue
        row = values[idx]
        on_hand = left.get(idx, to_int(row[qcol]) if qcol < len(row) else None)
        if on_hand is None or on_hand <= 0:
            continue
        units = min(int(quantity), on_hand)
        left[idx] = on_hand - units
        taken.append((n, idx, units))
    return taken, left


def pull(spreadsheet, inventory_title: str, request_id: str, lines: list, find_row: FindRow,
         name: str = "", contact: str = "", values: Optional[list] = None) -> int:
    """Take these lines' units off inventory and record them. Returns how many lines were taken.

    The record is written first: if the inventory write then fails, the line still counts as
    taken (nothing can be promised twice), and the inventory row needs a look by hand.
    values: the inventory tab as just read by the caller, to save reading it again.
    """
    inventory = spreadsheet.worksheet(inventory_title)
    if values is None:
        values = inventory.get_all_values()
    taken, left = plan(values, lines, find_row)
    if not taken:
        return 0
    header = values[0]
    ws, tab_header = _tab(spreadsheet, header)
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for n, idx, units in taken:
        copied = {name: (values[idx][j] if j < len(values[idx]) else "") for j, name in enumerate(header) if name}
        lead = {"Request ID": request_id, "Requested By": name, "Contact": contact, "Request Item": lines[n][0],
                "Quantity Taken": units, "Taken At": now}
        records.append([lead[c] if c in lead else copied.get(c, "") for c in tab_header])
    ws.append_rows(records, value_input_option="RAW", table_range="A1")

    qcol = header.index("Quantity") + 1
    lowered = [{"range": rowcol_to_a1(idx + 1, qcol), "values": [[qty]]} for idx, qty in left.items() if qty > 0]
    if lowered:
        inventory.batch_update(lowered, value_input_option="RAW")
    for idx in sorted((i for i, qty in left.items() if qty <= 0), reverse=True):  # bottom up: rows shift
        inventory.delete_rows(idx + 1)
    return len(taken)
