#!/usr/bin/env python3
"""/api/chat v2 Golden Test。

用法（務必使用丟棄式測試帳號，這支腳本會真的呼叫 /api/chat）：
    export CHAT_API_BASE=https://weather-api-q8iy.onrender.com
    export TEST_USER_ID=<測試帳號 user_id>
    export TEST_TOKEN=<測試帳號 Supabase access token>
    python3 docs/run_intent_tests.py            # 跑全部
    python3 docs/run_intent_tests.py create-01  # 只跑指定 id（可多個）

檢查三件事：intent、欄位、副作用（跑完後 events 筆數必須不變）。
只用 Python 標準函式庫，不需安裝套件。
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("CHAT_API_BASE", "https://weather-api-q8iy.onrender.com").rstrip("/")
USER_ID = os.environ.get("TEST_USER_ID", "")
TOKEN = os.environ.get("TEST_TOKEN", "")
CASES_FILE = Path(__file__).with_name("intent_test_cases.json")


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=90) as resp:
        return json.loads(resp.read().decode())


def count_events():
    res = call("GET", "/api/events?user_id=" + urllib.parse.quote(USER_ID))
    data = res.get("data") if isinstance(res, dict) else res
    return len(data or [])


def norm(v):
    return v.replace("台", "臺") if isinstance(v, str) else v


def diff(expected, actual, path=""):
    """部分比對：只檢查 expected 裡有列出的 key。回傳不符合的描述清單。"""
    errors = []
    for key, exp in expected.items():
        if key.startswith("_"):
            continue
        here = f"{path}.{key}" if path else key
        if key == "missing_fields_include":
            got = (actual or {}).get("missing_fields") or []
            lacking = [f for f in exp if f not in got]
            if lacking:
                errors.append(f"missing_fields 缺少 {lacking}（實際 {got}）")
            continue
        got = (actual or {}).get(key)
        if isinstance(exp, dict):
            if not isinstance(got, dict):
                errors.append(f"{here} 應為物件，實際 {got!r}")
            else:
                errors += diff(exp, got, here)
        elif norm(exp) != norm(got):
            errors.append(f"{here} 預期 {exp!r}，實際 {got!r}")
    return errors


def main():
    if not USER_ID or not TOKEN:
        sys.exit("請先設定 TEST_USER_ID 與 TEST_TOKEN（丟棄式測試帳號）")

    suite = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    only = set(sys.argv[1:])
    cases = [c for c in suite["cases"] if not only or c["id"] in only]

    before = count_events()
    passed = 0
    for case in cases:
        body = {
            "user_id": USER_ID,
            "contract_version": 2,
            "message": case["input"],
            "client_now": suite["client_now"],
            "current_location": suite["current_location"],
            "intent_hint": case.get("intent_hint"),
            "draft_id": None,
            "draft_event": case.get("draft_event"),
        }
        try:
            res = call("POST", "/api/chat", body)
            errors = diff(case["expect"], res)
            if not res.get("reply"):
                errors.append("reply 為空（必填）")
            if bool(res.get("needs_clarification")) != bool(res.get("missing_fields")):
                errors.append("needs_clarification 與 missing_fields 不一致")
        except Exception as e:  # noqa: BLE001
            errors = [f"呼叫失敗：{e}"]

        if errors:
            print(f"❌ {case['id']}  「{case['input']}」")
            for e in errors:
                print(f"     - {e}")
        else:
            passed += 1
            print(f"✅ {case['id']}")

    after = count_events()
    print(f"\n意圖／欄位：{passed}/{len(cases)} 通過")
    if after == before:
        print(f"副作用：✅ events 筆數不變（{before}）")
    else:
        print(f"副作用：❌ events 筆數 {before} → {after}，/api/chat 仍在異動行程資料！")
    sys.exit(0 if passed == len(cases) and after == before else 1)


if __name__ == "__main__":
    main()

