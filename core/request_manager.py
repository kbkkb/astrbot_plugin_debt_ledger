from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .database import DatabaseManager
from .ledger_service import LedgerService, PairDebtSummary


class RequestManager:
    """
    待确认借贷申请状态机管理器
    负责处理双人申请的发起、单号分配、目标用户校验、双方确认/拒绝/撤销以及超时失效。
    """

    def __init__(self, db: DatabaseManager, ledger_service: LedgerService, default_timeout: int = 600):
        self.db = db
        self.ledger_service = ledger_service
        self.default_timeout = default_timeout

    def _generate_req_code(self) -> str:
        """生成简洁易输入的 3-4 位纯数字短单号（如 101, 102）"""
        # 查询当前数据库中最大的单号数值或直接基于自增/随机
        for _ in range(50):
            code_num = random.randint(100, 999)
            code_str = str(code_num)
            existing = self.db.get_pending_request(code_str)
            if not existing:
                return code_str
        # 溢出降级为更大随机数
        return str(random.randint(1000, 9999))

    def create_request(
        self,
        proposer_id: str,
        proposer_name: str,
        target_id: str,
        target_name: str,
        lender_id: str,
        borrower_id: str,
        amount: float,
        record_type: str,  # 'BORROW' 或 'REPAY'
        note: str = "",
        origin_group_id: str = "",
        timeout_seconds: Optional[int] = None
    ) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        发起借款/欠款/还款申请
        """
        # 基础校验
        proposer_id = str(proposer_id).strip()
        target_id = str(target_id).strip()

        if not proposer_id or not target_id:
            return False, None, "发起人和目标用户的 QQ 号不能为空。"

        if proposer_id == target_id:
            return False, None, "不能对自己发起借款或还款申请。"

        if amount <= 0.001:
            return False, None, "金额必须大于 0 元。"

        amount = round(float(amount), 2)
        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout

        now = datetime.now()
        created_at = now.strftime("%Y-%m-%d %H:%M:%S")
        expire_at = (now + timedelta(seconds=timeout)).strftime("%Y-%m-%d %H:%M:%S")
        req_code = self._generate_req_code()

        # 更新用户名称缓存
        self.db.update_user_name(proposer_id, proposer_name)
        self.db.update_user_name(target_id, target_name)

        # 写入数据库
        self.db.insert_pending_request(
            req_code=req_code,
            proposer_id=proposer_id,
            proposer_name=proposer_name,
            target_id=target_id,
            target_name=target_name,
            lender_id=str(lender_id),
            borrower_id=str(borrower_id),
            amount=amount,
            record_type=record_type,
            note=note,
            origin_group_id=origin_group_id,
            created_at=created_at,
            expire_at=expire_at
        )

        req_data = self.db.get_pending_request(req_code)
        return True, req_data, "申请发起成功"

    def accept_request(
        self,
        operator_id: str,
        req_code: Optional[str] = None,
        group_id: str = ""
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[PairDebtSummary], str]:
        """
        被申请人确认并同意申请，使账目正式生效
        """
        operator_id = str(operator_id).strip()
        self.db.clean_expired_requests()

        req: Optional[Dict[str, Any]] = None
        if req_code:
            clean_code = str(req_code).replace("#", "").strip()
            req = self.db.get_request_by_code(clean_code)
            if not req:
                return False, None, None, f"未找到单号为 #{clean_code} 的借贷申请。"
            if req["status"] == "EXPIRED":
                return False, req, None, f"申请 #{clean_code} 已经超时失效。"
            if req["status"] == "ACCEPTED":
                return False, req, None, f"申请 #{clean_code} 此前已被确认生效。"
            if req["status"] == "REJECTED":
                return False, req, None, f"申请 #{clean_code} 此前已被拒绝作废。"
            if req["status"] == "REVOKED":
                return False, req, None, f"申请 #{clean_code} 此前已被发起人撤销。"
            if req["status"] != "PENDING":
                return False, req, None, f"申请 #{clean_code} 当前状态不可操作（{req['status']}）。"
        else:
            req = self.db.get_latest_pending_for_target(operator_id, group_id)
            if not req:
                return False, None, None, "当前没有等待您确认的借款/还款申请。"

        # 权限校验：必须是被申请的目标人
        if str(req["target_id"]) != operator_id:
            return False, req, None, f"权限不足：该申请（#{req['req_code']}）需要由指定的接收人 (QQ: {req['target_id']}) 进行确认。"

        # 检查是否过期
        now = datetime.now()
        expire_time = datetime.strptime(req["expire_at"], "%Y-%m-%d %H:%M:%S")
        if now > expire_time:
            self.db.update_pending_status(req["req_code"], "EXPIRED")
            return False, req, None, f"申请 #{req['req_code']} 已经超时失效。"

        # 更新状态为 ACCEPTED
        success = self.db.update_pending_status(req["req_code"], "ACCEPTED")
        if not success:
            return False, req, None, "处理失败，该申请可能已被其他操作处理。"

        # 真实写入流水账本
        confirmed_at = now.strftime("%Y-%m-%d %H:%M:%S")
        self.ledger_service.record_confirmed_transaction(
            lender_id=req["lender_id"],
            lender_name=req["proposer_name"] if req["lender_id"] == req["proposer_id"] else req["target_name"],
            borrower_id=req["borrower_id"],
            borrower_name=req["proposer_name"] if req["borrower_id"] == req["proposer_id"] else req["target_name"],
            amount=float(req["amount"]),
            record_type=req["record_type"],
            note=req["note"],
            origin_group_id=req["origin_group_id"],
            created_at=req["created_at"],
            confirmed_at=confirmed_at
        )

        # 计算最新的双人欠款汇总
        new_summary = self.ledger_service.calculate_pair_debt(req["proposer_id"], req["target_id"])
        return True, req, new_summary, "确认成功，账目已正式生效！"

    def reject_request(
        self,
        operator_id: str,
        req_code: Optional[str] = None,
        group_id: str = "",
        reason: str = ""
    ) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        被申请人拒绝申请
        """
        operator_id = str(operator_id).strip()
        self.db.clean_expired_requests()

        req: Optional[Dict[str, Any]] = None
        if req_code:
            clean_code = str(req_code).replace("#", "").strip()
            req = self.db.get_request_by_code(clean_code)
            if not req:
                return False, None, f"未找到单号为 #{clean_code} 的借贷申请。"
            if req["status"] == "EXPIRED":
                return False, req, f"申请 #{clean_code} 已经超时失效。"
            if req["status"] == "ACCEPTED":
                return False, req, f"申请 #{clean_code} 此前已被确认生效。"
            if req["status"] == "REJECTED":
                return False, req, f"申请 #{clean_code} 此前已被拒绝作废。"
            if req["status"] == "REVOKED":
                return False, req, f"申请 #{clean_code} 此前已被发起人撤销。"
            if req["status"] != "PENDING":
                return False, req, f"申请 #{clean_code} 当前状态不可操作（{req['status']}）。"
        else:
            req = self.db.get_latest_pending_for_target(operator_id, group_id)
            if not req:
                return False, None, "当前没有等待您确认的申请。"

        if str(req["target_id"]) != operator_id:
            return False, req, f"权限不足：只有指定的被申请人才能拒绝申请 #{req['req_code']}。"

        self.db.update_pending_status(req["req_code"], "REJECTED")
        return True, req, f"已成功拒绝申请 #{req['req_code']}。"

    def revoke_request(
        self,
        operator_id: str,
        req_code: Optional[str] = None,
        group_id: str = ""
    ) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        发起人在对方确认前撤销申请
        """
        operator_id = str(operator_id).strip()
        self.db.clean_expired_requests()

        req: Optional[Dict[str, Any]] = None
        if req_code:
            clean_code = str(req_code).replace("#", "").strip()
            req = self.db.get_request_by_code(clean_code)
            if not req:
                return False, None, f"未找到单号为 #{clean_code} 的借贷申请。"
            if req["status"] == "EXPIRED":
                return False, req, f"申请 #{clean_code} 已经超时失效。"
            if req["status"] == "ACCEPTED":
                return False, req, f"申请 #{clean_code} 此前已被确认生效。"
            if req["status"] == "REJECTED":
                return False, req, f"申请 #{clean_code} 此前已被拒绝作废。"
            if req["status"] == "REVOKED":
                return False, req, f"申请 #{clean_code} 此前已被撤销。"
            if req["status"] != "PENDING":
                return False, req, f"申请 #{clean_code} 当前状态不可操作（{req['status']}）。"
        else:
            req = self.db.get_latest_pending_for_proposer(operator_id, group_id)
            if not req:
                return False, None, "当前没有您发起的待处理申请可撤销。"

        if str(req["proposer_id"]) != operator_id:
            return False, req, f"权限不足：只有申请的发起人才能撤销申请 #{req['req_code']}。"

        self.db.update_pending_status(req["req_code"], "REVOKED")
        return True, req, f"已成功撤回申请 #{req['req_code']}。"

    def get_user_pending_overview(self, user_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        获取与该用户相关的所有未决申请
        返回: (等待我确认的申请列表, 我发起的等待他人确认的申请列表)
        """
        user_id = str(user_id).strip()
        self.db.clean_expired_requests()
        all_pending = self.db.get_all_pending_for_user(user_id)

        waiting_me: List[Dict[str, Any]] = []
        my_proposed: List[Dict[str, Any]] = []

        for req in all_pending:
            if str(req["target_id"]) == user_id:
                waiting_me.append(req)
            elif str(req["proposer_id"]) == user_id:
                my_proposed.append(req)

        return waiting_me, my_proposed
