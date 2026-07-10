from __future__ import annotations

import json
import os
import time
import re
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from io import BytesIO
from threading import Thread
from typing import Any
import xml.etree.ElementTree as ET

import requests


KAMIS_ENDPOINT = "https://www.kamis.or.kr/service/price/xml.do"
KAMIS_WHOLESALE_DOC_URL = "https://www.kamis.or.kr/customer/reference/openapi_list.do?action=detail&boardno=16"
KAMIS_RETAIL_DOC_URL = "https://www.kamis.or.kr/customer/reference/openapi_list.do?action=detail&boardno=17"
WORLD_BANK_PINK_SHEET_URL = "https://thedocs.worldbank.org/en/doc/5d903e848db1d1b83e0ec8f744e55570-0350012021/related/CMO-Historical-Data-Monthly.xlsx"
WORLD_BANK_COMMODITY_PAGE_URL = "https://www.worldbank.org/en/research/commodity-markets"
XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

WORLD_BANK_DEFAULT_COMMODITIES = {
    "Cocoa": "카카오",
    "Coffee, Arabica": "커피 아라비카",
    "Coffee, Robusta": "커피 로부스타",
    "Tea, avg 3 auctions": "차",
    "Coconut oil": "코코넛오일",
    "Groundnuts": "땅콩",
    "Palm oil": "팜유",
    "Soybeans": "대두",
    "Soybean oil": "대두유",
    "Soybean meal": "대두박",
    "Rapeseed oil": "유채유",
    "Sunflower oil": "해바라기유",
    "Barley": "보리",
    "Maize": "옥수수",
    "Sorghum": "수수",
    "Rice, Thai 5%": "쌀",
    "Wheat, US HRW": "밀",
    "Banana, US": "바나나",
    "Orange": "오렌지",
    "Beef **": "소고기",
    "Chicken **": "닭고기",
    "Sugar, world": "설탕",
}


@dataclass(frozen=True)
class KamisPriceItem:
    ingredient_name: str
    itemcategorycode: str
    itemcode: str
    kindcode: str
    productrankcode: str
    countrycode: str = "1101"
    price_type: str = "wholesale"
    market: str = "KAMIS 서울"
    grade: str = "상품"


@dataclass(frozen=True)
class PriceObservation:
    ingredient_name: str
    source: str
    price: float
    unit: str
    normalized_price_kg: float
    market: str
    grade: str
    observed_at: str
    status: str
    source_url: str


@dataclass(frozen=True)
class TrendSnapshot:
    change_pct: float
    low_price: float
    high_price: float
    points: int


class PriceSyncError(RuntimeError):
    pass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def configured_kamis_items() -> list[KamisPriceItem]:
    raw = os.getenv("KAMIS_PRICE_ITEMS_JSON", "").strip()
    if raw:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PriceSyncError(f"invalid_kamis_item_config: {exc}") from exc
        return [
            KamisPriceItem(
                ingredient_name=str(item["ingredient_name"]),
                itemcategorycode=str(item["itemcategorycode"]),
                itemcode=str(item["itemcode"]),
                kindcode=str(item["kindcode"]),
                productrankcode=str(item.get("productrankcode", "04")),
                countrycode=str(item.get("countrycode", "1101")),
                price_type=str(item.get("price_type", "wholesale")),
                market=str(item.get("market", "KAMIS 서울")),
                grade=str(item.get("grade", "상품")),
            )
            for item in items
        ]

    # KAMIS 공식 예시 코드와 동일한 쌀 품목을 기본값으로 둔다.
    return [
        KamisPriceItem(
            ingredient_name="쌀",
            itemcategorycode="100",
            itemcode="111",
            kindcode="01",
            productrankcode="04",
            countrycode="1101",
            price_type="wholesale",
            market="KAMIS 서울 도매",
            grade="상품",
        )
    ]


