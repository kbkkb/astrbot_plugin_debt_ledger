from __future__ import annotations

import glob
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("astrbot_plugin_debt_ledger.memory_bridge")


class MemoryBridge:
    """
    跨插件记忆与外号桥接器
    自动发现并同步《天使之忆》(Angel Memory)、《天使之眼》(Angel Eye)、《天使之心》(Angel Heart)
    以及画像插件中记录的用户外号、群名片与实体称呼。
    """

    @classmethod
    def get_candidate_data_dirs(cls) -> List[Path]:
        """获取所有可能的插件数据存放根目录"""
        dirs = []
        candidates = [
            Path("data"),
            Path("plugin_data"),
            Path("data/plugin_data"),
            Path("../plugin_data"),
            Path("../../plugin_data"),
            Path(r"D:\BotData\astrbot_data\plugin_data"),
            Path(r"D:\AstrBot\data\plugins"),
        ]
        for c in candidates:
            try:
                if c.exists() and c.is_dir():
                    dirs.append(c.resolve())
            except Exception:
                pass
        return list(set(dirs))

    @classmethod
    def extract_from_angel_memory(cls) -> Dict[str, str]:
        """从天使之忆 (Angel Memory) 的 SQLite 记忆库中提取实体/昵称/外号与 QQ 的绑定"""
        alias_map: Dict[str, str] = {}
        for base in cls.get_candidate_data_dirs():
            db_pattern = str(base / "astrbot_plugin_angel_memory" / "**" / "simple_memory.db")
            for db_path in glob.glob(db_pattern, recursive=True):
                try:
                    conn = sqlite3.connect(db_path, timeout=5.0)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT judgment, reasoning FROM memory_records WHERE is_active = 1 OR strength >= 1"
                    ).fetchall()
                    for r in rows:
                        combined_text = f"{r['judgment']} {r['reasoning']}"
                        # 匹配 昵称（123456） 或 昵称(123456)
                        matches = re.findall(r"([\u4e00-\u9fa5a-zA-Z0-9_\-\[\]]+)[（\(](\d{5,12})[）\)]", combined_text)
                        for name, qq in matches:
                            clean_name = name.strip()
                            if 1 <= len(clean_name) <= 20 and not clean_name.isdigit():
                                alias_map[clean_name] = str(qq)
                                # 如果包含前缀连字符（如 伊波恩首席鉴定师-零），拆分出核心名字（零）
                                if "-" in clean_name:
                                    sub_name = clean_name.split("-")[-1].strip()
                                    if len(sub_name) >= 1:
                                        alias_map[sub_name] = str(qq)
                    conn.close()
                except Exception as e:
                    logger.debug(f"[MemoryBridge] 读取 Angel Memory 数据库出错: {e}")
        return alias_map

    @classmethod
    def extract_from_angel_eye(cls) -> Dict[str, str]:
        """从天使之眼 (Angel Eye) 聊天历史数据库中提取群名片与昵称映射"""
        alias_map: Dict[str, str] = {}
        for base in cls.get_candidate_data_dirs():
            db_pattern = str(base / "astrbot_plugin_angel_eye" / "**" / "qq_history_cache.db")
            for db_path in glob.glob(db_pattern, recursive=True):
                try:
                    conn = sqlite3.connect(db_path, timeout=5.0)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT DISTINCT user_id, nickname FROM messages WHERE nickname != '' AND user_id IS NOT NULL"
                    ).fetchall()
                    for r in rows:
                        nick = str(r["nickname"]).strip()
                        uid = str(r["user_id"]).strip()
                        if 1 <= len(nick) <= 20 and not nick.isdigit():
                            alias_map[nick] = uid
                    conn.close()
                except Exception as e:
                    logger.debug(f"[MemoryBridge] 读取 Angel Eye 数据库出错: {e}")
        return alias_map

    @classmethod
    def extract_from_lzpersona(cls) -> Dict[str, str]:
        """从画像插件 (LZPersona) 的 user_profiles.json 中提取昵称/外号"""
        alias_map: Dict[str, str] = {}
        for base in cls.get_candidate_data_dirs():
            json_path = base / "astrbot_plugin_lzpersona" / "user_profiles.json"
            if json_path.exists():
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            for uid, profile in data.items():
                                if isinstance(profile, dict):
                                    name = profile.get("nickname") or profile.get("name") or profile.get("alias")
                                    if name and isinstance(name, str) and not name.isdigit():
                                        alias_map[name.strip()] = str(uid)
                except Exception as e:
                    logger.debug(f"[MemoryBridge] 读取 LZPersona 画像数据出错: {e}")
        return alias_map

    @classmethod
    def get_all_external_aliases(cls) -> Dict[str, str]:
        """汇总所有外部插件记忆的外号映射（去重合并）"""
        combined: Dict[str, str] = {}
        combined.update(cls.extract_from_angel_eye())
        combined.update(cls.extract_from_lzpersona())
        combined.update(cls.extract_from_angel_memory())  # 天使之忆权重最高
        return combined
