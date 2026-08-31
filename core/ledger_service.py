from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from .database import DatabaseManager


@dataclass
class PairDebtSummary:
    """双人债务汇总模型"""
    user_a_id: str
    user_a_name: str
    user_b_id: str
    user_b_name: str
    # 净债务值：>0 表示 B 欠 A; <0 表示 A 欠 B; ==0 表示两清
    net_balance: float
    # 谁欠谁详细统计
    a_lent_to_b: float       # A借给B总额
    b_repaid_to_a: float     # B还给A总额
    b_lent_to_a: float       # B借给A总额
    a_repaid_to_b: float     # A还给B总额
    total_tx_count: int      # 累计真实成交笔数
    recent_records: List[Dict[str, Any]]  # 最近交易流水


@dataclass
class CounterpartDebt:
    """个人大盘中的单个交易对手信息"""
    counterpart_id: str
    counterpart_name: str
    net_balance: float  # >0 表示对方欠我; <0 表示我欠对方


@dataclass
class UserGlobalOverview:
    """用户跨群全局大盘模型"""
    user_id: str
    user_name: str
    total_receivable: float  # 他人欠我总计
    total_payable: float     # 我欠他人总计
    debt_list: List[CounterpartDebt]  # 各对手明细


class LedgerService:
    """
    记账与债务计算服务
    负责精准的借贷双向冲抵（Netting）、跨群汇总及账单生成。
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    def calculate_pair_debt(
        self,
        user_a_id: str,
        user_b_id: str,
        user_a_name: str = "",
        user_b_name: str = ""
    ) -> PairDebtSummary:
        """
        精准计算两人之间的跨群双向借贷关系与实时净欠款
        """
        user_a_id = str(user_a_id)
        user_b_id = str(user_b_id)
        user_a_name = self.db.get_user_name(user_a_id, user_a_name or user_a_id)
        user_b_name = self.db.get_user_name(user_b_id, user_b_name or user_b_id)

        all_records = self.db.get_pair_records(user_a_id, user_b_id, limit=10000)

        a_lent_to_b = 0.0
        b_repaid_to_a = 0.0
        b_lent_to_a = 0.0
        a_repaid_to_b = 0.0

        for r in all_records:
            amt = float(r["amount"])
            rtype = r["record_type"]
            lender = str(r["lender_id"])
            borrower = str(r["borrower_id"])

            if rtype == "BORROW":
                if lender == user_a_id and borrower == user_b_id:
                    a_lent_to_b += amt
                elif lender == user_b_id and borrower == user_a_id:
                    b_lent_to_a += amt
            elif rtype == "REPAY":
                if lender == user_a_id and borrower == user_b_id:
                    # B还给A（lender是收款人A，borrower是还款人B）
                    b_repaid_to_a += amt
                elif lender == user_b_id and borrower == user_a_id:
                    # A还给B（lender是收款人B，borrower是还款人A）
                    a_repaid_to_b += amt

        # 净债务公式：(A借给B - B还A) - (B借给A - A还B)
        # >0: B 欠 A 钱; <0: A 欠 B 钱
        net_balance = round((a_lent_to_b - b_repaid_to_a) - (b_lent_to_a - a_repaid_to_b), 2)
        recent_records = self.db.get_pair_records_desc(user_a_id, user_b_id, limit=8)

        return PairDebtSummary(
            user_a_id=user_a_id,
            user_a_name=user_a_name,
            user_b_id=user_b_id,
            user_b_name=user_b_name,
            net_balance=net_balance,
            a_lent_to_b=round(a_lent_to_b, 2),
            b_repaid_to_a=round(b_repaid_to_a, 2),
            b_lent_to_a=round(b_lent_to_a, 2),
            a_repaid_to_b=round(a_repaid_to_b, 2),
            total_tx_count=len(all_records),
            recent_records=recent_records
        )

    def get_user_overview(
        self,
        user_id: str,
        user_name: str = ""
    ) -> UserGlobalOverview:
        """
        获取用户的跨群借贷全局大盘统计
        """
        user_id = str(user_id)
        user_name = self.db.get_user_name(user_id, user_name or user_id)
        all_records = self.db.get_all_active_records_for_user(user_id)

        # 收集所有出现过的交易对手 ID
        counterpart_ids = set()
        for r in all_records:
            lid = str(r["lender_id"])
            bid = str(r["borrower_id"])
            if lid == user_id and bid != user_id:
                counterpart_ids.add(bid)
            elif bid == user_id and lid != user_id:
                counterpart_ids.add(lid)

        total_receivable = 0.0
        total_payable = 0.0
        debt_list: List[CounterpartDebt] = []

        for cid in counterpart_ids:
            summary = self.calculate_pair_debt(user_id, cid)
            cname = self.db.get_user_name(cid, cid)
            # summary.net_balance > 0: 对手 cid 欠 user_id
            # summary.net_balance < 0: user_id 欠 对手 cid
            net = summary.net_balance
            if abs(net) >= 0.001:
                debt_list.append(CounterpartDebt(
                    counterpart_id=cid,
                    counterpart_name=cname,
                    net_balance=net
                ))
                if net > 0:
                    total_receivable += net
                else:
                    total_payable += abs(net)

        # 按金额绝对值由大到小排序
        debt_list.sort(key=lambda x: abs(x.net_balance), reverse=True)

        return UserGlobalOverview(
            user_id=user_id,
            user_name=user_name,
            total_receivable=round(total_receivable, 2),
            total_payable=round(total_payable, 2),
            debt_list=debt_list
        )

    def record_confirmed_transaction(
        self,
        lender_id: str,
        lender_name: str,
        borrower_id: str,
        borrower_name: str,
        amount: float,
        record_type: str,
        note: str,
        origin_group_id: str,
        created_at: str,
        confirmed_at: str
    ) -> int:
        """
        将双方确认的申请正式写入流水账本
        """
        # 更新用户名称缓存
        self.db.update_user_name(lender_id, lender_name)
        self.db.update_user_name(borrower_id, borrower_name)

        return self.db.insert_record(
            lender_id=lender_id,
            lender_name=lender_name,
            borrower_id=borrower_id,
            borrower_name=borrower_name,
            amount=round(float(amount), 2),
            record_type=record_type,
            note=note,
            origin_group_id=origin_group_id,
            created_at=created_at,
            confirmed_at=confirmed_at
        )

    def get_pair_history_paged(
        self,
        user_a_id: str,
        user_b_id: str,
        page: int = 1,
        page_size: int = 10
    ) -> Tuple[List[Dict[str, Any]], int, PairDebtSummary]:
        """
        分页获取两人之间的完整流水账（含总页数和双人汇总）
        """
        user_a_id = str(user_a_id)
        user_b_id = str(user_b_id)
        summary = self.calculate_pair_debt(user_a_id, user_b_id)
        all_records = self.db.get_pair_records(user_a_id, user_b_id, limit=10000)
        total_count = len(all_records)

        # 按确认时间倒序排序
        all_records.sort(key=lambda x: (x["confirmed_at"], x["id"]), reverse=True)

        offset = max(0, (page - 1) * page_size)
        paged_records = all_records[offset: offset + page_size]
        total_pages = max(1, (total_count + page_size - 1) // page_size)

        return paged_records, total_pages, summary
