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

    # 单号提取正则 (如 #101, 101, 单号101)
    REQ_CODE_PATTERN = re.compile(r"(?:#|单号|编号)?\s*([0-9]{3,6})")

    @classmethod
    def extract_amount_and_note(
        cls,
        text: str,
        ignore_qqs: Optional[List[str]] = None,
        target_names: Optional[List[str]] = None
    ) -> Tuple[float, str]:
        """
        从文本中精准提取金额并清洗出事由备注。
        会自动过滤已知 QQ 号、@ 标签以及无意义的连接词。
        """
        ignore_qqs = [str(q).strip() for q in (ignore_qqs or []) if str(q).strip()]
        target_names = [str(n).strip() for n in (target_names or []) if str(n).strip()]

        # 1. 过滤消息格式中的 At 标签及括号里的 QQ 号，防止误当做交易金额
        cleaned = re.sub(r"\[[Aa]t:\s*\d+\]", " ", text)
        cleaned = re.sub(r"@[\w\u4e00-\u9fa5\s]+\(\s*\d+\s*\)", " ", cleaned)
        cleaned = re.sub(r"@\d+", " ", cleaned)

        for q in ignore_qqs:
            if q:
                cleaned = re.sub(rf"(?<!\d){re.escape(q)}(?!\d)", " ", cleaned)

        # 2. 优先匹配紧跟在借贷动词后的金额（如 欠我33, 借给100, 借了50, 还了20, 垫付30）
        verb_amount_match = re.search(
            r"(?:欠我|欠了我|欠|借给|借出|借了|借入|还给|还了|已还|垫付|付了|借)\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块钱|块|rmb|RMB|￥|¥)?",
            cleaned
        )
        amount_val = 0.0
        raw_note = cleaned

        if verb_amount_match:
            amount_val = float(verb_amount_match.group(1))
            start, end = verb_amount_match.span()
            raw_note = (cleaned[:start] + " " + cleaned[end:]).strip()
        else:
            # 匹配明确带货币单位的金额（如 33元, 50.5块, 100块钱, ¥50）
            unit_match = re.search(r"(?<!\d)(\d+(?:\.\d{1,2})?)\s*(?:元|块钱|块|rmb|RMB|￥|¥)", cleaned)
            if unit_match:
                amount_val = float(unit_match.group(1))
                start, end = unit_match.span()
                raw_note = (cleaned[:start] + " " + cleaned[end:]).strip()
            else:
                # 匹配常规纯数字（限制整数部分不超过 6 位，彻底排除 7-12 位的 QQ 号）
                num_matches = list(re.finditer(r"(?<!\d)(\d+(?:\.\d{1,2})?)(?!\d)", cleaned))
                valid_matches = [m for m in num_matches if len(m.group(1).split(".")[0]) <= 6]
                if valid_matches:
                    m = valid_matches[0]
                    amount_val = float(m.group(1))
                    start, end = m.span()
                    raw_note = (cleaned[:start] + " " + cleaned[end:]).strip()

        # 3. 清理备注中的 @ 提及、人名、动词及标点
        note = re.sub(r"@\S+", " ", raw_note)
        for name in target_names:
            if name:
                note = note.replace(name, " ")

        note = re.sub(
            r"(?:我借给|我借出|借给|借出给|转借给|借了|欠我|欠了我|欠我钱|我向|我跟|我从|向|我欠|我欠了|欠了|我还给|我还了|已还给|还清给|还款给|还给|转账给|归还给|已还|帮|垫付|垫了|垫付了|付了|买了|给我|给你|给|向|从|用于|因为|事由|备注|理由|为了|的|钱)+",
            " ",
            note
        )
        note = re.sub(r"[，。！？,!?~@()（）\[\]\s]+", " ", note).strip()
        return amount_val, note

    @classmethod
    def parse_message(
        cls,
        text: str,
        mentioned_qq_list: Optional[List[str]] = None,
        sender_id: str = "",
        bot_id: str = "",
        target_names: Optional[List[str]] = None
    ) -> ParsedIntent:
        """
        解析用户自然语言消息
        :param text: 消息纯文本
        :param mentioned_qq_list: 消息中被 @ 的非机器人 QQ 列表
        :param sender_id: 发送者 QQ
        :param bot_id: 机器人自身 QQ
        :param target_names: 目标用户可能包含的昵称
        """
        text_clean = text.strip()
        # 排除机器人和发送者自己
        valid_targets = [
            str(q).strip() for q in (mentioned_qq_list or [])
            if str(q).strip() and str(q).strip() != str(sender_id) and str(q).strip() != str(bot_id)
        ]
        target_qq = valid_targets[0] if valid_targets else ""

        # 如果没有通过 @ 提取到，尝试从文本中寻找非自身/非机器人的 QQ 号
        if not target_qq:
            qq_matches = re.findall(r"(?<!\d)(\d{5,12})(?!\d)", text_clean)
            for qm in qq_matches:
                if qm != str(sender_id) and qm != str(bot_id):
                    target_qq = qm
                    break

        ignore_qqs = list(set([str(sender_id), str(bot_id), str(target_qq)] + valid_targets))

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
            amt, note = cls.extract_amount_and_note(text_clean, ignore_qqs=ignore_qqs, target_names=target_names)
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
            amt, note = cls.extract_amount_and_note(text_clean, ignore_qqs=ignore_qqs, target_names=target_names)
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
            amt, note = cls.extract_amount_and_note(text_clean, ignore_qqs=ignore_qqs, target_names=target_names)
            return ParsedIntent(
                intent_type="LEND",
                target_qq=target_qq,
                amount=amt,
                note=note,
                raw_text=text_clean
            )

        return ParsedIntent(intent_type="UNKNOWN", target_qq=target_qq, raw_text=text_clean)
