"""从海关数据商导出的 CSV / Excel 文件读线索。

大多数海关数据商（腾道、易之家、Panjiva、ImportGenius…）只提供网页下载或收费
API，没有统一标准。所以流水线的入口是「文件」：你从后台导出，丢进 data/inbox/，
剩下的自动化都不用改。列名靠别名表自动识别，识别不了就传 mapping 覆盖。
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from .base import CustomsLead

# 列名别名表：小写去空格后匹配。中英文都覆盖常见写法。
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "company_name": (
        "company", "companyname", "buyer", "buyername", "importer",
        "importername", "consignee", "consigneename", "customer",
        "采购商", "买方", "买家", "进口商", "收货人", "客户名称", "公司名称",
    ),
    "country": (
        "country", "buyercountry", "importercountry", "consigneecountry",
        "destinationcountry", "国家", "买方国家", "采购商国家", "目的国",
    ),
    "domain": ("domain", "website", "url", "homepage", "网站", "官网", "网址"),
    "contact_email": ("email", "buyeremail", "contactemail", "邮箱", "联系邮箱"),
    "direction": ("direction", "tradetype", "type", "贸易方式", "进出口"),
    "shipment_date": (
        "date", "shipmentdate", "arrivaldate", "tradedate", "transactiondate",
        "日期", "提单日期", "交易日期", "到港日期",
    ),
    "hs_code": ("hscode", "hs", "htscode", "tariffcode", "hs编码", "海关编码", "商品编码"),
    "product_desc": (
        "product", "productdescription", "description", "goods",
        "goodsdescription", "commodity", "产品", "产品描述", "商品描述", "品名",
    ),
    "quantity": ("quantity", "qty", "amount", "数量"),
    "unit": ("unit", "uom", "单位"),
    "value_usd": (
        "value", "valueusd", "amountusd", "totalvalue", "cifvalue", "fobvalue",
        "金额", "总金额", "美元金额", "成交金额",
    ),
    "supplier": (
        "supplier", "suppliername", "exporter", "exportername", "shipper",
        "shippername", "vendor", "供应商", "出口商", "发货人",
    ),
    "origin": ("origin", "origincountry", "countryoforigin", "起运国", "原产国", "起运地"),
    "destination": ("destination", "destinationport", "portofdischarge", "目的地", "目的港"),
}

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _norm_header(name: str) -> str:
    return re.sub(r"[\s_\-./()]+", "", (name or "").strip().lower())


def build_mapping(headers: Iterable[str]) -> dict[str, str]:
    """把文件表头映射成标准字段名。返回 {标准字段: 原表头}。"""
    mapping: dict[str, str] = {}
    normalized = {_norm_header(h): h for h in headers if h}
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            key = _norm_header(alias)
            if key in normalized:
                mapping[field_name] = normalized[key]
                break
    return mapping


def parse_number(value: Any) -> float | None:
    """从 "1,250 PCS" / "$3,400.00" / "3 400,50" 这类脏值里抠出数字。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "") if text.count(",") and "." in text else text
    text = text.replace(",", ".") if re.fullmatch(r"-?\d+,\d{1,2}", text) else text
    text = re.sub(r"[^\d.\-]", "", text)
    match = _NUM_RE.search(text)
    return float(match.group()) if match else None


def normalize_date(value: Any) -> str | None:
    """尽量归一成 YYYY-MM-DD，实在认不出就原样保留。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
    if m:  # 按 MM/DD/YYYY 解析（海关数据商多为美式）
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return text[:32]


def extract_domain(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/")[0].split("?")[0]
    text = text[4:] if text.startswith("www.") else text
    return text if "." in text and " " not in text else None


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as fh:
                sample = fh.read(8192)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.DictReader(fh, dialect=dialect)
                rows = [dict(r) for r in reader]
                return list(reader.fieldnames or []), rows
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法解码文件（试过 utf-8/gb18030/latin-1）: {path}")


def _read_xlsx(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        from openpyxl import load_workbook  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ImportError(
            "读 .xlsx 需要 openpyxl：pip install openpyxl（或先另存为 CSV）"
        ) from exc
    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return [], []
    headers = [str(h) if h is not None else "" for h in header_row]
    rows = []
    for raw_row in rows_iter:
        if all(cell is None or str(cell).strip() == "" for cell in raw_row):
            continue
        rows.append(dict(zip(headers, raw_row)))
    wb.close()
    return headers, rows


class CsvCustomsSource:
    """读一个文件或一整个目录（*.csv / *.xlsx）。"""

    def __init__(
        self,
        path: Path | str,
        mapping: dict[str, str] | None = None,
        direction: str = "import",
        source_name: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.mapping_override = mapping or {}
        self.direction = direction
        self.name = source_name or f"customs_file:{self.path.name}"

    def _files(self) -> list[Path]:
        if self.path.is_dir():
            return sorted(
                p
                for p in self.path.iterdir()
                if p.suffix.lower() in {".csv", ".tsv", ".xlsx", ".xlsm"}
                and not p.name.startswith(".")
            )
        return [self.path] if self.path.exists() else []

    def fetch(self) -> Iterator[CustomsLead]:
        for file_path in self._files():
            yield from self._fetch_file(file_path)

    def _fetch_file(self, file_path: Path) -> Iterator[CustomsLead]:
        if file_path.suffix.lower() in {".xlsx", ".xlsm"}:
            headers, rows = _read_xlsx(file_path)
        else:
            headers, rows = _read_csv(file_path)
        if not rows:
            return
        headers = headers or list(rows[0].keys())

        mapping = build_mapping(headers)
        mapping.update(self.mapping_override)
        if "company_name" not in mapping:
            raise ValueError(
                f"{file_path.name}: 找不到公司名列。表头={headers[:12]}。"
                f"请用 --map company_name=<列名> 指定。"
            )

        for row in rows:
            name = str(row.get(mapping["company_name"]) or "").strip()
            if not name or name.lower() in {"n/a", "na", "none", "-", "unknown"}:
                continue

            def cell(field_name: str) -> Any:
                col = mapping.get(field_name)
                if not col:
                    return None
                value = row.get(col)
                if value is None:
                    return None
                text = str(value).strip()
                return text or None

            domain = extract_domain(cell("domain"))
            yield CustomsLead(
                company_name=name,
                country=cell("country"),
                domain=domain,
                website=f"https://{domain}" if domain else None,
                contact_email=cell("contact_email"),
                direction=cell("direction") or self.direction,
                shipment_date=normalize_date(cell("shipment_date")),
                hs_code=cell("hs_code"),
                product_desc=cell("product_desc"),
                quantity=parse_number(cell("quantity")),
                unit=cell("unit"),
                value_usd=parse_number(cell("value_usd")),
                supplier=cell("supplier"),
                origin=cell("origin"),
                destination=cell("destination"),
                source=self.name,
                raw={k: (str(v) if v is not None else None) for k, v in row.items()},
            )
