#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kensaki-confirmation-loop.py — 確認フローの回転ループ

出典：Notion「ケンサキ丸 × 一本釣り　全体まとめ（2026-09-27）」
      ✅ 有効な決定 ＞「確認フローの回転ループ（2026-10-06設定）」
      （判定の型は同じく「確認フローの固定化（2026-10-06設定）」）

節に書かれている規則（このスクリプトが守ること）：
  - Grokが「調整」または「停め」を返した時：理由と修正案を1行で返す。
  - Chiefが専門Botへ直させ、結果を再提出。満足するまで繰り返す（回数に上限なし）。
  - 同じ調整が3回続いたら、4回目は自動で「停め」にし、kouへ理由を1行で報告する。
  - kouの「出して」は、Grokが「OK」を返した後のみ。確認が通るまで
    公開・投稿・お金に関わる作業は停まる。例外なし。
  - 学び：同じ調整が2回目になったら、その手順のスキルを自分で書き直す。
  - 状態は同フォルダの kensaki-loop-state.json に記録する。
  - 使い方：--task "<指示>" で1回転。--status で現在のループ状態を表示。

このスクリプトがやらないこと（kouの「出して」が必要な作業は入れない）：
  公開・Xへの投稿・公開ページの更新・メッセージ送信・支払い・push など。
  OKが出ても「kouの『出して』待ち」と表示して止まるだけ。
  kouへの1行報告も「表示」するだけで、送信はしない（Chiefが伝える）。
  スキルの書き直しも、印（フラグ）を状態ファイルに残して表示するだけ。

使い方の例：
  # 1回転目の確認を始める（確認視点と現在の回数を表示。記録はしない）
  python3 kensaki-confirmation-loop.py --task "<指示>"

  # Grokの判定を記録する（1回転）
  python3 kensaki-confirmation-loop.py --task "<指示>" --verdict 調整 \\
      --reason "<理由>" --fix "<修正案>"
  python3 kensaki-confirmation-loop.py --task "<指示>" --verdict OK

  # 状態を見る / 試す
  python3 kensaki-confirmation-loop.py --status
  python3 kensaki-confirmation-loop.py --self-test
  python3 kensaki-confirmation-loop.py --task "<指示>" --verdict 調整 \\
      --reason "<理由>" --dry-run   # 状態ファイルを書かない
