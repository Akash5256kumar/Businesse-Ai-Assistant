#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path


NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass(frozen=True)
class InventoryRow:
    product_name: str
    category: str | None
    quantity: float
    unit: str
    last_purchase_price: float | None
    last_sale_price: float | None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "product_name": self.product_name,
            "quantity": self.quantity,
            "unit": self.unit,
        }
        if self.category is not None:
            payload["category"] = self.category
        if self.last_purchase_price is not None:
            payload["last_purchase_price"] = self.last_purchase_price
        if self.last_sale_price is not None:
            payload["last_sale_price"] = self.last_sale_price
        return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import shopkeeper inventory items from an XLSX sheet.",
    )
    parser.add_argument("--xlsx", required=True, help="Path to the source XLSX file")
    parser.add_argument(
        "--base-url",
        required=True,
        help="API base URL, for example http://168.144.112.102",
    )
    parser.add_argument("--token", required=True, help="Bearer access token")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the sheet and print summary without calling the API",
    )
    return parser.parse_args()


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for node in root.findall("a:si", NS):
        text = "".join(part.text or "" for part in node.iterfind(".//a:t", NS))
        strings.append(text)
    return strings


def _resolve_sheet_path(archive: zipfile.ZipFile) -> str:
    wb = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
    first_sheet = wb.find("a:sheets", NS)[0]
    rel_id = first_sheet.attrib[
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    ]
    target = rel_map[rel_id].lstrip("/")
    return target if target.startswith("xl/") else f"xl/{target}"


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value_node = cell.find("a:v", NS)
    inline_node = cell.find("a:is", NS)
    if cell_type == "inlineStr" and inline_node is not None:
        return "".join(part.text or "" for part in inline_node.iterfind(".//a:t", NS))
    if value_node is None or value_node.text is None:
        return ""
    value = value_node.text
    if cell_type == "s":
        return shared_strings[int(value)]
    return value


def _rows_from_xlsx(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheet_path = _resolve_sheet_path(archive)
        sheet = ET.fromstring(archive.read(sheet_path))
        rows: list[dict[str, str]] = []
        for row in sheet.findall(".//a:sheetData/a:row", NS):
            values: dict[str, str] = {}
            for cell in row.findall("a:c", NS):
                ref = cell.attrib.get("r", "")
                column = "".join(ch for ch in ref if ch.isalpha())
                values[column] = _cell_value(cell, shared_strings)
            rows.append(values)
        return rows


def _to_optional_float(value: str) -> float | None:
    stripped = value.strip()
    if not stripped:
        return None
    return float(stripped)


def load_inventory_rows(path: Path) -> list[InventoryRow]:
    raw_rows = _rows_from_xlsx(path)
    if not raw_rows:
        raise ValueError("The spreadsheet is empty.")

    header = raw_rows[0]
    expected = {
        "A": "Product Name",
        "B": "Category",
        "C": "Stock Qty",
        "D": "Unit",
        "E": "Purchase Price",
        "F": "Sale Price",
    }
    if header != expected:
        raise ValueError(
            f"Unexpected header row. Expected {expected}, found {header}."
        )

    rows: list[InventoryRow] = []
    for index, raw in enumerate(raw_rows[1:], start=2):
        product_name = raw.get("A", "").strip()
        category = raw.get("B", "").strip().lower() or None
        unit = raw.get("D", "").strip().lower() or "piece"
        if not product_name:
            raise ValueError(f"Row {index}: product name is required.")
        quantity_text = raw.get("C", "").strip()
        if not quantity_text:
            raise ValueError(f"Row {index}: stock quantity is required.")
        rows.append(
            InventoryRow(
                product_name=product_name.strip().lower(),
                category=category,
                quantity=float(quantity_text),
                unit=unit,
                last_purchase_price=_to_optional_float(raw.get("E", "")),
                last_sale_price=_to_optional_float(raw.get("F", "")),
            )
        )
    return rows


def api_request(
    url: str,
    token: str,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    body = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {url} failed with {exc.code}: {error_body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc


def list_existing_inventory(base_url: str, token: str) -> set[str]:
    response = api_request(f"{base_url.rstrip('/')}/api/v1/inventory/", token)
    items = response.get("items", [])
    if not isinstance(items, list):
        raise RuntimeError("Inventory list response does not contain an items array.")
    names: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            name = item.get("product_name")
            if isinstance(name, str):
                names.add(name.strip().lower())
    return names


def build_verification(rows: list[InventoryRow], base_url: str, token: str) -> dict[str, object]:
    actual_names = list_existing_inventory(base_url, token)
    expected_names = {row.product_name for row in rows}
    missing = sorted(expected_names - actual_names)
    return {
        "inventory_count": len(actual_names),
        "sheet_products_present": len(expected_names & actual_names),
        "missing_count": len(missing),
        "missing_sample": missing[:10],
    }


def import_inventory(
    rows: list[InventoryRow],
    base_url: str,
    token: str,
) -> dict[str, object]:
    existing_names = list_existing_inventory(base_url, token)
    created = 0
    updated = 0
    for row in rows:
        name = row.product_name
        api_request(
            f"{base_url.rstrip('/')}/api/v1/inventory/",
            token,
            method="POST",
            payload=row.to_payload(),
        )
        if name in existing_names:
            updated += 1
        else:
            created += 1
            existing_names.add(name)
    verification = build_verification(rows, base_url, token)
    return {
        "created": created,
        "updated": updated,
        "processed": len(rows),
        **verification,
    }


def main() -> int:
    args = parse_args()
    rows = load_inventory_rows(Path(args.xlsx))

    if args.dry_run:
        print(
            json.dumps(
                {
                    "processed": len(rows),
                    "sample": [row.to_payload() for row in rows[:3]],
                },
                indent=2,
            )
        )
        return 0

    summary = import_inventory(rows, args.base_url, args.token)
    print(json.dumps(summary, indent=2))
    return 0 if summary["missing_count"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
