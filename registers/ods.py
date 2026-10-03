"""A minimal OpenDocument spreadsheet reader (the Swedish register ships ``.ods`` files).

Reads the first sheet's rows as lists of strings (or ``None`` for empty
cells) from ``content.xml``; numbers and dates come back as their
``office:value`` / ``office:date-value`` attribute text. No extra dependency.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
#: A repeated empty cell may be repeated thousands of times; cap the expansion.
_MAX_REPEAT = 64


def sheet_rows(data: bytes) -> list[list[str | None]]:
    """The first sheet's rows, trailing empty cells dropped, empty rows skipped."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        root = ET.fromstring(archive.read("content.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"not an OpenDocument spreadsheet: {exc}") from None
    tables = list(root.iter(f"{{{_TABLE}}}table"))
    if not tables:
        raise ValueError("the spreadsheet has no sheet")
    rows: list[list[str | None]] = []
    for row in tables[0].iter(f"{{{_TABLE}}}table-row"):
        cells: list[str | None] = []
        for cell in row.findall(f"{{{_TABLE}}}table-cell"):
            repeat = int(cell.get(f"{{{_TABLE}}}number-columns-repeated", "1"))
            value = cell.get(f"{{{_OFFICE}}}date-value") or cell.get(f"{{{_OFFICE}}}value")
            if value is None:
                text = "".join(p.text or "" for p in cell.iter(f"{{{_TEXT}}}p")).strip()
                value = text or None
            cells.extend([value] * min(repeat, _MAX_REPEAT))
        while cells and cells[-1] is None:
            cells.pop()
        if cells:
            rows.append(cells)
    return rows