"""

import argparse
import datetime
import json
import os
import sys
import tempfile

SECTION = "全体まとめ ＞ 有効な決定 ＞ 確認フローの回転ループ（2026-10-06設定）"
DEFAULT_STATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "kensaki-loop-state.json"
)

# 判定の種類（確認フローの固定化：OK／調整／停め）
VERDICTS = {"OK": "OK", "ok": "OK", "調整": "調整", "停め": "停め"}

# 同じ調整の回数のしきい値（節の規則どおり）
SKILL_REWRITE_AT = 2   # 2回目でスキル書き直し
AUTO_STOP_AFTER = 3    # 3回続いたら、4回目は自動で「停め」

# Grokの確認視点四点（確認フローの固定化）
CHECKPOINTS = [
    "① kouの決定と一致しているか",
    "② 「出して」ゲートを守っているか（公開・投稿・お金に関わる作業は「出して」まで待つ）",
    "③ 実行にかかる手順はkouのパスワード・認証だけか",
    "④ 学びはあるか（同じ失敗を繰り返していないか）",
]

GATE_MSG = ("「出して」ゲート：閉。確認が通るまで公開・投稿・お金に関わる作業は停まる"
            "（例外なし）。")
OK_MSG = ("OK：確認が通った。次はkouの「出して」待ち。"
          "このスクリプトは公開・投稿・送信・支払いを一切しない。")


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def norm(text):
    """同じ調整かどうかを比べるための正規化（空白を詰めるだけ）。"""
    return " ".join((text or "").split())


def load_state(path):
    if not os.path.exists(path):
        return {"section": SECTION, "tasks": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def task_entry(state, task):
    return state["tasks"].setdefault(task, {
        "status": "確認中",        # 確認中 / OK（出して待ち） / 停め / 自動停め
        "rounds": [],
        "same_adjust_key": None,
        "same_adjust_count": 0,
        "skill_rewrite": [],
    })


def one_line(*parts):
    """1行にまとめる（改行を消す）。"""
    return " ／ ".join(norm(p) for p in parts if norm(p))


def run_round(state, task, verdict, reason, fix, key):
    """1回転を記録し、表示する行のリストを返す。"""
    out = []
    t = task_entry(state, task)
    n = len(t["rounds"]) + 1

    if t["status"] in ("停め", "自動停め"):
        out.append(f"[{n}回目] このタスクは「{t['status']}」で止まっている。"
                   "再開はkouの判断のあと --reset で。")
        out.append(GATE_MSG)
        return out, False
    if t["status"].startswith("OK"):
        out.append(f"[{n}回目] このタスクは確認済み（OK）。kouの「出して」待ち。")
        return out, False

    # 同じ調整が3回続いた後の回転は、判定を待たずに自動で「停め」
    if t["same_adjust_count"] >= AUTO_STOP_AFTER:
        line = one_line(f"自動停め：同じ調整が{t['same_adjust_count']}回続いた",
                        t["same_adjust_key"])
        t["rounds"].append({"n": n, "at": now(), "verdict": "停め",
                            "auto": True, "reason": line, "fix": ""})
        t["status"] = "自動停め"
        out.append(f"[{n}回目] 停め（自動）")
        out.append(f"kouへの1行報告（表示のみ・送信しない）：{task}：{line}")
        out.append(GATE_MSG)
        return out, True

    if verdict is None:
        out.append(f"[{n}回目] 確認待ち：{task}")
        out.append("Grokの確認視点：")
        out.extend("  " + c for c in CHECKPOINTS)
        out.append("判定を --verdict OK / 調整 / 停め で記録する"
                   "（調整・停めは --reason と --fix も）。")
        if t["same_adjust_count"]:
            out.append(f"  いまの同じ調整の連続：{t['same_adjust_count']}回"
                       f"（{t['same_adjust_key']}）")
        out.append(GATE_MSG)
        return out, False

    rec = {"n": n, "at": now(), "verdict": verdict, "auto": False,
           "reason": norm(reason), "fix": norm(fix)}
    t["rounds"].append(rec)

    if verdict == "OK":
        t["status"] = "OK（出して待ち）"
        t["same_adjust_key"], t["same_adjust_count"] = None, 0
        out.append(f"[{n}回目] OK")
        out.append(OK_MSG)
        return out, True

    label = "修正案" if verdict == "調整" else "代替案"
    out.append(f"[{n}回目] {verdict}：" + one_line(
        f"理由：{reason}", f"{label}：{fix}" if norm(fix) else ""))

    if verdict == "停め":
        t["status"] = "停め"
        out.append(GATE_MSG)
        return out, True

    # 調整：同じ調整が続いているか数える
    k = norm(key) or norm(reason)
    if k and k == t["same_adjust_key"]:
        t["same_adjust_count"] += 1
    else:
        t["same_adjust_key"], t["same_adjust_count"] = k, 1
    c = t["same_adjust_count"]
    out.append(f"  同じ調整の連続：{c}回（{k}）")
    out.append("  → Chiefが専門Botへ直させ、結果を再提出（回数に上限なし）。")

    if c == SKILL_REWRITE_AT:
        t["skill_rewrite"].append({"at": now(), "round": n, "adjust": k})
        out.append("  ★ スキル書き直し：同じ調整が2回目。"
                   "その手順のスキルを自分で書き直す（学習ループの規則どおり）。")
    if c >= AUTO_STOP_AFTER:
        out.append(f"  ！ 同じ調整が{c}回続いた。次（{n + 1}回目）は自動で「停め」になる。")
    out.append(GATE_MSG)
    return out, True


def show_status(state, task=None):
    lines = [f"状態（{SECTION}）"]
    tasks = state.get("tasks", {})
    if task:
        tasks = {task: tasks[task]} if task in tasks else {}
    if not tasks:
        lines.append("  記録なし")
    for name, t in tasks.items():
        lines.append(f"- {name}")
        lines.append(f"    状態：{t['status']}　回転数：{len(t['rounds'])}")
        if t["same_adjust_count"]:
            lines.append(f"    同じ調整の連続：{t['same_adjust_count']}回"
                         f"（{t['same_adjust_key']}）")
        if t["skill_rewrite"]:
            lines.append(f"    スキル書き直しの印：{len(t['skill_rewrite'])}件")
        for r in t["rounds"]:
            auto = "（自動）" if r.get("auto") else ""
            lines.append(f"    [{r['n']}] {r['verdict']}{auto} {r['reason']}"
                         + (f" → {r['fix']}" if r.get("fix") else ""))
    return lines


def self_test():
    """一時ファイルで動きを確かめる（本物の状態ファイルには触らない）。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        task = "テスト用の指示"
        reason = "見出しの語尾がキャラ正本と違う"
        print(f"== self-test（状態ファイル：一時 {path}）==")
        flags = []
        for i in range(1, 5):
            st = load_state(path)
            v = "調整" if i <= 3 else None  # 4回目は判定なしで回す
            out, changed = run_round(st, task, v, reason, "語尾を直す", None)
            if changed:
                save_state(path, st)
            print("\n".join(out))
            flags.append(out)
        st = load_state(path)
        t = st["tasks"][task]
        assert any("スキル書き直し" in l for l in flags[1]), "2回目の印がない"
        assert not any("スキル書き直し" in l for l in flags[0] + flags[2])
        assert t["status"] == "自動停め", t["status"]
        assert t["rounds"][3]["auto"] is True and t["rounds"][3]["verdict"] == "停め"
        assert len(t["skill_rewrite"]) == 1
        # 違う調整が挟まると連続は切れる
        st2 = {"section": SECTION, "tasks": {}}
        run_round(st2, "b", "調整", "A", "", None)
        run_round(st2, "b", "調整", "B", "", None)
        assert st2["tasks"]["b"]["same_adjust_count"] == 1
        # OKの後は「出して」待ちで止まる（何も実行しない）
        st3 = {"section": SECTION, "tasks": {}}
        out, _ = run_round(st3, "c", "OK", "", "", None)
        assert st3["tasks"]["c"]["status"] == "OK（出して待ち）"
        print("\n".join(out))
        print("\n".join(show_status(st)))
    print("== self-test OK ==")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="確認フローの回転ループ（出典：" + SECTION + "）。"
                    "公開・投稿・送信・支払いはしない。OKの後はkouの「出して」待ちで止まる。")
    p.add_argument("--task", help="確認する指示（1回転＝この指示の確認1回）")
    p.add_argument("--verdict", choices=sorted(set(VERDICTS)),
                   help="Grokの判定：OK / 調整 / 停め")
    p.add_argument("--reason", default="", help="理由（1行）")
    p.add_argument("--fix", default="", help="修正案（調整）／代替案（停め）（1行）")
    p.add_argument("--key", default="",
                   help="同じ調整かを見分ける名札（省略時は理由の文で比べる）")
    p.add_argument("--status", action="store_true", help="現在のループ状態を表示")
    p.add_argument("--reset", action="store_true",
                   help="--task の記録を消す（停めの後、kouの判断で再開する時）")
    p.add_argument("--state", default=DEFAULT_STATE,
                   help="状態ファイル（既定：同フォルダの kensaki-loop-state.json）")
    p.add_argument("--dry-run", action="store_true", help="状態ファイルを書かない")
    p.add_argument("--self-test", action="store_true",
                   help="一時ファイルで2回目のスキル書き直しと4回目の自動停めを確かめる")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()

    state = load_state(a.state)
    if a.status:
        print("\n".join(show_status(state, a.task)))
        return 0
    if not a.task:
        p.error("--task か --status か --self-test を指定する")
    if a.reset:
        state["tasks"].pop(a.task, None)
        if not a.dry_run:
            save_state(a.state, state)
        print(f"記録を消した：{a.task}" + ("（dry-run・未保存）" if a.dry_run else ""))
        return 0
    verdict = VERDICTS[a.verdict] if a.verdict else None
    if verdict in ("調整", "停め") and not norm(a.reason):
        p.error("調整・停めには --reason（理由1行）が必要")

    out, changed = run_round(state, a.task, verdict, a.reason, a.fix, a.key)
    print("\n".join(out))
    if changed and not a.dry_run:
        save_state(a.state, state)
    elif changed:
        print("（dry-run：状態ファイルは書いていない）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
