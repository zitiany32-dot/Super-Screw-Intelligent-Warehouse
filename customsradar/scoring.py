"""确定性预打分：先用规则排序，再决定把 AI 预算花在谁身上。

为什么不是直接全丢给 AI：一晚上几百条线索，每条都调模型既慢又贵。
先用海关数据本身能算出来的信号排个序，只有排在前面的才进 AI 环节。
分数区间 0-100。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# 紧固件相关 HS 编码（73.18 章 = 钢铁制螺钉螺栓螺母垫圈）
RELEVANT_HS_PREFIXES = ("7318",)
SECONDARY_HS_PREFIXES = ("7317", "7319", "7326", "8302")

PRODUCT_KEYWORDS = (
    "bolt", "nut", "screw", "washer", "fastener", "stud", "rivet", "anchor",
    "threaded", "din", "iso", "hex", "flange", "螺栓", "螺母", "螺钉", "垫圈", "紧固件",
)

# 不做地域筛选：任何国家一视同仁（用户明确要求「国家没有要求」）。
# 打分只看采购活跃度、金额、品类匹配、供应链和可触达性，跟买家在哪个国家无关。


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def prescore(
    company: dict[str, Any],
    records: list[dict[str, Any]],
    today: date | None = None,
) -> tuple[int, list[str]]:
    """返回 (分数 0-100, 人话理由列表)。"""
    today = today or date.today()
    score = 0
    reasons: list[str] = []

    # --- 采购活跃度：提单票数 ---
    shipments = len(records)
    if shipments >= 6:
        score += 25
        reasons.append(f"采购频繁：{shipments} 票提单")
    elif shipments >= 3:
        score += 18
        reasons.append(f"持续采购：{shipments} 票提单")
    elif shipments >= 1:
        score += 8
        reasons.append(f"有 {shipments} 票提单记录")

    # --- 新鲜度：最近一次采购距今多久 ---
    dates = [d for d in (_parse_date(r.get("shipment_date")) for r in records) if d]
    if dates:
        days = (today - max(dates)).days
        if days <= 60:
            score += 20
            reasons.append(f"{days} 天前刚采购，正在活跃期")
        elif days <= 180:
            score += 12
            reasons.append(f"最近一次采购在 {days} 天前")
        elif days <= 365:
            score += 5
            reasons.append(f"一年内有采购（{days} 天前）")
        else:
            reasons.append(f"最近采购已是 {days} 天前，可能已换供应商或停采")

    # --- 采购金额 ---
    total_value = sum(float(r.get("value_usd") or 0) for r in records)
    if total_value >= 200_000:
        score += 20
        reasons.append(f"累计采购额约 ${total_value:,.0f}，属大单客户")
    elif total_value >= 50_000:
        score += 14
        reasons.append(f"累计采购额约 ${total_value:,.0f}")
    elif total_value > 0:
        score += 6
        reasons.append(f"累计采购额约 ${total_value:,.0f}")

    # --- 品类匹配度 ---
    hs_codes = [str(r.get("hs_code") or "") for r in records]
    products = " ".join(str(r.get("product_desc") or "").lower() for r in records)
    if any(h.startswith(RELEVANT_HS_PREFIXES) for h in hs_codes):
        score += 20
        reasons.append("HS 编码落在 7318（紧固件），与我们主营完全对口")
    elif any(h.startswith(SECONDARY_HS_PREFIXES) for h in hs_codes):
        score += 10
        reasons.append("HS 编码属五金相邻品类，有交叉销售空间")
    matched_keywords = [k for k in PRODUCT_KEYWORDS if k in products]
    if matched_keywords:
        score += 8
        reasons.append(f"产品描述命中关键词：{', '.join(matched_keywords[:5])}")

    # --- 现有供应链：已经在从中国采购的，切换成本最低 ---
    origins = " ".join(
        f"{r.get('origin') or ''} {r.get('supplier') or ''}".lower() for r in records
    )
    if "china" in origins or "中国" in origins:
        score += 10
        reasons.append("已在从中国进口，无需教育市场，直接比价即可")
    elif any(place in origins for place in ("taiwan", "vietnam", "india")):
        score += 6
        reasons.append("从亚洲其他产地进口，有替换空间")

    # --- 可触达性 ---
    if company.get("contact_email"):
        score += 8
        reasons.append("已有联系邮箱")
    if company.get("website") or company.get("domain"):
        score += 5
        reasons.append("有官网，可做背景调研")
    else:
        reasons.append("没有官网信息，触达难度高")

    return max(0, min(100, score)), reasons


def priority_from_score(score: int, high_threshold: int = 70) -> str:
    if score >= high_threshold:
        return "high"
    if score >= max(40, high_threshold - 25):
        return "medium"
    return "low"
