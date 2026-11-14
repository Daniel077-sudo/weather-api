#!/usr/bin/env python3
"""/api/chat v2 Golden Test。

用法（務必使用丟棄式測試帳號，這支腳本會真的呼叫 /api/chat，也會花 Gemini 額度）：
    export CHAT_API_BASE=https://weather-api-q8iy.onrender.com
    export TEST_USER_ID=<測試帳號 user_id>
    export TEST_TOKEN=<測試帳號 Supabase access token>
    python3 docs/run_intent_tests.py                 # 跑全部
    python3 docs/run_intent_tests.py create-01       # 只跑指定 id（可多個）
    python3 docs/run_intent_tests.py --prefix v1110  # 只跑 id 開頭符合的
    python3 docs/run_intent_tests.py --repeat 3      # 每題問 3 次，檢查回答穩不穩定

檢查：intent、欄位、reply 不可空白、needs_clarification 與 missing_fields 一致、
副作用（跑完後 events 筆數必須不變）。
最後分開統計「無法回覆」（逾時、HTTP 錯誤、空白回覆、AI 失敗）與「答錯」，兩者修法不同。
只用 Python 標準函式庫，不需安裝套件。

（2026-11-10）新增：先叫醒 Render（免費方案休眠時第一個請求要 1～2 分鐘）、
每題耗時與 gemini_status、intent_in、draft_event_absent、--repeat、--prefix。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

BASE = os.environ.get("CHAT_API_BASE", "https://weather-api-q8iy.onrender.com").rstrip("/")
USER_ID = os.environ.get("TEST_USER_ID", "")
TOKEN = os.environ.get("TEST_TOKEN", "")
CASES_FILE = Path(__file__).with_name("intent_test_cases.json")
SLOW_SECONDS = 15

USAGE = """用法：
  python docs/run_intent_tests.py [case-id ...]
  python docs/run_intent_tests.py --prefix PREFIX
  python docs/run_intent_tests.py --repeat N

環境變數：
  CHAT_API_BASE  API 根網址（預設為正式 Render 網址）
  TEST_USER_ID   丟棄式測試帳號 UUID
  TEST_TOKEN     該帳號的 Supabase access token