def kamis_credentials() -> tuple[str, str] | None:
    cert_key = os.getenv("KAMIS_CERT_KEY", "").strip()
    cert_id = os.getenv("KAMIS_CERT_ID", "").strip()
    if not cert_key or not cert_id:
        return None
    return cert_key, cert_id


def year_chunks(end_day: date, years: int) -> list[tuple[date, date]]:
    start_day = end_day - timedelta(days=365 * years)
    chunks: list[tuple[date, date]] = []
    current = start_day
    while current <= end_day:
        chunk_end = min(current + timedelta(days=364), end_day)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


def collect_public_crawl_prices(years: int = 5) -> tuple[list[PriceObservation], dict[str, TrendSnapshot], str]:
    observations = collect_world_bank_pink_sheet_prices(years=years)
    trends = build_trends(observations)
    summary = f"World Bank Pink Sheet {len(set(row.ingredient_name for row in observations))}개 품목, {len(observations)}개 월별 관측치 수집"
    return observations, trends, summary


def collect_world_bank_pink_sheet_prices(years: int = 5) -> list[PriceObservation]:
    response = requests.get(latest_world_bank_pink_sheet_url(), timeout=30)
    response.raise_for_status()
    return parse_world_bank_pink_sheet(response.content, years=years)


def latest_world_bank_pink_sheet_url() -> str:
    try:
        response = requests.get(WORLD_BANK_COMMODITY_PAGE_URL, timeout=20)
        response.raise_for_status()
    except requests.RequestException:
        return WORLD_BANK_PINK_SHEET_URL
    match = re.search(r'https?://[^"\']*CMO-Historical-Data-Monthly\.xlsx', response.text, re.IGNORECASE)
    return match.group(0) if match else WORLD_BANK_PINK_SHEET_URL


def configured_world_bank_commodities() -> dict[str, str]:
    raw = os.getenv("WORLD_BANK_COMMODITIES_JSON", "").strip()
    if not raw:
        return WORLD_BANK_DEFAULT_COMMODITIES
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PriceSyncError(f"invalid_world_bank_commodity_config: {exc}") from exc
    if not isinstance(loaded, dict):
        raise PriceSyncError("invalid_world_bank_commodity_config: object_required")
    return {str(source_name): str(local_name) for source_name, local_name in loaded.items()}


def parse_world_bank_pink_sheet(content: bytes, years: int = 5) -> list[PriceObservation]:
    workbook = ZipXlsx(content)
    sheet = workbook.sheet("Monthly Prices")
    headers = sheet.get(5, {})
    units = sheet.get(6, {})
    selected = configured_world_bank_commodities()
    columns = {
        col: clean_world_bank_header(name)
        for col, name in headers.items()
        if clean_world_bank_header(name) in selected
    }
    if not columns:
        raise PriceSyncError("world_bank_commodity_columns_not_found")

    min_month = month_floor(date.today(), years)
    usd_krw = env_float("WORLD_BANK_USD_KRW", 1380.0, 1.0, 100000.0)
    observations: list[PriceObservation] = []
    for row_idx in sorted(row for row in sheet if row > 6):
        row = sheet[row_idx]
        observed_at = world_bank_month_to_date(row.get(1, ""))
        if not observed_at or observed_at < min_month:
            continue
        for col, source_name in columns.items():
            price = parse_price(row.get(col))
            unit = units.get(col, "")
            if price is None or not unit:
                continue
            usd_per_kg = normalize_world_bank_price_to_kg(price, unit)
            if usd_per_kg is None:
                continue
            observations.append(
                PriceObservation(
                    ingredient_name=selected[source_name],
                    source="worldbank_pink_sheet",
                    price=price,
                    unit=unit,
                    normalized_price_kg=round(usd_per_kg * usd_krw, 2),
                    market="World Bank Commodity Markets",
                    grade=source_name,
                    observed_at=observed_at,
                    status="public_crawl",
                    source_url=WORLD_BANK_COMMODITY_PAGE_URL,
                )
            )
    return observations


