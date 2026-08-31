from __future__ import annotations

from typing import Any, Dict, List, Optional
from .ledger_service import PairDebtSummary, UserGlobalOverview


class TextFormatter:
    """
    账单与消息文本排版器
    提供清晰美观、精确到秒的账本日志和汇总格式化输出。
    """

    @staticmethod
    def format_request_created(req: Dict[str, Any], currency: str = "¥") -> str:
        """格式化待确认申请卡片"""
        amt_str = f"{currency}{float(req['amount']):.2f}"
        rtype = req["record_type"]
        p_name = req["proposer_name"]
        t_name = req["target_name"]
        p_id = req["proposer_id"]
        t_id = req["target_id"]
        l_id = req["lender_id"]
        b_id = req["borrower_id"]
        note = req["note"] or "无"

        if rtype == "BORROW":
            if l_id == p_id:
                action_desc = f"{p_name} 借出给 {t_name} (记录为 {t_name} 欠款)"
            else:
                action_desc = f"{p_name} 向 {t_name} 借入 (记录为 {p_name} 欠款)"
        else:  # REPAY
            if b_id == p_id:
                action_desc = f"{p_name} 还款给 {t_name} (抵扣欠款)"
            else:
                action_desc = f"{p_name} 确认收到 {t_name} 的还款 (抵扣欠款)"

        lines = [
            "📋【借贷/还款申请已发起】",
            "━━━━━━━━━━━━━━",
            f"🔹 申请单号：#{req['req_code']}",
            f"🔹 申请内容：{action_desc}",
            f"🔹 交易金额：{amt_str}",
            f"🔹 事由备注：{note}",
            f"🔹 发起时间：{req['created_at']}",
            f"🔹 超时截止：{req['expire_at']}",
            "━━━━━━━━━━━━━━",
            f"⚠️ 请被申请人 @{t_name}(QQ:{t_id}) 及时确认：",
            f"👉 同意回复：/同意 #{req['req_code']} 或 直接回复「同意」",
            f"👉 拒绝回复：/拒绝 #{req['req_code']} 或 直接回复「拒绝」",
            f"💡 发起人可在生效前发送 /撤销 #{req['req_code']} 取消申请"
        ]
        return "\n".join(lines)

    @staticmethod
    def format_request_accepted(req: Dict[str, Any], summary: Optional[PairDebtSummary], currency: str = "¥") -> str:
        """格式化确认成功入账卡片"""
        amt_str = f"{currency}{float(req['amount']):.2f}"
        rtype = req["record_type"]
        p_name = req["proposer_name"]
        t_name = req["target_name"]
        note = req["note"] or "无"

        lines = [
            "✅【借贷申请已确认入账】",
            "━━━━━━━━━━━━━━",
            f"🔹 申请单号：#{req['req_code']}",
            f"🔹 交易类型：{'借款记录' if rtype == 'BORROW' else '还款抵扣'}",
            f"🔹 入账金额：{amt_str}",
            f"🔹 事由备注：{note}",
            f"🔹 生效时间：{req.get('confirmed_at', req['created_at'])}",
            "━━━━━━━━━━━━━━"
        ]

        if summary:
            lines.append("💰【双方最新实时对账结余】：")
            net = summary.net_balance
            a_name = summary.user_a_name
            b_name = summary.user_b_name

            if abs(net) < 0.001:
                lines.append("✨ 双方账目已全部两清，互不相欠！")
            elif net > 0:
                lines.append(f"👉 当前 {b_name} 净欠 {a_name}：{currency}{net:.2f}")
            else:
                lines.append(f"👉 当前 {a_name} 净欠 {b_name}：{currency}{abs(net):.2f}")

        return "\n".join(lines)

    @staticmethod
    def format_pair_debt_summary(summary: PairDebtSummary, currency: str = "¥") -> str:
        """格式化双人实时对账汇总"""
        a_name = summary.user_a_name
        b_name = summary.user_b_name
        a_id = summary.user_a_id
        b_id = summary.user_b_id
        net = summary.net_balance

        lines = [
            "📊【双人借贷实时对账单】",
            "━━━━━━━━━━━━━━",
            f"👥 对账双方：{a_name}({a_id}) ⇄ {b_name}({b_id})",
            "━━━━━━━━━━━━━━"
        ]

        if abs(net) < 0.001:
            lines.append("💰 实时结余：✨ 双方已完全两清，互无欠款！")
        elif net > 0:
            lines.append(f"💰 实时结余：🔴 {b_name} 净欠 {a_name}：{currency}{net:.2f}")
        else:
            lines.append(f"💰 实时结余：🔴 {a_name} 净欠 {b_name}：{currency}{abs(net):.2f}")

        lines.extend([
            "━━━━━━━━━━━━━━",
            "📈 累计往来流水统计：",
            f"• {a_name} 累计借出给 {b_name}：{currency}{summary.a_lent_to_b:.2f}",
            f"• {b_name} 累计还款给 {a_name}：{currency}{summary.b_repaid_to_a:.2f}",
            f"• {b_name} 累计借出给 {a_name}：{currency}{summary.b_lent_to_a:.2f}",
            f"• {a_name} 累计还款给 {b_name}：{currency}{summary.a_repaid_to_b:.2f}",
            f"• 累计成交笔数：{summary.total_tx_count} 笔"
        ])

        if summary.recent_records:
            lines.extend([
                "━━━━━━━━━━━━━━",
                "🕒 最近交易明细（精确到秒）："
            ])
            for r in summary.recent_records:
                r_type = "借出" if r["record_type"] == "BORROW" else "还款"
                r_amt = f"{currency}{float(r['amount']):.2f}"
                r_time = r["confirmed_at"]
                r_note = f" (事由: {r['note']})" if r["note"] else ""
                lines.append(f"• [{r_time}] {r['lender_name']} ➔ {r['borrower_name']} {r_type} {r_amt}{r_note}")

        lines.append("━━━━━━━━━━━━━━")
        lines.append("💡 发送「/对账 @某人」可分页查看完整历史流水明细")
        return "\n".join(lines)

    @staticmethod
    def format_user_overview(overview: UserGlobalOverview, currency: str = "¥") -> str:
        """格式化个人全局借贷大盘总览"""
        lines = [
            f"🌐【{overview.user_name} 的全局借贷总览】",
            "━━━━━━━━━━━━━━",
            f"👤 用户账号：QQ {overview.user_id}",
            f"🟢 他人欠我（待收总计）：{currency}{overview.total_receivable:.2f}",
            f"🔴 我欠他人（待还总计）：{currency}{overview.total_payable:.2f}",
            "━━━━━━━━━━━━━━"
        ]

        if not overview.debt_list:
            lines.append("✨ 暂无任何未结清债务，全身无债一身轻！")
        else:
            lines.append("🧾 交易对手明细列表：")
            for idx, item in enumerate(overview.debt_list, 1):
                cname = item.counterpart_name
                cid = item.counterpart_id
                amt = item.net_balance
                if amt > 0:
                    lines.append(f"  {idx}. {cname}({cid})：欠我 {currency}{amt:.2f}")
                else:
                    lines.append(f"  {idx}. {cname}({cid})：我欠 {currency}{abs(amt):.2f}")

        lines.append("━━━━━━━━━━━━━━")
        lines.append("💡 发送「/查账 @某人」可查看与指定好友的双人对账单")
        return "\n".join(lines)

    @staticmethod
    def format_pair_history_paged(
        records: List[Dict[str, Any]],
        page: int,
        total_pages: int,
        summary: PairDebtSummary,
        currency: str = "¥"
    ) -> str:
        """格式化分页对账单明细"""
        a_name = summary.user_a_name
        b_name = summary.user_b_name
        lines = [
            f"📜【{a_name} 与 {b_name} 的历史对账单】",
            f"📄 页码：第 {page}/{total_pages} 页",
            "━━━━━━━━━━━━━━"
        ]

        if not records:
            lines.append("本页暂无交易流水记录。")
        else:
            for r in records:
                r_type = "借出" if r["record_type"] == "BORROW" else "还款"
                r_amt = f"{currency}{float(r['amount']):.2f}"
                r_time = r["confirmed_at"]
                r_note = f"\n  事由: {r['note']}" if r["note"] else ""
                lines.append(f"🔹 单号 #{r['id']} [{r_time}]\n  {r['lender_name']} ➔ {r['borrower_name']} {r_type} {r_amt}{r_note}")

        lines.extend([
            "━━━━━━━━━━━━━━",
            f"💡 发送「/对账 @某人 {page+1}」查看下一页"
        ])
        return "\n".join(lines)

    @staticmethod
    def format_pending_list(
        waiting_me: List[Dict[str, Any]],
        my_proposed: List[Dict[str, Any]],
        currency: str = "¥"
    ) -> str:
        """格式化待办申请列表"""
        lines = [
            "📋【我的借贷待办申请】",
            "━━━━━━━━━━━━━━"
        ]

        lines.append(f"⏳ 等待我确认的申请 ({len(waiting_me)} 笔)：")
        if not waiting_me:
            lines.append("  暂无等待您确认的申请。")
        else:
            for req in waiting_me:
                amt = f"{currency}{float(req['amount']):.2f}"
                lines.append(
                    f"  • 单号 #{req['req_code']} | {req['proposer_name']} 申请 {amt} ({req['note'] or '无备注'})\n"
                    f"    回复「/同意 #{req['req_code']}」或「/拒绝 #{req['req_code']}」"
                )

        lines.append("━━━━━━━━━━━━━━")
        lines.append(f"📤 我发起等待对方确认的申请 ({len(my_proposed)} 笔)：")
        if not my_proposed:
            lines.append("  暂无进行中的发起申请。")
        else:
            for req in my_proposed:
                amt = f"{currency}{float(req['amount']):.2f}"
                lines.append(
                    f"  • 单号 #{req['req_code']} | 向 {req['target_name']} 申请 {amt} (截止: {req['expire_at']})\n"
                    f"    如需取消可发送「/撤销 #{req['req_code']}」"
                )

        return "\n".join(lines)

    @staticmethod
    def format_help() -> str:
        """格式化帮助说明"""
        lines = [
            "📖【聊天记账与双人债务管理助手指南】",
            "━━━━━━━━━━━━━━",
            "🤖【自然语言 @机器人 使用（最方便）】：",
            "• 借钱给别人：@Bot 我借给 @张三 50元 买奶茶",
            "• 垫付/记欠款：@Bot @李四 欠我 100 块打车费",
            "• 向别人借钱：@Bot 我向 @王五 借了 30 块吃午饭",
            "• 记录已还款：@Bot 我还给 @张三 50 元 微信已转",
            "• 查双人账目：@Bot 查一下我和 @张三 的账",
            "• 查个人总账：@Bot 我的账单 / 谁欠我钱",
            "• 同意或拒绝：@Bot 同意 / @Bot 拒绝",
            "━━━━━━━━━━━━━━",
            "⚡【快捷指令列表】：",
            "• /借出 @某人 [金额] [事由] : 记录借给对方钱",
            "• /借入 @某人 [金额] [事由] : 记录向对方借钱",
            "• /还款 @某人 [金额] [事由] : 记录还款给对方",
            "• /同意 [单号] : 同意借还款申请真实入账",
            "• /拒绝 [单号] : 拒绝借还款申请",
            "• /撤销 [单号] : 发起人撤销未确认申请",
            "• /查账 @某人 : 查询双人净欠款与最近流水",
            "• /我的账单 : 跨群统计个人全局借出/借入大盘",
            "• /对账 @某人 [页码] : 分页查看完整对账单",
            "• /待办 : 查看当前等待我确认或我发起的申请",
            "━━━━━━━━━━━━━━",
            "💡 提示：所有记录均绑定 QQ 号，支持跨不同群聊统一对账！"
        ]
        return "\n".join(lines)