注意：腳本會呼叫正式 API 並消耗 Gemini 額度，但不應新增、修改或刪除行程。
"""


def call(method, path, body=None, timeout=90):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def wake_up():
    """Render 免費方案閒置會休眠：先打一次首頁等它醒，免得第一題被算成逾時。"""
    start = time.time()
    try:
        urllib.request.urlopen(BASE + "/", timeout=180).read()
        print(f"後端已就緒（{time.time() - start:.1f} 秒）\n")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"後端 180 秒內都沒回應，先不測：{e}")


def count_events():
    res = call("GET", "/api/events?user_id=" + urllib.parse.quote(USER_ID))
    data = res.get("data") if isinstance(res, dict) else res
    return len(data or [])


def norm(v):
    return v.replace("台", "臺") if isinstance(v, str) else v


def diff(expected, actual, path=""):
    """部分比對：只檢查 expected 裡有列出的 key。回傳不符合的描述清單。"""
    errors = []
    actual = actual or {}
    for key, exp in expected.items():
        if key.startswith("_"):
            continue
        here = f"{path}.{key}" if path else key
        if key == "missing_fields_include":
            got = actual.get("missing_fields") or []
            lacking = [f for f in exp if f not in got]
            if lacking:
                errors.append(f"missing_fields 缺少 {lacking}（實際 {got}）")
            continue
        if key == "intent_in":
            if actual.get("intent") not in exp:
                errors.append(f"intent 應為 {exp} 其中之一，實際 {actual.get('intent')!r}")
            continue
        if key == "draft_event_absent":
            draft = actual.get("draft_event") or {}
            filled = {f: draft.get(f) for f in exp if draft.get(f) not in (None, "")}
            if filled:
                errors.append(f"draft_event 不該有值卻填了 {filled}（不猜、不補）")
            continue
        got = actual.get(key)
        if isinstance(exp, dict):
            if not isinstance(got, dict):
                errors.append(f"{here} 應為物件，實際 {got!r}")
            else:
                errors += diff(exp, got, here)
        elif norm(exp) != norm(got):
            errors.append(f"{here} 預期 {exp!r}，實際 {got!r}")
    return errors


def run_case(suite, case):
    """回傳 (類別, 錯誤清單, 回應, 秒數)。類別：ok／wrong／no_reply。"""
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
    start = time.time()
    try:
        res = call("POST", "/api/chat", body)
    except urllib.error.HTTPError as e:
        return "no_reply", [f"HTTP {e.code}"], None, time.time() - start
    except Exception as e:  # noqa: BLE001
        return "no_reply", [f"呼叫失敗：{e}"], None, time.time() - start
    secs = time.time() - start
    if not res.get("reply") and res.get("intent") not in {"QUERY_EVENT", "QUERY_WEATHER", "EVENT_WEATHER"}:
        return "no_reply", ["reply 為空（必填）"], res, secs
    if res.get("gemini_status") in ("timeout", "error"):
        return "no_reply", [f"AI 失敗（gemini_status={res.get('gemini_status')}）"], res, secs
    errors = diff(case["expect"], res)
    if bool(res.get("needs_clarification")) != bool(res.get("missing_fields")):
        errors.append("needs_clarification 與 missing_fields 不一致")
    return ("wrong" if errors else "ok"), errors, res, secs


def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(USAGE)
        return
    repeat, prefix = 1, None
    if "--repeat" in args:
        i = args.index("--repeat"); repeat = int(args[i + 1]); del args[i:i + 2]
    if "--prefix" in args:
        i = args.index("--prefix"); prefix = args[i + 1]; del args[i:i + 2]
    if not USER_ID or not TOKEN:
        sys.exit("請先設定 TEST_USER_ID 與 TEST_TOKEN（丟棄式測試帳號）")

    suite = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    only = set(args)
    cases = [c for c in suite["cases"]
             if (not only or c["id"] in only) and (not prefix or c["id"].startswith(prefix))]

    wake_up()
    before = count_events()
    tally, statuses, slow, unstable = Counter(), Counter(), [], []
    for case in cases:
        results = [run_case(suite, case) for _ in range(repeat)]
        for _, _, res, _ in results:
            statuses[(res or {}).get("gemini_status") or "（沒有回傳）"] += 1
        kinds = [r[0] for r in results]
        worst = "no_reply" if "no_reply" in kinds else ("wrong" if "wrong" in kinds else "ok")
        tally[worst] += 1
        secs = max(r[3] for r in results)
        if secs > SLOW_SECONDS:
            slow.append((case["id"], secs))
        if repeat > 1:
            intents = {(r[2] or {}).get("intent") for r in results}
            if len(set(kinds)) > 1 or len(intents) > 1:
                unstable.append(case["id"])
        mark = {"ok": "✅", "wrong": "❌", "no_reply": "⛔"}[worst]
        print(f"{mark} {case['id']}  {secs:.1f}s" + ("" if worst == "ok" else f"  「{case['input']}」"))
        for kind, errors, _, _ in results:
            if kind != "ok":
                for e in errors:
                    print(f"     - {e}")

    after = count_events()
    print(f"\n通過 {tally['ok']}/{len(cases)}　答錯 {tally['wrong']}　無法回覆 {tally['no_reply']}")
    print("gemini_status：" + "、".join(f"{k} {v}" for k, v in statuses.most_common()))
    if slow:
        print(f"超過 {SLOW_SECONDS} 秒：" + "、".join(f"{i}（{s:.0f}s）" for i, s in slow))
    if repeat > 1:
        print(f"同一題 {repeat} 次結果不一樣：" + ("、".join(unstable) if unstable else "沒有"))
    if after == before:
        print(f"副作用：✅ events 筆數不變（{before}）")
    else:
        print(f"副作用：❌ events 筆數 {before} → {after}，/api/chat 仍在異動行程資料！")
    sys.exit(0 if tally["ok"] == len(cases) and after == before else 1)


if __name__ == "__main__":
    main()
