from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools, register

from .core.database import DatabaseManager
from .core.ledger_service import LedgerService
from .core.nl_parser import NaturalLanguageParser, ParsedIntent
from .core.request_manager import RequestManager
from .core.text_formatter import TextFormatter


@register(
    "astrbot_plugin_debt_ledger",
    "eskyfun",
    "聊天记账与双人债务管理插件。支持借款/还款双向申请、双方确认生效、精确到秒的流水日志记录，并基于QQ号跨群统一汇总。",
    "1.0.0",
    "https://github.com/eskyfun/astrbot_plugin_debt_ledger"
)
class DebtLedgerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = context.get_config() or {}

        # 配置项读取
        self.timeout_seconds = int(self.config.get("timeout_seconds", 600))
        self.currency_symbol = str(self.config.get("currency_symbol", "¥"))
        self.enable_nl = bool(self.config.get("enable_natural_language", True))
        self.enable_llm_tool = bool(self.config.get("enable_llm_tool", True))
        self.max_single_amount = float(self.config.get("max_single_amount", 1000000.0))

        # 持久化数据库初始化
        try:
            data_dir = Path(StarTools.get_data_dir()) / "astrbot_plugin_debt_ledger"
        except Exception:
            data_dir = Path("data") / "astrbot_plugin_debt_ledger"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "ledger.db"

        self.db = DatabaseManager(db_path)
        self.ledger_service = LedgerService(self.db)
        self.request_manager = RequestManager(self.db, self.ledger_service, self.timeout_seconds)

    async def initialize(self):
        """插件初始化"""
        logger.info(f"[DebtLedger] 记账与债务管理插件已就绪，超时时间: {self.timeout_seconds}s，货币符号: {self.currency_symbol}")

    # ==========================================
    # 辅助方法：提取事件中的发送者、群号与被 @ 的目标 QQ
    # ==========================================

    def _get_bot_self_id(self, event: AstrMessageEvent) -> str:
        """获取当前机器人自身的 QQ/平台 ID"""
        try:
            if hasattr(event, "get_self_id") and callable(event.get_self_id):
                bot_id = str(event.get_self_id() or "").strip()
                if bot_id:
                    return bot_id
        except Exception:
            pass
        try:
            bot_id = str(getattr(event.message_obj, "self_id", "") or "").strip()
            if bot_id:
                return bot_id
        except Exception:
            pass
        return str(getattr(self.context, "self_id", "") or "").strip()

    def _get_sender_info(self, event: AstrMessageEvent) -> Tuple[str, str, str]:
        """返回 (sender_id, sender_name, group_id)"""
        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or sender_id).strip()
        group_id = str(event.get_group_id() or "").strip()
        # 更新用户昵称
        if sender_id:
            self.db.update_user_name(sender_id, sender_name)
        return sender_id, sender_name, group_id

    def _extract_target_qq_and_name(self, event: AstrMessageEvent, text_param: str = "") -> Tuple[str, str]:
        """
        从消息组件或文本参数中智能提取被@的目标 QQ 与昵称
        """
        bot_id = self._get_bot_self_id(event)
        sender_id = str(event.get_sender_id() or "").strip()

        # 1. 优先从消息组件中的 At 提取
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    qq = str(getattr(comp, "qq", "") or getattr(comp, "target", "") or getattr(comp, "user_id", "")).strip()
                    if qq and qq != bot_id and qq != sender_id:
                        name = str(getattr(comp, "name", "") or getattr(comp, "display", "") or "").strip()
                        if not name:
                            name = self.db.get_user_name(qq, qq)
                        else:
                            self.db.update_user_name(qq, name)
                        return qq, name

        # 2. 从文本参数中正则匹配 QQ 号 (5-12位数字)
        if text_param:
            qq_matches = re.findall(r"(?<!\d)(\d{5,12})(?!\d)", text_param)
            for qm in qq_matches:
                if qm != bot_id and qm != sender_id:
                    target_name = self.db.get_user_name(qm, qm)
                    return qm, target_name

        return "", ""

    def _extract_all_mentioned_qqs(self, event: AstrMessageEvent) -> List[str]:
        """提取消息中所有非机器人自身且非发送者本人的被 @ 用户 QQ 列表"""
        bot_id = self._get_bot_self_id(event)
        sender_id = str(event.get_sender_id() or "").strip()
        qq_list = []
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    qq = str(getattr(comp, "qq", "") or getattr(comp, "target", "") or getattr(comp, "user_id", "")).strip()
                    if qq and qq != bot_id and qq != sender_id and qq not in qq_list:
                        qq_list.append(qq)
        return qq_list

    # ==========================================
    # 指令实现区：借出 / 借入 / 还款
    # ==========================================

    @filter.command("借出", alias={"借给", "出借", "lend"})
    async def cmd_lend(self, event: AstrMessageEvent, text: str = ""):
        """
        发起出借申请：/借出 @某人 [金额] [事由]
        """
        sender_id, sender_name, group_id = self._get_sender_info(event)
        target_id, target_name = self._extract_target_qq_and_name(event, text)

        if not target_id:
            yield event.plain_result("❌ 请 @你要借给的好友 或 输入其QQ号，例如：/借出 @张三 100 晚餐AA")
            return

        bot_id = self._get_bot_self_id(event)
        amt, note = NaturalLanguageParser.extract_amount_and_note(
            text, ignore_qqs=[sender_id, target_id, bot_id], target_names=[target_name]
        )
        if amt <= 0.001:
            yield event.plain_result("❌ 请提供有效的借出金额，例如：/借出 @张三 100 晚餐AA")
            return

        if amt > self.max_single_amount:
            yield event.plain_result(f"❌ 单笔金额超出上限（最大允许 {self.currency_symbol}{self.max_single_amount:.2f}）。")
            return

        # 记录发起方出借给接收方 (Lender=Sender, Borrower=Target)
        ok, req_data, msg = self.request_manager.create_request(
            proposer_id=sender_id,
            proposer_name=sender_name,
            target_id=target_id,
            target_name=target_name,
            lender_id=sender_id,
            borrower_id=target_id,
            amount=amt,
            record_type="BORROW",
            note=note,
            origin_group_id=group_id,
            timeout_seconds=self.timeout_seconds
        )

        if not ok or not req_data:
            yield event.plain_result(f"❌ 发起失败: {msg}")
            return

        resp_text = TextFormatter.format_request_created(req_data, self.currency_symbol)
        yield event.plain_result(resp_text)

    @filter.command("借入", alias={"欠款", "借款", "borrow"})
    async def cmd_borrow(self, event: AstrMessageEvent, text: str = ""):
        """
        发起借入申请（我向对方借钱/我欠对方）：/借入 @某人 [金额] [事由]
        """
        sender_id, sender_name, group_id = self._get_sender_info(event)
        target_id, target_name = self._extract_target_qq_and_name(event, text)

        if not target_id:
            yield event.plain_result("❌ 请 @出借人 或 输入其QQ号，例如：/借入 @李四 50 垫付车费")
            return

        bot_id = self._get_bot_self_id(event)
        amt, note = NaturalLanguageParser.extract_amount_and_note(
            text, ignore_qqs=[sender_id, target_id, bot_id], target_names=[target_name]
        )
        if amt <= 0.001:
            yield event.plain_result("❌ 请提供有效的借入金额，例如：/借入 @李四 50 垫付车费")
            return

        if amt > self.max_single_amount:
            yield event.plain_result(f"❌ 单笔金额超出上限（最大允许 {self.currency_symbol}{self.max_single_amount:.2f}）。")
            return

        # 记录发起方向目标借入 (Lender=Target, Borrower=Sender)
        ok, req_data, msg = self.request_manager.create_request(
            proposer_id=sender_id,
            proposer_name=sender_name,
            target_id=target_id,
            target_name=target_name,
            lender_id=target_id,
            borrower_id=sender_id,
            amount=amt,
            record_type="BORROW",
            note=note,
            origin_group_id=group_id,
            timeout_seconds=self.timeout_seconds
        )

        if not ok or not req_data:
            yield event.plain_result(f"❌ 发起失败: {msg}")
            return

        resp_text = TextFormatter.format_request_created(req_data, self.currency_symbol)
        yield event.plain_result(resp_text)

    @filter.command("还款", alias={"已还", "平账", "repay"})
    async def cmd_repay(self, event: AstrMessageEvent, text: str = ""):
        """
        发起还款申请（记录我已还款给对方）：/还款 @某人 [金额] [事由]
        """
        sender_id, sender_name, group_id = self._get_sender_info(event)
        target_id, target_name = self._extract_target_qq_and_name(event, text)

        if not target_id:
            yield event.plain_result("❌ 请 @收款人 或 输入其QQ号，例如：/还款 @张三 100 微信已转")
            return

        bot_id = self._get_bot_self_id(event)
        amt, note = NaturalLanguageParser.extract_amount_and_note(
            text, ignore_qqs=[sender_id, target_id, bot_id], target_names=[target_name]
        )
        if amt <= 0.001:
            yield event.plain_result("❌ 请提供有效的还款金额，例如：/还款 @张三 100 微信已转")
            return

        if amt > self.max_single_amount:
            yield event.plain_result(f"❌ 单笔金额超出上限（最大允许 {self.currency_symbol}{self.max_single_amount:.2f}）。")
            return

        # 记录发起方还款给目标 (收款人 Lender=Target, 还款人 Borrower=Sender)
        ok, req_data, msg = self.request_manager.create_request(
            proposer_id=sender_id,
            proposer_name=sender_name,
            target_id=target_id,
            target_name=target_name,
            lender_id=target_id,
            borrower_id=sender_id,
            amount=amt,
            record_type="REPAY",
            note=note,
            origin_group_id=group_id,
            timeout_seconds=self.timeout_seconds
        )

        if not ok or not req_data:
            yield event.plain_result(f"❌ 发起失败: {msg}")
            return

        resp_text = TextFormatter.format_request_created(req_data, self.currency_symbol)
        yield event.plain_result(resp_text)

    @filter.command("还清", alias={"结清", "两清", "settle", "clear"})
    async def cmd_settle(self, event: AstrMessageEvent, text: str = ""):
        """
        一键发起结清全部欠款申请：/还清 @某人 [备注]
        """
        sender_id, sender_name, group_id = self._get_sender_info(event)
        target_id, target_name = self._extract_target_qq_and_name(event, text)

        if not target_id:
            yield event.plain_result("❌ 请 @你要还清欠款的好友，例如：/还清 @张三 微信已转")
            return

        # 查两人当前实时净欠款
        summary = self.ledger_service.calculate_pair_debt(sender_id, target_id, sender_name, target_name)
        # net_balance > 0 说明 target 欠 sender
        # net_balance < 0 说明 sender 欠 target
        debt_i_owe = -summary["net_balance"]
        if debt_i_owe <= 0.001:
            if summary["net_balance"] > 0:
                yield event.plain_result(f"💡 当前是 {target_name} 净欠您 {self.currency_symbol}{summary['net_balance']:.2f}，无需由您发起还清。")
            else:
                yield event.plain_result(f"💡 您与 {target_name} 当前账目已两清，无任何待还欠款。")
            return

        bot_id = self._get_bot_self_id(event)
        _, note = NaturalLanguageParser.extract_amount_and_note(
            text, ignore_qqs=[sender_id, target_id, bot_id], target_names=[target_name]
        )
        if not note:
            note = "结清全部欠款"

        ok, req_data, msg = self.request_manager.create_request(
            proposer_id=sender_id,
            proposer_name=sender_name,
            target_id=target_id,
            target_name=target_name,
            lender_id=target_id,
            borrower_id=sender_id,
            amount=debt_i_owe,
            record_type="REPAY",
            note=note,
            origin_group_id=group_id,
            timeout_seconds=self.timeout_seconds
        )

        if not ok or not req_data:
            yield event.plain_result(f"❌ 发起还清失败: {msg}")
            return

        resp_text = TextFormatter.format_request_created(req_data, self.currency_symbol)
        yield event.plain_result(resp_text)

    # ==========================================
    # 指令实现区：同意 / 拒绝 / 撤销
    # ==========================================

    @filter.command("同意", alias={"确认", "通过", "accept"})
    async def cmd_accept(self, event: AstrMessageEvent, req_code: str = ""):
        """
        被申请人确认并同意申请：/同意 [单号]
        """
        sender_id, sender_name, group_id = self._get_sender_info(event)
        code_match = re.search(r"(\d{3,6})", req_code)
        code_val = code_match.group(1) if code_match else None

        ok, req_data, summary, msg = self.request_manager.accept_request(
            operator_id=sender_id,
            req_code=code_val,
            group_id=group_id
        )

        if not ok or not req_data:
            yield event.plain_result(f"❌ 操作失败: {msg}")
            return

        resp_text = TextFormatter.format_request_accepted(req_data, summary, self.currency_symbol)
        yield event.plain_result(resp_text)

    @filter.command("拒绝", alias={"不同意", "驳回", "reject"})
    async def cmd_reject(self, event: AstrMessageEvent, text: str = ""):
        """
        被申请人拒绝申请：/拒绝 [单号] [理由]
        """
        sender_id, sender_name, group_id = self._get_sender_info(event)
        code_match = re.search(r"(\d{3,6})", text)
        code_val = code_match.group(1) if code_match else None

        ok, req_data, msg = self.request_manager.reject_request(
            operator_id=sender_id,
            req_code=code_val,
            group_id=group_id
        )

        if not ok or not req_data:
            yield event.plain_result(f"❌ 拒绝失败: {msg}")
            return

        yield event.plain_result(f"🚫 申请 #{req_data['req_code']} 已被 @{sender_name} 拒绝，该记录已作废。")

    @filter.command("撤销", alias={"撤回", "取消申请", "revoke"})
    async def cmd_revoke(self, event: AstrMessageEvent, text: str = ""):
        """
        发起人撤销未确认申请：/撤销 [单号]
        """
        sender_id, sender_name, group_id = self._get_sender_info(event)
        code_match = re.search(r"(\d{3,6})", text)
        code_val = code_match.group(1) if code_match else None

        ok, req_data, msg = self.request_manager.revoke_request(
            operator_id=sender_id,
            req_code=code_val,
            group_id=group_id
        )

        if not ok or not req_data:
            yield event.plain_result(f"❌ 撤销失败: {msg}")
            return

        yield event.plain_result(f"↩️ 发起人 @{sender_name} 已主动撤销申请 #{req_data['req_code']}。")

    # ==========================================
    # 指令实现区：查账 / 我的账单 / 对账 / 待办 / 帮助
    # ==========================================

    @filter.command("查账", alias={"双人对账", "查欠款"})
    async def cmd_query_pair(self, event: AstrMessageEvent, text: str = ""):
        """
        查询双人净欠款与最近流水：/查账 @某人
        """
        sender_id, sender_name, _ = self._get_sender_info(event)
        target_id, target_name = self._extract_target_qq_and_name(event, text)

        if not target_id:
            yield event.plain_result("❌ 请 @你要对账的好友 或 输入其QQ号，例如：/查账 @张三")
            return

        summary = self.ledger_service.calculate_pair_debt(sender_id, target_id, sender_name, target_name)
        resp_text = TextFormatter.format_pair_debt_summary(summary, self.currency_symbol)
        yield event.plain_result(resp_text)

    @filter.command("我的账单", alias={"总账", "账本", "借贷总览"})
    async def cmd_query_self(self, event: AstrMessageEvent):
        """
        跨群统计个人全局借出/借入大盘：/我的账单
        """
        sender_id, sender_name, _ = self._get_sender_info(event)
        overview = self.ledger_service.get_user_overview(sender_id, sender_name)
        resp_text = TextFormatter.format_user_overview(overview, self.currency_symbol)
        yield event.plain_result(resp_text)

    @filter.command("对账", alias={"账单明细", "流水明细"})
    async def cmd_query_history(self, event: AstrMessageEvent, text: str = ""):
        """
        分页查看双人历史流水明细：/对账 @某人 [页码]
        """
        sender_id, sender_name, _ = self._get_sender_info(event)
        target_id, target_name = self._extract_target_qq_and_name(event, text)

        if not target_id:
            yield event.plain_result("❌ 请 @你要对账的好友，例如：/对账 @张三 1")
            return

        # 提取页码
        page = 1
        page_match = re.search(r"(?:第\s*)?(\d+)(?:\s*页)?$", text.strip())
        if page_match:
            try:
                page = max(1, int(page_match.group(1)))
            except Exception:
                page = 1

        records, total_pages, summary = self.ledger_service.get_pair_history_paged(
            sender_id, target_id, page=page, page_size=6
        )
        resp_text = TextFormatter.format_pair_history_paged(records, page, total_pages, summary, self.currency_symbol)
        yield event.plain_result(resp_text)

    @filter.command("待办", alias={"待我确认", "我的待办"})
    async def cmd_pending(self, event: AstrMessageEvent):
        """
        查看当前未决申请：/待办
        """
        sender_id, sender_name, _ = self._get_sender_info(event)
        waiting_me, my_proposed = self.request_manager.get_user_pending_overview(sender_id)
        resp_text = TextFormatter.format_pending_list(waiting_me, my_proposed, self.currency_symbol)
        yield event.plain_result(resp_text)

    @filter.command("记账帮助", alias={"债务帮助", "账本帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """
        查看帮助与使用指南：/记账帮助
        """
        yield event.plain_result(TextFormatter.format_help())

    # ==========================================
    # 自然语言 @机器人 智能分发引擎
    # ==========================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_event(self, event: AstrMessageEvent):
        """
        监听所有消息，若开启自然语言且包含借贷关键词/@Bot，则智能处理
        """
        if not self.enable_nl:
            return

        raw_str = event.message_str.strip()
        if not raw_str:
            return

        # 若是斜杠指令开头，则跳过避免重复触发
        if raw_str.startswith("/") or raw_str.startswith("／"):
            return

        # 检查是否包含借贷记账核心意图关键词
        keywords = ["借给", "借出", "借入", "欠我", "我欠", "还给", "已还", "借了", "查账", "我的账单", "谁欠我", "同意", "拒绝", "撤销", "垫付"]
        if not any(kw in raw_str for kw in keywords):
            return

        # 提取发送者、机器人自身及被 @ 的非机器人目标 QQ
        sender_id, sender_name, group_id = self._get_sender_info(event)
        bot_id = self._get_bot_self_id(event)
        mentioned_qqs = self._extract_all_mentioned_qqs(event)
        parsed = NaturalLanguageParser.parse_message(
            raw_str,
            mentioned_qq_list=mentioned_qqs,
            sender_id=sender_id,
            bot_id=bot_id
        )

        if parsed.intent_type == "UNKNOWN":
            return

        target_id = parsed.target_qq
        target_name = self.db.get_user_name(target_id, target_id) if target_id else ""

        # 意图分发
        if parsed.intent_type == "LEND":
            if not target_id:
                yield event.plain_result("❌ 未检测到被借款人，请在语句中 @对方，例如：@Bot 我借给 @张三 100元 吃火锅")
                return
            if parsed.amount <= 0.001:
                yield event.plain_result("❌ 未检测到借款金额，请说明金额，例如：@Bot 我借给 @张三 100元")
                return
            ok, req_data, msg = self.request_manager.create_request(
                proposer_id=sender_id, proposer_name=sender_name,
                target_id=target_id, target_name=target_name,
                lender_id=sender_id, borrower_id=target_id,
                amount=parsed.amount, record_type="BORROW",
                note=parsed.note, origin_group_id=group_id,
                timeout_seconds=self.timeout_seconds
            )
            if ok and req_data:
                yield event.plain_result(TextFormatter.format_request_created(req_data, self.currency_symbol))
            else:
                yield event.plain_result(f"❌ 发起借款失败: {msg}")

        elif parsed.intent_type == "BORROW":
            if not target_id:
                yield event.plain_result("❌ 未检测到出借人，请在语句中 @对方，例如：@Bot 我向 @李四 借了 50元 垫付车费")
                return
            if parsed.amount <= 0.001:
                yield event.plain_result("❌ 未检测到借入金额，请说明金额，例如：@Bot 我向 @李四 借了 50元")
                return
            ok, req_data, msg = self.request_manager.create_request(
                proposer_id=sender_id, proposer_name=sender_name,
                target_id=target_id, target_name=target_name,
                lender_id=target_id, borrower_id=sender_id,
                amount=parsed.amount, record_type="BORROW",
                note=parsed.note, origin_group_id=group_id,
                timeout_seconds=self.timeout_seconds
            )
            if ok and req_data:
                yield event.plain_result(TextFormatter.format_request_created(req_data, self.currency_symbol))
            else:
                yield event.plain_result(f"❌ 发起借款失败: {msg}")

        elif parsed.intent_type == "REPAY":
            if not target_id:
                yield event.plain_result("❌ 未检测到收款人，请在语句中 @对方，例如：@Bot 我还给 @张三 100元 微信已转")
                return
            if parsed.amount <= 0.001:
                yield event.plain_result("❌ 未检测到还款金额，请说明金额，例如：@Bot 我还给 @张三 100元")
                return
            ok, req_data, msg = self.request_manager.create_request(
                proposer_id=sender_id, proposer_name=sender_name,
                target_id=target_id, target_name=target_name,
                lender_id=target_id, borrower_id=sender_id,
                amount=parsed.amount, record_type="REPAY",
                note=parsed.note, origin_group_id=group_id,
                timeout_seconds=self.timeout_seconds
            )
            if ok and req_data:
                yield event.plain_result(TextFormatter.format_request_created(req_data, self.currency_symbol))
            else:
                yield event.plain_result(f"❌ 发起还款失败: {msg}")

        elif parsed.intent_type == "RECEIVE_REPAY":
            if not target_id:
                yield event.plain_result("❌ 未检测到还款人，请在语句中 @对方，例如：@Bot @张三 还了我 50元")
                return
            if parsed.amount <= 0.001:
                yield event.plain_result("❌ 未检测到还款金额，请说明金额，例如：@Bot @张三 还了我 50元")
                return
            ok, req_data, msg = self.request_manager.create_request(
                proposer_id=sender_id, proposer_name=sender_name,
                target_id=target_id, target_name=target_name,
                lender_id=sender_id, borrower_id=target_id,
                amount=parsed.amount, record_type="REPAY",
                note=parsed.note, origin_group_id=group_id,
                timeout_seconds=self.timeout_seconds
            )
            if ok and req_data:
                yield event.plain_result(TextFormatter.format_request_created(req_data, self.currency_symbol))
            else:
                yield event.plain_result(f"❌ 发起还款记录失败: {msg}")

        elif parsed.intent_type == "SETTLE":
            if not target_id:
                yield event.plain_result("❌ 请在语句中 @你要结清账目的人，例如：@Bot 我还清了 @张三")
                return
            summary = self.ledger_service.calculate_pair_debt(sender_id, target_id, sender_name, target_name)
            debt_i_owe = -summary["net_balance"]
            if debt_i_owe <= 0.001:
                if summary["net_balance"] > 0:
                    yield event.plain_result(f"💡 当前是 {target_name} 净欠您 {self.currency_symbol}{summary['net_balance']:.2f}，无需发起还清。")
                else:
                    yield event.plain_result(f"💡 您与 {target_name} 当前账目已两清，无任何待还欠款。")
                return
            ok, req_data, msg = self.request_manager.create_request(
                proposer_id=sender_id, proposer_name=sender_name,
                target_id=target_id, target_name=target_name,
                lender_id=target_id, borrower_id=sender_id,
                amount=debt_i_owe, record_type="REPAY",
                note=parsed.note or "结清全部欠款", origin_group_id=group_id,
                timeout_seconds=self.timeout_seconds
            )
            if ok and req_data:
                yield event.plain_result(TextFormatter.format_request_created(req_data, self.currency_symbol))
            else:
                yield event.plain_result(f"❌ 发起还清失败: {msg}")

        elif parsed.intent_type == "ACCEPT":
            ok, req_data, summary, msg = self.request_manager.accept_request(
                operator_id=sender_id, req_code=parsed.req_code, group_id=group_id
            )
            if ok and req_data:
                yield event.plain_result(TextFormatter.format_request_accepted(req_data, summary, self.currency_symbol))
            else:
                yield event.plain_result(f"❌ 确认失败: {msg}")

        elif parsed.intent_type == "REJECT":
            ok, req_data, msg = self.request_manager.reject_request(
                operator_id=sender_id, req_code=parsed.req_code, group_id=group_id
            )
            if ok and req_data:
                yield event.plain_result(f"🚫 申请 #{req_data['req_code']} 已被拒绝并作废。")
            else:
                yield event.plain_result(f"❌ 拒绝失败: {msg}")

        elif parsed.intent_type == "REVOKE":
            ok, req_data, msg = self.request_manager.revoke_request(
                operator_id=sender_id, req_code=parsed.req_code, group_id=group_id
            )
            if ok and req_data:
                yield event.plain_result(f"↩️ 已撤回申请 #{req_data['req_code']}。")
            else:
                yield event.plain_result(f"❌ 撤销失败: {msg}")

        elif parsed.intent_type == "QUERY_PAIR":
            summary = self.ledger_service.calculate_pair_debt(sender_id, target_id, sender_name, target_name)
            yield event.plain_result(TextFormatter.format_pair_debt_summary(summary, self.currency_symbol))

        elif parsed.intent_type == "QUERY_SELF":
            overview = self.ledger_service.get_user_overview(sender_id, sender_name)
            yield event.plain_result(TextFormatter.format_user_overview(overview, self.currency_symbol))

        elif parsed.intent_type == "PENDING":
            waiting_me, my_proposed = self.request_manager.get_user_pending_overview(sender_id)
            yield event.plain_result(TextFormatter.format_pending_list(waiting_me, my_proposed, self.currency_symbol))

        elif parsed.intent_type == "HELP":
            yield event.plain_result(TextFormatter.format_help())

    # ==========================================
    # LLM Function Calling 原生工具注册
    # ==========================================

    @filter.llm_tool(name="debt_propose_request")
    async def tool_propose_debt(
        self,
        event: AstrMessageEvent,
        action_type: str,
        target_qq: str,
        amount: float,
        note: str = ""
    ) -> str:
        """发起借款、出借或还款申请。

        Args:
            action_type(string): 操作类型，可选 'LEND'(我借给对方/对方欠我), 'BORROW'(我向对方借/我欠对方), 'REPAY'(我还款给对方)
            target_qq(string): 交易对方的 QQ 号
            amount(number): 交易金额（正数）
            note(string): 事由或备注说明
        """
        sender_id, sender_name, group_id = self._get_sender_info(event)
        target_name = self.db.get_user_name(target_qq, target_qq)

        if action_type == "LEND":
            lender_id, borrower_id = sender_id, target_qq
            rec_type = "BORROW"
        elif action_type == "BORROW":
            lender_id, borrower_id = target_qq, sender_id
            rec_type = "BORROW"
        elif action_type == "REPAY":
            lender_id, borrower_id = target_qq, sender_id
            rec_type = "REPAY"
        else:
            return "错误：不支持的操作类型。"

        ok, req_data, msg = self.request_manager.create_request(
            proposer_id=sender_id, proposer_name=sender_name,
            target_id=target_qq, target_name=target_name,
            lender_id=lender_id, borrower_id=borrower_id,
            amount=amount, record_type=rec_type,
            note=note, origin_group_id=group_id,
            timeout_seconds=self.timeout_seconds
        )

        if ok and req_data:
            return TextFormatter.format_request_created(req_data, self.currency_symbol)
        return f"发起申请失败: {msg}"

    @filter.llm_tool(name="debt_confirm_request")
    async def tool_confirm_debt(
        self,
        event: AstrMessageEvent,
        accept: bool,
        req_code: str = ""
    ) -> str:
        """同意或拒绝待确认的借贷/还款申请。

        Args:
            accept(boolean): True 表示同意入账，False 表示拒绝作废
            req_code(string): 可选的申请单号（如 '101'），若为空则自动匹配最新一笔
        """
        sender_id, sender_name, group_id = self._get_sender_info(event)
        if accept:
            ok, req_data, summary, msg = self.request_manager.accept_request(
                operator_id=sender_id, req_code=req_code or None, group_id=group_id
            )
            if ok and req_data:
                return TextFormatter.format_request_accepted(req_data, summary, self.currency_symbol)
            return f"确认失败: {msg}"
        else:
            ok, req_data, msg = self.request_manager.reject_request(
                operator_id=sender_id, req_code=req_code or None, group_id=group_id
            )
            if ok and req_data:
                return f"已成功拒绝申请 #{req_data['req_code']}。"
            return f"拒绝失败: {msg}"

    @filter.llm_tool(name="debt_query_summary")
    async def tool_query_debt(
        self,
        event: AstrMessageEvent,
        target_qq: str = ""
    ) -> str:
        """查询借贷账目对账单或全局借贷大盘。

        Args:
            target_qq(string): 可选的交易对方 QQ 号。若填写则查询双人对账单；若为空则查询当前用户的全局借贷大盘。
        """
        sender_id, sender_name, _ = self._get_sender_info(event)
        if target_qq:
            target_name = self.db.get_user_name(target_qq, target_qq)
            summary = self.ledger_service.calculate_pair_debt(sender_id, target_qq, sender_name, target_name)
            return TextFormatter.format_pair_debt_summary(summary, self.currency_symbol)
        else:
            overview = self.ledger_service.get_user_overview(sender_id, sender_name)
            return TextFormatter.format_user_overview(overview, self.currency_symbol)