class ZipXlsx:
    def __init__(self, content: bytes):
        self.archive = zipfile.ZipFile(BytesIO(content))
        self.shared_strings = self._shared_strings()
        self.sheet_paths = self._sheet_paths()

    def sheet(self, name: str) -> dict[int, dict[int, str]]:
        path = self.sheet_paths.get(name)
        if not path:
            raise PriceSyncError(f"xlsx_sheet_not_found: {name}")
        root = ET.fromstring(self.archive.read(path))
        rows: dict[int, dict[int, str]] = {}
        for row in root.findall("m:sheetData/m:row", XLSX_NS):
            row_number = int(row.attrib["r"])
            values: dict[int, str] = {}
            for cell in row.findall("m:c", XLSX_NS):
                col = column_number(cell.attrib["r"])
                values[col] = self._cell_value(cell)
            rows[row_number] = values
        return rows

    def _shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self.archive.namelist():
            return []
        root = ET.fromstring(self.archive.read("xl/sharedStrings.xml"))
        return ["".join(text.text or "" for text in item.findall(".//m:t", XLSX_NS)) for item in root.findall("m:si", XLSX_NS)]

    def _sheet_paths(self) -> dict[str, str]:
        rels_root = ET.fromstring(self.archive.read("xl/_rels/workbook.xml.rels"))
        rels = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels_root}
        workbook_root = ET.fromstring(self.archive.read("xl/workbook.xml"))
        paths: dict[str, str] = {}
        rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        for sheet in workbook_root.findall("m:sheets/m:sheet", XLSX_NS):
            target = rels[sheet.attrib[rel_ns]].lstrip("/")
            paths[sheet.attrib["name"]] = target if target.startswith("xl/") else f"xl/{target}"
        return paths

    def _cell_value(self, cell: ET.Element) -> str:
        value = cell.find("m:v", XLSX_NS)
        if value is None:
            return ""
        text = value.text or ""
        return self.shared_strings[int(text)] if cell.attrib.get("t") == "s" else text


def column_number(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref)
    if not letters:
        return 0
    value = 0
    for char in letters.group(0):
        value = value * 26 + ord(char) - ord("A") + 1
    return value


def clean_world_bank_header(value: str) -> str:
    return str(value).replace("\n", " ").strip()


def month_floor(today: date, years: int) -> str:
    return f"{today.year - years:04d}-{today.month:02d}-01"


def world_bank_month_to_date(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})M(\d{2})", text)
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}-01"


def normalize_world_bank_price_to_kg(price: float, unit: str) -> float | None:
    normalized = unit.strip().lower()
    if "$/kg" in normalized:
        return price
    if "$/mt" in normalized:
        return price / 1000
    return None


def collect_kamis_prices(years: int = 5) -> tuple[list[PriceObservation], dict[str, TrendSnapshot], str]:
    credentials = kamis_credentials()
    if not credentials:
        return [], {}, "KAMIS_CERT_KEY/KAMIS_CERT_ID 미설정"

    items = configured_kamis_items()
    today = date.today()
    observations: list[PriceObservation] = []
    for item in items:
        action = "periodRetailProductList" if item.price_type == "retail" else "periodWholesaleProductList"
        source_url = KAMIS_RETAIL_DOC_URL if item.price_type == "retail" else KAMIS_WHOLESALE_DOC_URL
        for start_day, end_day in year_chunks(today, years):
            payload = fetch_kamis_payload(action, item, credentials, start_day, end_day)
            observations.extend(parse_kamis_observations(payload, item, source_url))

    trends = build_trends(observations)
    summary = f"KAMIS {len(items)}개 품목, {len(observations)}개 관측치 수집"
    return observations, trends, summary


