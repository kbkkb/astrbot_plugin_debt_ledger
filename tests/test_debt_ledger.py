from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from core.database import DatabaseManager
from core.ledger_service import LedgerService
from core.nl_parser import NaturalLanguageParser
from core.request_manager import RequestManager
from core.text_formatter import TextFormatter


class TestDebtLedgerPlugin(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ledger.db")
        self.db = DatabaseManager(self.db_path)
        self.ledger_service = LedgerService(self.db)
        self.request_manager = RequestManager(self.db, self.ledger_service, default_timeout=600)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_database_and_user_cache(self):
        self.db.update_user_name("10001", "张三")
        self.db.update_user_name("10002", "李四")
        self.assertEqual(self.db.get_user_name("10001"), "张三")
        self.assertEqual(self.db.get_user_name("10002"), "李四")

    def test_request_lifecycle_and_acceptance(self):
        # 1. 发起出借申请：张三(10001) 借给 李四(10002) 100元
        ok, req, msg = self.request_manager.create_request(
            proposer_id="10001",
            proposer_name="张三",
            target_id="10002",
            target_name="李四",
            lender_id="10001",
            borrower_id="10002",
            amount=100.0,
            record_type="BORROW",
            note="晚餐AA",
            origin_group_id="group_888"
        )
        self.assertTrue(ok)
        self.assertIsNotNone(req)
        req_code = req["req_code"]

        # 2. 非目标人（如第三人王五 10003）尝试同意 -> 失败
        ok, _, _, err_msg = self.request_manager.accept_request("10003", req_code=req_code)
        self.assertFalse(ok)
        self.assertIn("权限不足", err_msg)

        # 3. 目标人李四(10002) 同意 -> 成功入账
        ok, req_acc, summary, acc_msg = self.request_manager.accept_request("10002", req_code=req_code)
        self.assertTrue(ok)
        self.assertIsNotNone(summary)
        # 李四净欠张三 100.00 元
        self.assertEqual(summary.net_balance, 100.00)
        self.assertEqual(summary.a_lent_to_b, 100.00)

        # 4. 再次尝试同意 -> 已被处理，失败
        ok, _, _, err2 = self.request_manager.accept_request("10002", req_code=req_code)
        self.assertFalse(ok)

    def test_netting_and_repayment_calculation(self):
        """测试多笔借贷与还款的精准双向对冲计算"""
        now_str = "2026-08-31 10:00:00"
        # 张三 借出给 李四 100
        self.ledger_service.record_confirmed_transaction("10001", "张三", "10002", "李四", 100.0, "BORROW", "借款1", "g1", now_str, now_str)
        # 张三 又借出给 李四 50
        self.ledger_service.record_confirmed_transaction("10001", "张三", "10002", "李四", 50.0, "BORROW", "借款2", "g2", now_str, now_str)
        # 李四 还给 张三 30
        self.ledger_service.record_confirmed_transaction("10001", "张三", "10002", "李四", 30.0, "REPAY", "还款1", "g1", now_str, now_str)
        # 李四 借给 张三 20 (反向借款)
        self.ledger_service.record_confirmed_transaction("10002", "李四", "10001", "张三", 20.0, "BORROW", "反向借款", "g3", now_str, now_str)

        # 计算双人净债务：
        # 张三->李四净借出: 100 + 50 - 30 = 120
        # 李四->张三净借出: 20 - 0 = 20
        # 总净债务: 120 - 20 = 100 (李四欠张三 100.00)
        summary = self.ledger_service.calculate_pair_debt("10001", "10002")
        self.assertEqual(summary.net_balance, 100.00)
        self.assertEqual(summary.total_tx_count, 4)

        # 全局大盘测试
        overview_zhangsan = self.ledger_service.get_user_overview("10001")
        self.assertEqual(overview_zhangsan.total_receivable, 100.00)
        self.assertEqual(overview_zhangsan.total_payable, 0.00)
        self.assertEqual(len(overview_zhangsan.debt_list), 1)

        overview_lisi = self.ledger_service.get_user_overview("10002")
        self.assertEqual(overview_lisi.total_receivable, 0.00)
        self.assertEqual(overview_lisi.total_payable, 100.00)

    def test_rejection_and_revocation(self):
        # 测试拒绝
        ok, req1, _ = self.request_manager.create_request("10001", "张三", "10002", "李四", "10001", "10002", 50.0, "BORROW")
        self.assertTrue(ok)
        ok_rej, _, _ = self.request_manager.reject_request("10002", req_code=req1["req_code"])
        self.assertTrue(ok_rej)

        # 检查未生成流水
        summary = self.ledger_service.calculate_pair_debt("10001", "10002")
        self.assertEqual(summary.total_tx_count, 0)

        # 测试撤销
        ok, req2, _ = self.request_manager.create_request("10001", "张三", "10002", "李四", "10001", "10002", 60.0, "BORROW")
        self.assertTrue(ok)
        ok_rev, _, _ = self.request_manager.revoke_request("10001", req_code=req2["req_code"])
        self.assertTrue(ok_rev)

        summary = self.ledger_service.calculate_pair_debt("10001", "10002")
        self.assertEqual(summary.total_tx_count, 0)

    def test_natural_language_parser(self):
        # 1. 借出解析
        p1 = NaturalLanguageParser.parse_message("我借给 @张三 100元 吃火锅", mentioned_qq_list=["10002"])
        self.assertEqual(p1.intent_type, "LEND")
        self.assertEqual(p1.target_qq, "10002")
        self.assertEqual(p1.amount, 100.0)
        self.assertIn("吃火锅", p1.note)

        # 2. 欠我/垫付解析
        p2 = NaturalLanguageParser.parse_message("@李四 欠我 50.5 块打车费", mentioned_qq_list=["10002"])
        self.assertEqual(p2.intent_type, "LEND")
        self.assertEqual(p2.target_qq, "10002")
        self.assertEqual(p2.amount, 50.5)
        self.assertIn("打车", p2.note)

        # 3. 借入解析
        p3 = NaturalLanguageParser.parse_message("我向 @王五 借了 30 元 买奶茶", mentioned_qq_list=["10003"])
        self.assertEqual(p3.intent_type, "BORROW")
        self.assertEqual(p3.target_qq, "10003")
        self.assertEqual(p3.amount, 30.0)

        # 4. 还款解析
        p4 = NaturalLanguageParser.parse_message("我还给 @张三 50 块 微信已转", mentioned_qq_list=["10001"])
        self.assertEqual(p4.intent_type, "REPAY")
        self.assertEqual(p4.target_qq, "10001")
        self.assertEqual(p4.amount, 50.0)

        # 5. 查双人账解析
        p5 = NaturalLanguageParser.parse_message("查一下我和 @张三 的账", mentioned_qq_list=["10001"])
        self.assertEqual(p5.intent_type, "QUERY_PAIR")
        self.assertEqual(p5.target_qq, "10001")

        # 6. 查总账解析
        p6 = NaturalLanguageParser.parse_message("我的账单")
        self.assertEqual(p6.intent_type, "QUERY_SELF")

        # 7. 同意带单号
        p7 = NaturalLanguageParser.parse_message("同意 #101")
        self.assertEqual(p7.intent_type, "ACCEPT")
        self.assertEqual(p7.req_code, "101")

        # 8. 拒绝带单号
        p8 = NaturalLanguageParser.parse_message("拒绝 102 算错了")
        self.assertEqual(p8.intent_type, "REJECT")
        self.assertEqual(p8.req_code, "102")

        # 9. 帮助
        p9 = NaturalLanguageParser.parse_message("记账帮助")
        self.assertEqual(p9.intent_type, "HELP")

        # 10. @Bot @目标 欠我33 / 带 QQ 号与 At 标签场景
        p10 = NaturalLanguageParser.parse_message(
            "[At:1457589185] [At:1606732762] 欠我33",
            mentioned_qq_list=["1457589185", "1606732762"],
            sender_id="905746960",
            bot_id="1457589185"
        )
        self.assertEqual(p10.intent_type, "LEND")
        self.assertEqual(p10.target_qq, "1606732762")
        self.assertEqual(p10.amount, 33.0)
        self.assertEqual(p10.note, "")

        # 11. @昵称(QQ) 欠我33
        p11 = NaturalLanguageParser.parse_message(
            "@你的心是氢气做的吗(1606732762) 欠我33",
            sender_id="905746960",
            bot_id="1457589185"
        )
        self.assertEqual(p11.intent_type, "LEND")
        self.assertEqual(p11.target_qq, "1606732762")
        self.assertEqual(p11.amount, 33.0)

        # 12. 收到还款 / @张三 还了我 33 元
        p12 = NaturalLanguageParser.parse_message(
            "@张三 还了我 33 元",
            mentioned_qq_list=["10001"],
            sender_id="10002"
        )
        self.assertEqual(p12.intent_type, "RECEIVE_REPAY")
        self.assertEqual(p12.target_qq, "10001")
        self.assertEqual(p12.amount, 33.0)

        # 13. 一键还清 / 结清 @张三
        p13 = NaturalLanguageParser.parse_message(
            "我还清了 @张三 微信已转",
            mentioned_qq_list=["10001"],
            sender_id="10002"
        )
        self.assertEqual(p13.intent_type, "SETTLE")
        self.assertEqual(p13.target_qq, "10001")

    def test_text_formatter(self):
        req = {
            "req_code": "101",
            "proposer_id": "10001",
            "proposer_name": "张三",
            "target_id": "10002",
            "target_name": "李四",
            "lender_id": "10001",
            "borrower_id": "10002",
            "amount": 100.0,
            "record_type": "BORROW",
            "note": "晚餐AA",
            "created_at": "2026-08-31 10:00:00",
            "expire_at": "2026-08-31 10:10:00"
        }
        text = TextFormatter.format_request_created(req)
        self.assertIn("#101", text)
        self.assertIn("100.00", text)
        self.assertIn("晚餐AA", text)

        summary = self.ledger_service.calculate_pair_debt("10001", "10002")
        acc_text = TextFormatter.format_request_accepted(req, summary)
        self.assertIn("借贷申请已确认入账", acc_text)



    def test_cross_group_aggregation(self):
        """测试跨不同群号发生的借贷在全局总账与双人账中的精准合并"""
        now_str = "2026-08-31 10:00:00"
        # 在群 A 中：张三 借给 李四 200元
        self.ledger_service.record_confirmed_transaction("10001", "张三", "10002", "李四", 200.0, "BORROW", "群A借款", "group_A", now_str, now_str)
        # 在群 B 中：李四 还给 张三 50元
        self.ledger_service.record_confirmed_transaction("10001", "张三", "10002", "李四", 50.0, "REPAY", "群B还款", "group_B", now_str, now_str)
        # 在群 C 中：张三 借给 王五 100元
        self.ledger_service.record_confirmed_transaction("10001", "张三", "10003", "王五", 100.0, "BORROW", "群C借款", "group_C", now_str, now_str)

        # 验证跨群双人账：李四欠张三 150.00
        summary_pair = self.ledger_service.calculate_pair_debt("10001", "10002")
        self.assertEqual(summary_pair.net_balance, 150.00)

        # 验证张三跨群全局总账：待收 150(李四) + 100(王五) = 250.00
        overview = self.ledger_service.get_user_overview("10001")
        self.assertEqual(overview.total_receivable, 250.00)
        self.assertEqual(overview.total_payable, 0.00)
        self.assertEqual(len(overview.debt_list), 2)

    def test_timeout_expiration(self):
        """测试过期超时的自动失效处理"""
        # 创建一个超时时间为 -1 秒（立即过期）的申请
        ok, req, _ = self.request_manager.create_request(
            proposer_id="10001",
            proposer_name="张三",
            target_id="10002",
            target_name="李四",
            lender_id="10001",
            borrower_id="10002",
            amount=50.0,
            record_type="BORROW",
            timeout_seconds=-1
        )
        self.assertTrue(ok)
        req_code = req["req_code"]

        # 尝试同意已过期申请 -> 失败
        ok_acc, _, _, err_msg = self.request_manager.accept_request("10002", req_code=req_code)
        self.assertFalse(ok_acc)
        self.assertIn("超时失效", err_msg)


if __name__ == "__main__":
    unittest.main()
