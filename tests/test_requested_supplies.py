"""A request's units come off the inventory tab when it is saved, into Requested Supplies (aak, 20 Nov 2026)."""
import gspread
import pytest
from fastapi.testclient import TestClient

INV_HEADER = ["Manufacturer Name", "Category", "General", "Name", "Lot Number", "Quantity",
              "Pallet Number", "Box Number", "Image URL"]


class Sheet:
    def __init__(self, rows):
        self.rows = [list(r) for r in rows]

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def row_values(self, n):
        return list(self.rows[n - 1]) if len(self.rows) >= n else []

    def append_row(self, row, value_input_option=None):
        self.rows.append([str(v) for v in row])

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.rows.extend([str(v) for v in r] for r in rows)

    def update(self, values, a1, value_input_option=None):
        assert a1 == "A1"
        if self.rows:
            self.rows[0] = list(values[0])
        else:
            self.rows.append(list(values[0]))

    def batch_update(self, updates, value_input_option=None):
        for u in updates:
            row, col = gspread.utils.a1_to_rowcol(u["range"])
            while len(self.rows[row - 1]) < col:
                self.rows[row - 1].append("")
            self.rows[row - 1][col - 1] = str(u["values"][0][0])

    def delete_rows(self, start, end=None):
        del self.rows[start - 1:(end or start)]


class Book:
    def __init__(self, sheet_id, tabs):
        self.id = sheet_id
        self.tabs = tabs

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows=0, cols=0):
        self.tabs[title] = Sheet([])
        return self.tabs[title]

    def worksheets(self):
        return list(self.tabs.values())


@pytest.fixture
def live(monkeypatch):
    import main
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("USE_SAMPLE_DATA", raising=False)
    monkeypatch.delenv("REQUESTS_SHEET_ID", raising=False)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "inv-sheet")
    monkeypatch.setenv("EMAIL_ON_SUBMIT", "false")
    inv = Sheet([INV_HEADER,
                 ["Medline", "PH", "gloves", "Exam Gloves", "L1", "10", "1", "2", "img-1"],
                 ["3M", "ER", "tape", "Medical Tape", "", "4", "5", "3", "img-2"],
                 ["Acme", "ER", "pad", "FROZEN Pt. ID 6529865018-12 PP10", "", "1", "1", "5", "img-3"]])
    book = Book("inv-sheet", {main.INVENTORY_TAB: inv, "Requests": Sheet([main.ORG_REQ_HEADERS])})

    class Client:
        def open_by_key(self, key):
            assert key == "inv-sheet"
            return book

    monkeypatch.setattr(main, "_gspread_client", lambda: Client())
    main._inventory_cache.clear()
    return TestClient(main.app), book, inv


def _send(c, *lines):
    items = [{"item_name": name, "quantity": qty, **({"sheet_row": row} if row else {})} for name, qty, row in lines]
    r = c.post("/requests", json={"org_name": "Clinic", "org_email": "c@example.org", "items": items})
    assert r.status_code == 200, r.text
    return r.json()["request_id"]


def test_a_request_takes_its_units_off_inventory_and_records_them(live):
    c, book, inv = live
    rid = _send(c, ("Exam Gloves [Lot L1, Box 2, Pallet 1]", 3, 2),
                ("Medical Tape (3M) [Lot —, Box 3, Pallet 5]", 4, 3))
    names = [r[3] for r in inv.rows[1:]]
    assert names == ["Exam Gloves", "FROZEN Pt. ID 6529865018-12 PP10"]  # tape taken whole: row gone
    assert inv.rows[1][5] == "7"  # gloves taken in part
    tab = book.tabs["Requested Supplies"].rows
    assert tab[0][:6] == ["Request ID", "Requested By", "Contact", "Request Item", "Quantity Taken", "Taken At"]
    assert tab[0][6:] == INV_HEADER
    col = tab[0].index
    assert [(r[0], r[col("Quantity Taken")], r[col("Image URL")]) for r in tab[1:]] == [(rid, "3", "img-1"),
                                                                                       (rid, "4", "img-2")]
    assert (tab[1][1], tab[1][2]) == ("Clinic", "c@example.org")  # who asked
    assert tab[1][tab[0].index("Quantity")] == "10"  # the copy keeps what the row held
    # nothing is subtracted twice: availability is what's left on the sheet
    avail = {a["item_name"]: a["remaining"] for a in c.get("/inventory/availability").json()}
    assert avail["Exam Gloves"] == 7


