"""配置：全部通过环境变量注入，代码里不放任何密钥。

用法：复制 .env.example 为 .env，填好后 `python -m customsradar.cli` 会自动加载。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载器，避免多引一个依赖。已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class SellerProfile:
    """我方公司信息 —— 写进提示词，让 AI 生成的切入点和开发信有事实依据。"""

    company_name: str = "TC-Smart Link / 永年紧固件"
    website: str = ""
    one_liner: str = "河北永年紧固件工厂，标准件与非标件生产商"
    products: str = "高强度螺母、外六角螺栓、法兰螺母、平垫圈、弹簧垫圈（M4-M10 为主）"
    differentiators: str = (
        "自有工厂直供、可做非标定制、小批量起订、DIN/ISO/ANSI 标准件齐全"
    )
    certifications: str = "ISO 9001"
    moq: str = "小批量可谈"
    lead_time: str = "常规品 15-25 天"
    sender_name: str = ""
    sender_title: str = "Sales Manager"
    sender_email: str = ""
    sender_phone: str = ""

    @classmethod
    def from_env(cls) -> "SellerProfile":
        defaults = cls()
        return cls(
            company_name=os.environ.get("SELLER_COMPANY", defaults.company_name),
            website=os.environ.get("SELLER_WEBSITE", defaults.website),
            one_liner=os.environ.get("SELLER_ONE_LINER", defaults.one_liner),
            products=os.environ.get("SELLER_PRODUCTS", defaults.products),
            differentiators=os.environ.get(
                "SELLER_DIFFERENTIATORS", defaults.differentiators
            ),
            certifications=os.environ.get(
                "SELLER_CERTIFICATIONS", defaults.certifications
            ),
            moq=os.environ.get("SELLER_MOQ", defaults.moq),
            lead_time=os.environ.get("SELLER_LEAD_TIME", defaults.lead_time),
            sender_name=os.environ.get("SELLER_SENDER_NAME", defaults.sender_name),
            sender_title=os.environ.get("SELLER_SENDER_TITLE", defaults.sender_title),
            sender_email=os.environ.get("SMTP_FROM", defaults.sender_email),
            sender_phone=os.environ.get("SELLER_SENDER_PHONE", defaults.sender_phone),
        )

    def as_prompt_block(self) -> str:
        lines = [
            f"公司名称: {self.company_name}",
            f"一句话定位: {self.one_liner}",
            f"主营产品: {self.products}",
            f"差异化优势: {self.differentiators}",
            f"资质: {self.certifications}",
            f"起订量: {self.moq}",
            f"交期: {self.lead_time}",
        ]
        if self.website:
            lines.append(f"官网: {self.website}")
        if self.sender_name:
            lines.append(f"署名: {self.sender_name} / {self.sender_title}")
        return "\n".join(lines)


@dataclass
class Config:
    # --- 存储 ---
    data_dir: Path = DEFAULT_DATA_DIR
    db_path: Path = DEFAULT_DATA_DIR / "customsradar.db"

    # --- AI ---
    anthropic_api_key: str | None = None
    model: str = "claude-opus-5"
    effort: str = "high"
    max_tokens: int = 8000
    # 每晚 AI 花费上限（美元）。超了就停止调用，当晚剩余公司留到明天。
    nightly_budget_usd: float = 5.0
    # 单价（美元 / 百万 token），claude-opus-5 = $5 / $25
    price_input_per_mtok: float = 5.0
    price_output_per_mtok: float = 25.0
    price_cache_write_per_mtok: float = 6.25
    price_cache_read_per_mtok: float = 0.5

    # --- 流水线 ---
    max_companies_per_night: int = 20
    crawl_timeout: int = 15
    crawl_max_pages: int = 5
    crawl_delay_seconds: float = 1.0
    user_agent: str = "CustomsRadarBot/0.1 (+contact via website; B2B research)"
    respect_robots: bool = True
    high_priority_threshold: int = 70

    # --- 邮箱发现 ---
    # 免费来源（官网、按人名生成模式）始终开。下面这些要 key 或开关才启用。
    discover_whois: bool = True          # 系统有 whois 命令就用，没有自动跳过
    hunter_api_key: str = ""             # Hunter.io，B2B 找邮箱行业标配
    search_engine: str = ""              # serpapi / bing / brave
    search_api_key: str = ""
    discover_timeout: int = 12
    # 校验：MX 便宜且安全，默认开；SMTP 探测有风险（伤发信信誉），默认关。
    verify_mx: bool = True
    verify_smtp: bool = False
    verify_smtp_min_confidence: int = 40  # 只对够可信的候选做 SMTP 探测
    verify_smtp_max: int = 20             # 单轮 SMTP 探测次数上限，限速保信誉

    # --- 邮件 ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    # 总开关：为 False 时 send() 直接拒绝发送，只能存草稿。
    allow_send: bool = False

    seller: SellerProfile = field(default_factory=SellerProfile)

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        _load_dotenv(env_file or (REPO_ROOT / ".env"))
        data_dir = Path(os.environ.get("RADAR_DATA_DIR", str(DEFAULT_DATA_DIR)))
        defaults = cls()
        return cls(
            data_dir=data_dir,
            db_path=Path(os.environ.get("RADAR_DB", str(data_dir / "customsradar.db"))),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            model=os.environ.get("RADAR_MODEL", defaults.model),
            effort=os.environ.get("RADAR_EFFORT", defaults.effort),
            max_tokens=_env_int("RADAR_MAX_TOKENS", defaults.max_tokens),
            nightly_budget_usd=_env_float(
                "RADAR_NIGHTLY_BUDGET_USD", defaults.nightly_budget_usd
            ),
            price_input_per_mtok=_env_float(
                "RADAR_PRICE_INPUT", defaults.price_input_per_mtok
            ),
            price_output_per_mtok=_env_float(
                "RADAR_PRICE_OUTPUT", defaults.price_output_per_mtok
            ),
            max_companies_per_night=_env_int(
                "RADAR_MAX_COMPANIES", defaults.max_companies_per_night
            ),
            crawl_timeout=_env_int("RADAR_CRAWL_TIMEOUT", defaults.crawl_timeout),
            crawl_max_pages=_env_int(
                "RADAR_CRAWL_MAX_PAGES", defaults.crawl_max_pages
            ),
            crawl_delay_seconds=_env_float(
                "RADAR_CRAWL_DELAY", defaults.crawl_delay_seconds
            ),
            user_agent=os.environ.get("RADAR_USER_AGENT", defaults.user_agent),
            respect_robots=_env_bool("RADAR_RESPECT_ROBOTS", True),
            discover_whois=_env_bool("DISCOVER_WHOIS", True),
            hunter_api_key=os.environ.get("HUNTER_API_KEY", ""),
            search_engine=os.environ.get("SEARCH_ENGINE", ""),
            search_api_key=os.environ.get("SEARCH_API_KEY", ""),
            discover_timeout=_env_int("DISCOVER_TIMEOUT", defaults.discover_timeout),
            verify_mx=_env_bool("DISCOVER_VERIFY_MX", True),
            verify_smtp=_env_bool("DISCOVER_VERIFY_SMTP", False),
            verify_smtp_min_confidence=_env_int(
                "DISCOVER_VERIFY_SMTP_MIN_CONFIDENCE",
                defaults.verify_smtp_min_confidence,
            ),
            verify_smtp_max=_env_int(
                "DISCOVER_VERIFY_SMTP_MAX", defaults.verify_smtp_max
            ),
            high_priority_threshold=_env_int(
                "RADAR_HIGH_PRIORITY_THRESHOLD", defaults.high_priority_threshold
            ),
            smtp_host=os.environ.get("SMTP_HOST", ""),
            smtp_port=_env_int("SMTP_PORT", defaults.smtp_port),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_from=os.environ.get("SMTP_FROM", ""),
            smtp_starttls=_env_bool("SMTP_STARTTLS", True),
            imap_host=os.environ.get("IMAP_HOST", ""),
            imap_port=_env_int("IMAP_PORT", defaults.imap_port),
            imap_user=os.environ.get("IMAP_USER", ""),
            imap_password=os.environ.get("IMAP_PASSWORD", ""),
            imap_folder=os.environ.get("IMAP_FOLDER", defaults.imap_folder),
            allow_send=_env_bool("RADAR_ALLOW_SEND", False),
            seller=SellerProfile.from_env(),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
