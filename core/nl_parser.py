from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ParsedIntent:
    """自然语言意图解析结果"""
    intent_type: str  # 'LEND', 'BORROW', 'REPAY', 'QUERY_PAIR', 'QUERY_SELF', 'ACCEPT', 'REJECT', 'REVOKE', 'PENDING', 'HELP', 'UNKNOWN'
    target_qq: str = ""
    amount: float = 0.0
    note: str = ""
    req_code: str = ""
    raw_text: str = ""


class NaturalLanguageParser:
    """
    借贷记账自然语言解析引擎
    精准提取用户 @机器人 对话中的意图、目标 QQ、金额、事由及单号。
    """

    # 金额提取正则 (如 100, 50.5, 30元, 100.00块, 50块钱)
    AMOUNT_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d{1,2})?)\s*(?:元|块钱|块|rmb|RMB|￥|¥)?(?!\d)")
    # 单号提取正则 (如 #101, 101, 单号101)
    REQ_CODE_PATTERN = re.compile(r"(?:#|单号|编号)?\s*([0-9]{3,6})")

    @classmethod
    def extract_amount_and_note(cls, text: str) -> Tuple[float, str]:
        """从文本中提取金额并清洗出事由备注"""
        # 寻找匹配的金额
        matches = list(cls.AMOUNT_PATTERN.finditer(text))
        if not matches:
            return 0.0, text.strip()

        # 优先选取最符合语义的金额项
        selected_match = matches[0]
        amount_val = float(selected_match.group(1))

        # 从原文中剔除金额部分，剩余部分作为事由备注
        start, end = selected_match.span()
        note = (text[:start] + " " + text[end:]).strip()
        # 清理多余介词和标点
        note = re.sub(r"^(?:用于|因为|事由|备注|理由|为了|买|去|吃|喝|喝的|吃的|打车|的)+", "", note).strip()
        note = re.sub(r"[，。！？,!?~]+", " ", note).strip()
        return amount_val, note

    @classmethod
    def parse_message(
        cls,
        text: str,
        mentioned_qq_list: Optional[List[str]] = None
    ) -> ParsedIntent:
        """
        解析用户自然语言消息
        :param text: 消息纯文本（已过滤掉@Bot自身）
        :param mentioned_qq_list: 消息中额外 @到的非机器人 QQ 列表（按顺序）
        """
        text_clean = text.strip()
        target_qq = mentioned_qq_list[0] if mentioned_qq_list else ""

        # 1. 帮助意图
        if re.search(r"^(?:记账帮助|怎么记账|记账使用说明|记账指令|记账怎么用|账本帮助)$", text_clean):
            return ParsedIntent(intent_type="HELP", raw_text=text_clean)

        # 2. 同意 / 确认意图
        if re.search(r"^(?:同意|确认|通过|接受|确认入账|同意借款|同意还款|好|好的|没问题|行|可以)(?:\s|$|[0-9#])", text_clean):
            code_match = cls.REQ_CODE_PATTERN.search(text_clean)
            req_code = code_match.group(1) if code_match else ""
            return ParsedIntent(intent_type="ACCEPT", req_code=req_code, raw_text=text_clean)

        # 3. 拒绝意图
        if re.search(r"^(?:拒绝|不同意|不认账|驳回|假的|不对|算错了)(?:\s|$|[0-9#])", text_clean):
            code_match = cls.REQ_CODE_PATTERN.search(text_clean)
            req_code = code_match.group(1) if code_match else ""
            return ParsedIntent(intent_type="REJECT", req_code=req_code, raw_text=text_clean)

        # 4. 撤销意图
        if re.search(r"^(?:撤销|撤回|取消|取消申请)(?:\s|$|[0-9#])", text_clean):
            code_match = cls.REQ_CODE_PATTERN.search(text_clean)
            req_code = code_match.group(1) if code_match else ""
            return ParsedIntent(intent_type="REVOKE", req_code=req_code, raw_text=text_clean)

        # 5. 待办查询意图
        if re.search(r"^(?:待办|待我确认|待确认|我的待办|待处理申请|未决申请)$", text_clean):
            return ParsedIntent(intent_type="PENDING", raw_text=text_clean)

        # 6. 全局查总账意图
        if re.search(r"^(?:我的账单|我的总账|查总账|我现在欠谁钱|谁欠我钱|账本|我的欠款|总账|借贷总览|账目总览)$", text_clean):
            return ParsedIntent(intent_type="QUERY_SELF", raw_text=text_clean)

        # 7. 查双人账意图
        if (
            re.search(r"(?:查.*?(?:账|欠款|借还)|对账|账单明细|明细|谁欠谁|算账|账目)", text_clean)
            or re.search(r"^(?:查|看看|看下)", text_clean)
        ) and target_qq:
            return ParsedIntent(intent_type="QUERY_PAIR", target_qq=target_qq, raw_text=text_clean)

        # 8. 还款记录 (我已还给对方 / 记录还款)
        repay_pattern = re.compile(
            r"(?:我还给|我还了|已还给|还清给|还款给|还给|转账给|归还给|已还)"
        )
        if repay_pattern.search(text_clean):
            amt, note = cls.extract_amount_and_note(text_clean)
            # 清理匹配词
            note = repay_pattern.sub("", note).strip()
            return ParsedIntent(
                intent_type="REPAY",
                target_qq=target_qq,
                amount=amt,
                note=note,
                raw_text=text_clean
            )

        # 9. 借入记录 (我向对方借钱 / 我欠对方钱)
        borrow_pattern = re.compile(
            r"(?:我向|我跟|我从|向).+(?:借了|借入|借)|(?:我欠|我欠了|欠了)"
        )
        if borrow_pattern.search(text_clean):
            amt, note = cls.extract_amount_and_note(text_clean)
            note = re.sub(r"(?:我向|我跟|我从|向|借了|借入|借|我欠|我欠了|欠了)", "", note).strip()
            return ParsedIntent(
                intent_type="BORROW",
                target_qq=target_qq,
                amount=amt,
                note=note,
                raw_text=text_clean
            )

        # 10. 借出记录 (我借给对方 / 对方欠我 / 帮对方垫付)
        lend_pattern = re.compile(
            r"(?:我借给|我借出|借给|借出给|转借给|借了)|(?:欠我|欠了我|欠我钱)|(?:帮).+(?:垫付|垫了|垫付了|付了|买了)"
        )
        if lend_pattern.search(text_clean):
            amt, note = cls.extract_amount_and_note(text_clean)
            note = re.sub(r"(?:我借给|我借出|借给|借出给|转借给|借了|欠我|欠了我|欠我钱|帮|垫付|垫了|垫付了|付了|买了)", "", note).strip()
            return ParsedIntent(
                intent_type="LEND",
                target_qq=target_qq,
                amount=amt,
                note=note,
                raw_text=text_clean
            )

        return ParsedIntent(intent_type="UNKNOWN", target_qq=target_qq, raw_text=text_clean)