def test_each_request_line_records_the_item_id_of_the_row_it_was_taken_from(live):
    import main
    c, book, inv = live
    _send(c, ("Exam Gloves [Lot L1, Box 2, Pallet 1]", 3, 2),
          ("Medical Tape (3M) [Lot —, Box 3, Pallet 5]", 4, 3),
          ("Nothing Like This [Lot —, Box 9, Pallet 9]", 1, None))  # matches no row: no ID
    rows = book.tabs["Requests"].rows
    assert rows[0][-3:] == list(main.REQUEST_EXTRA_HEADERS)
    assert [r[-3:] for r in rows[1:]] == [["2", "1", "img-1"], ["3", "5", "img-2"], ["9", "9", ""]]
    # the same rows Requested Supplies recorded
    tab = book.tabs["Requested Supplies"].rows
    assert [r[tab[0].index("Image URL")] for r in tab[1:]] == ["img-1", "img-2"]


def test_rows_that_moved_are_found_by_name_and_lot_and_patient_ids_are_ignored(live):
    c, book, inv = live
    _send(c, ("FROZEN Pt. ID [removed] PP10 [Lot —, Box 5, Pallet 1]", 1, 2),  # sheet_row is stale
          ("Nothing Like This [Lot —, Box 9, Pallet 9]", 1, None))
    assert [r[3] for r in inv.rows[1:]] == ["Exam Gloves", "Medical Tape"]
    assert len(book.tabs["Requested Supplies"].rows) == 2  # header + the one line that matched


def test_two_lines_from_one_row_never_take_more_than_it_has(live):
    c, book, inv = live
    _send(c, ("Exam Gloves [Lot L1, Box 2, Pallet 1]", 6, 2), ("Exam Gloves [Lot L1, Box 2, Pallet 1]", 4, 2))
    assert inv.rows[1][3] == "Medical Tape"  # 10 taken: the gloves row is gone
    assert [r[4] for r in book.tabs["Requested Supplies"].rows[1:]] == ["6", "4"]


def test_new_requests_say_requested(live):
    c, book, _ = live
    _send(c, ("Exam Gloves [Lot L1, Box 2, Pallet 1]", 1, 2))
    assert book.tabs["Requests"].rows[1][6] == "Requested"


def test_older_lines_without_a_record_still_hold_their_units(live):
    import main
    c, book, inv = live
    book.tabs["Requests"].rows.append(["REQ-OLD", "O", "o@x.org", "Exam Gloves [Lot L1, Box 2, Pallet 1]", "PH",
                                       "2", "Under Review", "t", "FALSE"])
    main._inventory_cache.clear()
    avail = {a["item_name"]: a["remaining"] for a in c.get("/inventory/availability").json()}
    assert avail["Exam Gloves"] == 8


def test_another_spreadsheet_for_requests_takes_nothing_off(live, monkeypatch):
    import main
    c, book, inv = live
    monkeypatch.setattr(main, "_inventory_sheet_id", lambda: "some-other-sheet")
    _send(c, ("Exam Gloves [Lot L1, Box 2, Pallet 1]", 3, 2))
    assert inv.rows[1][5] == "10" and "Requested Supplies" not in book.tabs


def test_a_tab_made_before_name_and_contact_keeps_its_columns(live):
    c, book, inv = live
    old = ["Request ID", "Request Item", "Quantity Taken", "Taken At"] + INV_HEADER
    book.tabs["Requested Supplies"] = Sheet([old])
    rid = _send(c, ("Exam Gloves [Lot L1, Box 2, Pallet 1]", 3, 2))
    tab = book.tabs["Requested Supplies"].rows
    assert tab[0] == old and tab[1][:3] == [rid, "Exam Gloves [Lot L1, Box 2, Pallet 1]", "3"]