def fetch_kamis_payload(
    action: str,
    item: KamisPriceItem,
    credentials: tuple[str, str],
    start_day: date,
    end_day: date,
) -> Any:
    cert_key, cert_id = credentials
    params = {
        "action": action,
        "p_cert_key": cert_key,
        "p_cert_id": cert_id,
        "p_returntype": "json",
        "p_startday": start_day.isoformat(),
        "p_endday": end_day.isoformat(),
        "p_countrycode": item.countrycode,
        "p_itemcategorycode": item.itemcategorycode,
        "p_itemcode": item.itemcode,
        "p_kindcode": item.kindcode,
        "p_productrankcode": item.productrankcode,
        "p_convert_kg_yn": "Y",
    }
    response = requests.get(KAMIS_ENDPOINT, params=params, timeout=20)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise PriceSyncError("kamis_response_not_json") from exc


def parse_kamis_observations(payload: Any, item: KamisPriceItem, source_url: str) -> list[PriceObservation]:
    rows = [row for row in flatten_dicts(payload) if "price" in row and ("regday" in row or "yyyy" in row)]
    observations: list[PriceObservation] = []
    for row in rows:
        price = parse_price(row.get("price"))
        observed_at = normalize_observed_at(row)
        if price is None or not observed_at:
            continue
        market = str(row.get("marketname") or row.get("countyname") or item.market)
        kind = str(row.get("kindname") or item.grade)
        observations.append(
            PriceObservation(
                ingredient_name=str(row.get("itemname") or item.ingredient_name),
                source="kamis_api",
                price=price,
                unit="kg",
                normalized_price_kg=price,
                market=market,
                grade=kind,
                observed_at=observed_at,
                status="public_api",
                source_url=source_url,
            )
        )
    return observations


def flatten_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        rows = [value]
        for child in value.values():
            rows.extend(flatten_dicts(child))
        return rows
    if isinstance(value, list):
        rows: list[dict[str, Any]] = []
        for child in value:
            rows.extend(flatten_dicts(child))
        return rows
    return []


def parse_price(value: Any) -> float | None:
    if value is None:
        return None
    cleaned = str(value).replace(",", "").replace("원", "").strip()
    if cleaned in {"", "-", "--"}:
        return None
    try:
        price = float(cleaned)
    except ValueError:
        return None
    return price if price >= 0 else None


def normalize_observed_at(row: dict[str, Any]) -> str:
    regday = str(row.get("regday") or "").strip()
    yyyy = str(row.get("yyyy") or "").strip()
    if len(regday) == 10 and regday[4] == "-" and regday[7] == "-":
        return regday
    if yyyy and "." in regday:
        month, day = [part.zfill(2) for part in regday.split(".")[:2]]
        return f"{yyyy}-{month}-{day}"
    if yyyy and "/" in regday:
        month, day = [part.zfill(2) for part in regday.split("/")[:2]]
        return f"{yyyy}-{month}-{day}"
    return regday if regday else yyyy


def build_trends(observations: list[PriceObservation]) -> dict[str, TrendSnapshot]:
    grouped: dict[str, list[PriceObservation]] = {}
    for row in observations:
        grouped.setdefault(row.ingredient_name, []).append(row)

    trends: dict[str, TrendSnapshot] = {}
    for name, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: row.observed_at)
        if not ordered:
            continue
        start_price = ordered[0].normalized_price_kg
        end_price = ordered[-1].normalized_price_kg
        change_pct = 0.0 if start_price <= 0 else ((end_price - start_price) / start_price) * 100
        prices = [row.normalized_price_kg for row in ordered]
        trends[name] = TrendSnapshot(
            change_pct=round(change_pct, 2),
            low_price=round(min(prices), 2),
            high_price=round(max(prices), 2),
            points=len(ordered),
        )
    return trends


def start_periodic_price_sync(sync_once: Any) -> None:
    if not env_bool("PRICE_SYNC_ENABLED"):
        return

    interval_hours = env_int("PRICE_SYNC_INTERVAL_HOURS", 24, 1, 24 * 30)
    run_on_startup = env_bool("PRICE_SYNC_RUN_ON_STARTUP", True)

    def loop() -> None:
        if run_on_startup:
            sync_once()
        while True:
            time.sleep(interval_hours * 3600)
            sync_once()

    Thread(target=loop, name="price-sync-scheduler", daemon=True).start()
