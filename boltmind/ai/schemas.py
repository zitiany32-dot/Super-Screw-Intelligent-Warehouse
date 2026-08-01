"""AI 输出的 JSON Schema。

用 structured outputs（output_config.format）强约束模型输出，省掉「模型偶尔
返回一段散文导致解析炸掉」这类问题。注意限制：所有 object 必须
additionalProperties=false 且列全 required，不支持 minLength/maximum 之类约束。
"""

from __future__ import annotations

from typing import Any


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


COMPANY_ANALYSIS_SCHEMA: dict[str, Any] = _obj(
    {
        "profile": _obj(
            {
                "business_summary": {
                    "type": "string",
                    "description": "这家公司是做什么的，2-4 句中文。只写材料里有依据的内容。",
                },
                "likely_role": {
                    "type": "string",
                    "enum": [
                        "distributor",
                        "wholesaler",
                        "oem_manufacturer",
                        "contractor",
                        "retailer",
                        "trader",
                        "end_user",
                        "unknown",
                    ],
                    "description": "在供应链中的角色。判断不了就填 unknown。",
                },
                "products_focus": {
                    "type": "string",
                    "description": "他们采购/销售的主要品类，中文。",
                },
                "size_signal": {
                    "type": "string",
                    "description": "规模判断及依据（提单量、官网信息等）。没依据就写「材料不足」。",
                },
                "buying_pattern": {
                    "type": "string",
                    "description": "采购节奏与现有供应链特征（频率、产地、金额级别）。",
                },
                "decision_maker_guess": {
                    "type": "string",
                    "description": "该找什么岗位的人（采购经理/老板/技术），以及依据。",
                },
                "website_language": {
                    "type": "string",
                    "description": "官网主要语言的 ISO 代码，如 en/es/de/fr；不确定填 unknown。",
                },
            }
        ),
        "angles": {
            "type": "array",
            "description": "2-4 个切入点，按有效性排序。",
            "items": _obj(
                {
                    "angle": {
                        "type": "string",
                        "description": "切入点一句话概括，中文。",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "依据来自哪条材料（提单/官网某页）。不能编。",
                    },
                    "opening_line": {
                        "type": "string",
                        "description": "对应的开场白示例，用收件人的语言写。",
                    },
                }
            ),
        },
        "priority": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "值不值得今天就发信。",
        },
        "score": {
            "type": "integer",
            "description": "0-100 的成单可能性打分。",
        },
        "reasons": {
            "type": "string",
            "description": "为什么给这个优先级，中文，3 句以内。这是早上要给人看的理由。",
        },
        "risks": {
            "type": "string",
            "description": "风险或不确定点（材料不足、可能是同行、可能已有长期供应商等）。",
        },
        "recommended_contact": {
            "type": "string",
            "description": "建议发给哪个邮箱；材料里没有就填 unknown。不要编邮箱。",
        },
        "email_language": {
            "type": "string",
            "description": "开发信该用的语言 ISO 代码，如 en/es/de。",
        },
        "data_gaps": {
            "type": "string",
            "description": "还缺什么信息，人工补哪一块最值。",
        },
    }
)


COLD_EMAIL_SCHEMA: dict[str, Any] = _obj(
    {
        "subject": {
            "type": "string",
            "description": "邮件主题，用目标语言，不超过 60 字符，不要全大写、不要感叹号。",
        },
        "body": {
            "type": "string",
            "description": (
                "邮件正文纯文本，用目标语言。120-180 词，四段：切入点、我们是谁、"
                "一个具体价值点、一个低门槛的下一步。不要编造对方没有的信息。"
            ),
        },
        "language": {"type": "string", "description": "正文语言 ISO 代码。"},
        "personalization_used": {
            "type": "string",
            "description": "这封信用到的个性化事实，逐条列出来源，便于人工核对真伪。",
        },
        "rationale": {
            "type": "string",
            "description": "为什么这么写，中文两三句，给审核的人看。",
        },
        "review_flags": {
            "type": "string",
            "description": "需要人工确认的地方（不确定的事实、要补的价格/规格）。没有就写「无」。",
        },
    }
)


REPLY_ANALYSIS_SCHEMA: dict[str, Any] = _obj(
    {
        "intent": {
            "type": "string",
            "enum": [
                "interested",
                "asking_price",
                "asking_samples",
                "asking_specs",
                "not_now",
                "not_interested",
                "unsubscribe",
                "out_of_office",
                "wrong_person",
                "complaint",
                "spam_or_unrelated",
                "other",
            ],
            "description": "对方回复的主要意图。",
        },
        "urgency": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "多急需要回。",
        },
        "summary": {
            "type": "string",
            "description": "对方说了什么，中文两三句。",
        },
        "key_points": {
            "type": "array",
            "description": "对方提到的具体要求（规格、数量、认证、价格区间等）。",
            "items": {"type": "string"},
        },
        "objections": {
            "type": "string",
            "description": "对方的顾虑或异议；没有就写「无」。",
        },
        "recommended_action": {
            "type": "string",
            "description": "建议下一步怎么做，中文一句。",
        },
        "needs_human_input": {
            "type": "string",
            "description": "回信前必须由人确认的信息（报价、交期、能否做该规格）。没有就写「无」。",
        },
        "reply_subject": {
            "type": "string",
            "description": "回信主题；沿用原主题就写 Re: 原主题。",
        },
        "reply_body": {
            "type": "string",
            "description": (
                "回信草稿正文，用对方的语言。对于价格/交期这类必须人工确认的内容，"
                "用 [[待确认: ...]] 占位，绝对不要自己编数字。"
            ),
        },
        "reply_language": {"type": "string", "description": "回信语言 ISO 代码。"},
        "rationale": {
            "type": "string",
            "description": "回信策略说明，中文两三句。",
        },
    }
)
