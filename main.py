#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WhaleWinRateBot V2 (Integrated: Tracking + Discovery)
- Receives Helius Enhanced Transaction webhooks for DEX swaps (Raydium/Orca/Jupiter ...)
- Maintains per-wallet positions and realizes PnL on SELL (base = USDC/wSOL)
- Computes win-rate per wallet (lifetime + time windows) and discovers long-term profitable wallets
- Telegram Bot provides: /top, /stats, /follow, /unfollow, /discover, /toplong, /ping
- Optional internal API: POST /api/follow  (Authorization: Bearer <INTERNAL_API_KEY>)

Environment:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  HELIUS_WEBHOOK_SECRET (optional)
  QUOTE_MINTS (default: USDC + wSOL mints)
  MIN_TRADE_BASE (default: 50)
  PORT (default: 3000)
  ADMIN_CHAT_ID (optional)
  TZ (default: Asia/Taipei)
  # Discovery config (defaults aimed at 'long-term winners'):
  DISCOVERY_ENABLE=true
  DISCOVERY_INTERVAL_MIN=180          # run every 180 minutes
  D30_MIN_TRADES=5
  D30_MIN_WINRATE=55
  D30_MIN_PNL=100
  D90_MIN_TRADES=12
  D90_MIN_WINRATE=60
  D90_MIN_PNL=300
  LIFE_MIN_TRADES=30
  LIFE_MIN_WINRATE=58
  LIFE_MIN_PNL=1000
  AUTO_FOLLOW_DISCOVERED=false        # auto add to follows when discovered
  INTERNAL_API_KEY=secret             # used if another service calls /api/follow
"""
import os
import json
import time
import hmac
import hashlib
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- Config ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ADMIN_CHAT_ID   = os.environ.get("ADMIN_CHAT_ID", "").strip()
HELIUS_SECRET   = os.environ.get("HELIUS_WEBHOOK_SECRET", "").strip()
PORT            = int(os.environ.get("PORT", "3000"))
TZ              = os.environ.get("TZ", "Asia/Taipei")

DEFAULT_QUOTES = [
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "So11111111111111111111111111111111111111112",   # wSOL
]
QUOTE_MINTS = [m.strip() for m in os.environ.get("QUOTE_MINTS", ",".join(DEFAULT_QUOTES)).split(",") if m.strip()]
MIN_TRADE_BASE = float(os.environ.get("MIN_TRADE_BASE", "50"))

# Discovery thresholds
DISCOVERY_ENABLE = os.environ.get("DISCOVERY_ENABLE", "true").lower() == "true"
DISCOVERY_INTERVAL_MIN = int(os.environ.get("DISCOVERY_INTERVAL_MIN", "180"))

D30_MIN_TRADES = int(os.environ.get("D30_MIN_TRADES", "5"))
D30_MIN_WINRATE = float(os.environ.get("D30_MIN_WINRATE", "55"))
D30_MIN_PNL = float(os.environ.get("D30_MIN_PNL", "100"))

D90_MIN_TRADES = int(os.environ.get("D90_MIN_TRADES", "12"))
D90_MIN_WINRATE = float(os.environ.get("D90_MIN_WINRATE", "60"))
D90_MIN_PNL = float(os.environ.get("D90_MIN_PNL", "300"))

LIFE_MIN_TRADES = int(os.environ.get("LIFE_MIN_TRADES", "30"))
LIFE_MIN_WINRATE = float(os.environ.get("LIFE_MIN_WINRATE", "58"))
LIFE_MIN_PNL = float(os.environ.get("LIFE_MIN_PNL", "1000"))

AUTO_FOLLOW_DISCOVERED = os.environ.get("AUTO_FOLLOW_DISCOVERED", "false").lower() == "true"
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "").strip()

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("[FATAL] TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing.")
bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# ---------- App & DB ----------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DB_PATH = "data.db"

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS swaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signature TEXT UNIQUE,
        ts INTEGER,
        wallet TEXT,
        direction TEXT,
        base_mint TEXT,
        base_amount REAL,
        token_mint TEXT,
        token_amount REAL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS positions (
        wallet TEXT,
        token_mint TEXT,
        qty REAL DEFAULT 0,
        cost_base REAL DEFAULT 0,
        PRIMARY KEY (wallet, token_mint)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS realized (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        wallet TEXT,
        token_mint TEXT,
        pnl_base REAL,
        base_mint TEXT,
        is_win INTEGER
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS follows (
        wallet TEXT PRIMARY KEY,
        ts INTEGER
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS announced (
        wallet TEXT PRIMARY KEY,      -- discovered and announced to TG
        ts INTEGER
    );
    """)
    conn.commit()
    conn.close()

init_db()

def now_ts() -> int:
    return int(time.time())

# ---------- Utils ----------
def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, (int, float)):
            return float(v)
        return float(str(v).replace(",", ""))
    except Exception:
        return default

def human_ts(ts: int) -> str:
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)

def send_tg(text: str, chat_id: Optional[str] = None):
    if not bot:
        print("[WARN] TG bot not initialized.")
        return
    try:
        bot.send_message(chat_id or TELEGRAM_CHAT_ID, text, disable_web_page_preview=True)
    except Exception as e:
        logging.exception("TG send failed")
        if ADMIN_CHAT_ID:
            try:
                bot.send_message(ADMIN_CHAT_ID, f"[Error] send_tg: {e}")
            except Exception:
                pass

def verify_signature(req) -> bool:
    if not HELIUS_SECRET:
        return True
    try:
        raw = req.get_data()
        sig = req.headers.get("X-Helius-Signature", "")
        expected = hmac.new(HELIUS_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False

# ---------- Core PnL Logic ----------
def upsert_position(conn: sqlite3.Connection, wallet: str, token_mint: str, qty_delta: float, base_delta: float):
    cur = conn.cursor()
    cur.execute("SELECT qty, cost_base FROM positions WHERE wallet=? AND token_mint=?", (wallet, token_mint))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO positions (wallet, token_mint, qty, cost_base) VALUES (?,?,?,?)",
                    (wallet, token_mint, 0.0, 0.0))
        qty = 0.0
        cost = 0.0
    else:
        qty = safe_float(row["qty"])
        cost = safe_float(row["cost_base"])

    qty_new  = qty + qty_delta
    cost_new = cost + base_delta

    if abs(qty_new) < 1e-9: qty_new = 0.0
    if abs(cost_new) < 1e-9: cost_new = 0.0

    cur.execute("UPDATE positions SET qty=?, cost_base=? WHERE wallet=? AND token_mint=?",
                (qty_new, cost_new, wallet, token_mint))
    conn.commit()

def realize_sell(conn: sqlite3.Connection, wallet: str, token_mint: str, sell_qty: float, base_received: float, base_mint: str) -> float:
    cur = conn.cursor()
    cur.execute("SELECT qty, cost_base FROM positions WHERE wallet=? AND token_mint=?", (wallet, token_mint))
    row = cur.fetchone()
    qty = safe_float(row["qty"]) if row else 0.0
    cost = safe_float(row["cost_base"]) if row else 0.0

    if sell_qty <= 0 or base_received <= 0 or qty <= 0:
        upsert_position(conn, wallet, token_mint, -sell_qty, 0.0)
        return 0.0

    portion = min(1.0, sell_qty / qty)
    cost_portion = cost * portion
    pnl = base_received - cost_portion

    upsert_position(conn, wallet, token_mint, -sell_qty, -cost_portion)

    cur.execute(
        "INSERT INTO realized (ts, wallet, token_mint, pnl_base, base_mint, is_win) VALUES (?,?,?,?,?,?)",
        (now_ts(), wallet, token_mint, pnl, base_mint, 1 if pnl > 0 else 0)
    )
    conn.commit()
    return pnl

def insert_swap(conn: sqlite3.Connection, sig: str, ts: int, wallet: str, direction: str, base_mint: str, base_amount: float, token_mint: str, token_amount: float) -> bool:
    try:
        conn.execute("""
        INSERT INTO swaps (signature, ts, wallet, direction, base_mint, base_amount, token_mint, token_amount)
        VALUES (?,?,?,?,?,?,?,?)
        """, (sig, ts, wallet, direction, base_mint, base_amount, token_mint, token_amount))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

# ---------- Helius Enhanced Tx Parser ----------
def parse_swap_from_helius_obj(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        signature = obj.get("signature") or obj.get("transaction", {}).get("signatures", [None])[0]
        ts = int(obj.get("timestamp") or obj.get("blockTime") or now_ts())

        events = obj.get("events") or {}
        swap = events.get("swap") or obj.get("swap") or {}

        if not swap and isinstance(events, list):
            for ev in events:
                if isinstance(ev, dict) and ev.get("type", "").upper() == "SWAP":
                    swap = ev.get("info", {}) or ev
                    break

        user_wallet = (swap.get("userAccount") or swap.get("authority") or swap.get("user") or
                       obj.get("accountData", [{}])[0].get("account") or
                       obj.get("feePayer") or obj.get("source"))

        token_in_mint  = swap.get("tokenIn") or swap.get("mintIn") or swap.get("fromMint")
        token_out_mint = swap.get("tokenOut") or swap.get("mintOut") or swap.get("toMint")
        token_in_amt   = safe_float(swap.get("amountIn") or swap.get("tokenInAmount"))
        token_out_amt  = safe_float(swap.get("amountOut") or swap.get("tokenOutAmount"))

        if not (user_wallet and token_in_mint and token_out_mint and (token_in_amt > 0 or token_out_amt > 0)):
            return None

        direction = None
        base_mint = None
        base_amt  = 0.0
        token_mint = None
        token_amt  = 0.0

        if token_in_mint in QUOTE_MINTS and token_out_mint not in QUOTE_MINTS:
            direction = "BUY"
            base_mint = token_in_mint
            base_amt  = token_in_amt
            token_mint = token_out_mint
            token_amt  = token_out_amt
        elif token_out_mint in QUOTE_MINTS and token_in_mint not in QUOTE_MINTS:
            direction = "SELL"
            base_mint = token_out_mint
            base_amt  = token_out_amt
            token_mint = token_in_mint
            token_amt  = token_in_amt
        else:
            return None

        if base_amt < MIN_TRADE_BASE:
            return None

        return {
            "signature": signature,
            "ts": ts,
            "wallet": str(user_wallet),
            "direction": direction,
            "base_mint": base_mint,
            "base_amount": float(base_amt),
            "token_mint": token_mint,
            "token_amount": float(token_amt),
        }
    except Exception:
        logging.exception("parse_swap_from_helius_obj failed")
        return None

# ---------- Flask routes ----------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": int(time.time())})

@app.route("/helius", methods=["POST"])
def helius_webhook():
    if not verify_signature(request):
        return ("Forbidden", 403)

    try:
        payload = request.get_json(force=True, silent=True)
    except Exception:
        payload = None

    if payload is None:
        return jsonify({"ok": False, "msg": "no json"}), 400

    items: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], list):
            items = payload["data"]
        else:
            items = [payload]
    else:
        return jsonify({"ok": False, "msg": "bad payload"}), 400

    conn = db()
    new_swaps = 0
    realized_msgs = []

    for obj in items:
        data = parse_swap_from_helius_obj(obj)
        if not data:
            continue

        ok = insert_swap(conn, data["signature"], data["ts"], data["wallet"], data["direction"],
                         data["base_mint"], data["base_amount"], data["token_mint"], data["token_amount"])
        if not ok:
            continue

        new_swaps += 1

        if data["direction"] == "BUY":
            upsert_position(conn, data["wallet"], data["token_mint"], data["token_amount"], data["base_amount"])
        else:
            pnl = realize_sell(conn, data["wallet"], data["token_mint"], data["token_amount"], data["base_amount"], data["base_mint"])
            if abs(pnl) > 0:
                sign = "✅" if pnl > 0 else "❌"
                realized_msgs.append(f"{sign} 實現損益 {pnl:.2f} ({'USDC' if data['base_mint']==DEFAULT_QUOTES[0] else 'SOL'})\n"
                                     f"錢包: `{data['wallet']}`\n代幣: {data['token_mint']}\n成交: {data['base_amount']:.2f}\n時間: {human_ts(data['ts'])}")

        cur = conn.cursor()
        cur.execute("SELECT 1 FROM follows WHERE wallet=?", (data["wallet"],))
        if cur.fetchone():
            emoji = "🟢" if data["direction"] == "BUY" else "🔴"
            base_symbol = "USDC" if data["base_mint"] == DEFAULT_QUOTES[0] else "SOL"
            msg = (f"{emoji} {data['direction']} 觸發\n"
                   f"錢包: `{data['wallet']}`\n"
                   f"代幣: {data['token_mint']}\n"
                   f"金額: {data['base_amount']:.2f} {base_symbol}\n"
                   f"時間: {human_ts(data['ts'])}")
            send_tg(msg)

    conn.close()

    for m in realized_msgs:
        send_tg(m)

    return jsonify({"ok": True, "received": len(items), "new_swaps": new_swaps})

# ---- Internal API for split-architecture (optional) ----
@app.route("/api/follow", methods=["POST"])
def api_follow():
    auth = request.headers.get("Authorization", "")
    if not INTERNAL_API_KEY or not auth.startswith("Bearer "):
        return ("Unauthorized", 401)
    token = auth.split(" ", 1)[1].strip()
    if token != INTERNAL_API_KEY:
        return ("Forbidden", 403)
    data = request.get_json(silent=True) or {}
    wallet = (data.get("wallet") or "").strip()
    if not wallet:
        return jsonify({"ok": False, "msg": "wallet required"}), 400
    conn = db()
    try:
        conn.execute("INSERT OR REPLACE INTO follows (wallet, ts) VALUES (?,?)", (wallet, now_ts()))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "wallet": wallet})

# ---------- Ranking & Windows ----------
def get_top_wallets(min_trades: int = 3, limit: int = 10) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT wallet,
               SUM(is_win) AS wins,
               COUNT(*) AS trades,
               ROUND(100.0 * SUM(is_win) / COUNT(*), 2) AS win_rate_pct,
               ROUND(SUM(pnl_base),2) AS pnl_sum,
               COUNT(DISTINCT token_mint) AS tokens
        FROM realized
        GROUP BY wallet
        HAVING trades >= ?
        ORDER BY win_rate_pct DESC, pnl_sum DESC
        LIMIT ?
    """, (min_trades, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_window_stats(days: int) -> List[sqlite3.Row]:
    since = now_ts() - days * 86400
    conn = db()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT wallet,
               SUM(is_win) AS wins,
               COUNT(*) AS trades,
               ROUND(100.0 * SUM(is_win) / COUNT(*), 2) AS win_rate_pct,
               ROUND(SUM(pnl_base),2) AS pnl_sum
        FROM realized
        WHERE ts >= ?
        GROUP BY wallet
        ORDER BY win_rate_pct DESC, pnl_sum DESC
    """, (since,))
    rows = cur.fetchall()
    conn.close()
    return rows

def filter_long_term_winners() -> List[Dict[str, Any]]:
    # Intersect 30d, 90d, and lifetime thresholds
    life = { r["wallet"]: r for r in get_top_wallets(min_trades=1, limit=100000) }  # full list
    d30  = { r["wallet"]: r for r in get_window_stats(30) }
    d90  = { r["wallet"]: r for r in get_window_stats(90) }

    winners = []
    for w, life_row in life.items():
        # Lifetime checks
        life_trades = int(life_row["trades"])
        life_win = float(life_row["win_rate_pct"])
        life_pnl = float(life_row["pnl_sum"])

        if life_trades < LIFE_MIN_TRADES or life_win < LIFE_MIN_WINRATE or life_pnl < LIFE_MIN_PNL:
            continue

        d30_row = d30.get(w)
        d90_row = d90.get(w)
        if not d30_row or not d90_row:
            continue

        d30_trades = int(d30_row["trades"])
        d30_win = float(d30_row["win_rate_pct"])
        d30_pnl = float(d30_row["pnl_sum"])

        d90_trades = int(d90_row["trades"])
        d90_win = float(d90_row["win_rate_pct"])
        d90_pnl = float(d90_row["pnl_sum"])

        if d30_trades >= D30_MIN_TRADES and d30_win >= D30_MIN_WINRATE and d30_pnl >= D30_MIN_PNL \
           and d90_trades >= D90_MIN_TRADES and d90_win >= D90_MIN_WINRATE and d90_pnl >= D90_MIN_PNL:
            winners.append({
                "wallet": w,
                "life": life_row,
                "d30": d30_row,
                "d90": d90_row
            })
    return winners

def format_winner_msg(w: Dict[str, Any]) -> str:
    return (
        "🧭 發現長期贏家\n"
        f"錢包: `{w['wallet']}`\n"
        f"30d 勝率: {w['d30']['win_rate_pct']}% / 回合 {w['d30']['trades']} / 淨PnL {w['d30']['pnl_sum']}\n"
        f"90d 勝率: {w['d90']['win_rate_pct']}% / 回合 {w['d90']['trades']} / 淨PnL {w['d90']['pnl_sum']}\n"
        f"總累積: 勝率 {w['life']['win_rate_pct']}% / 回合 {w['life']['trades']} / 淨PnL {w['life']['pnl_sum']}\n"
        "（可用 `/follow <wallet>` 追蹤）"
    )

# ---------- Discovery Job ----------
def discovery_job():
    try:
        conn = db()
        cur = conn.cursor()
        winners = filter_long_term_winners()

        # Fetch already announced
        cur.execute("SELECT wallet FROM announced")
        announced = {row["wallet"] for row in cur.fetchall()}

        new_winners = [w for w in winners if w["wallet"] not in announced]

        for w in new_winners:
            send_tg(format_winner_msg(w))
            if AUTO_FOLLOW_DISCOVERED:
                try:
                    conn.execute("INSERT OR REPLACE INTO follows (wallet, ts) VALUES (?,?)", (w["wallet"], now_ts()))
                    conn.commit()
                    send_tg(f"✅ 已自動追蹤 `{w['wallet']}`", TELEGRAM_CHAT_ID)
                except Exception:
                    logging.exception("auto-follow failed")

            # mark announced
            try:
                conn.execute("INSERT OR REPLACE INTO announced (wallet, ts) VALUES (?,?)", (w["wallet"], now_ts()))
                conn.commit()
            except Exception:
                logging.exception("announce mark failed")

        conn.close()
    except Exception:
        logging.exception("discovery_job failed")

# ---------- Telegram handlers ----------
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        min_trades = int(context.args[0]) if len(context.args) >= 1 else 3
        limit = int(context.args[1]) if len(context.args) >= 2 else 10
    except Exception:
        min_trades, limit = 3, 10
    rows = get_top_wallets(min_trades=min_trades, limit=limit)
    if not rows:
        await update.message.reply_text("目前沒有達標的錢包紀錄。")
        return
    lines = ["🏆 高勝率錢包排行榜"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i}. `{r['wallet']}` | 勝率 {r['win_rate_pct']}% ({r['wins']}/{r['trades']}) | 累計PnL {r['pnl_sum']} | 幣種 {r['tokens']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法: /stats <wallet>")
        return
    wallet = context.args[0].strip()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT SUM(is_win) AS wins, COUNT(*) AS trades, ROUND(100.0*SUM(is_win)/COUNT(*),2) AS win_rate_pct,
               ROUND(SUM(pnl_base),2) AS pnl_sum
        FROM realized WHERE wallet=?
    """, (wallet,))
    summary = cur.fetchone()
    cur.execute("""
        SELECT token_mint,
               SUM(CASE WHEN pnl_base>0 THEN 1 ELSE 0 END) AS wins,
               COUNT(*) AS trades,
               ROUND(SUM(pnl_base),2) AS pnl_sum
        FROM realized WHERE wallet=?
        GROUP BY token_mint
        ORDER BY pnl_sum DESC
        LIMIT 10
    """, (wallet,))
    per_token = cur.fetchall()
    conn.close()

    if not summary or summary["trades"] is None:
        await update.message.reply_text("查無此錢包的實現紀錄。")
        return

    lines = [
        f"📊 錢包 `{wallet}`",
        f"勝率: {summary['win_rate_pct']}% ({summary['wins']}/{summary['trades']})",
        f"累計PnL: {summary['pnl_sum']}",
        "前10代幣："
    ]
    for r in per_token:
        lines.append(f"- {r['token_mint']} | 勝 {r['wins']}/{r['trades']} | PnL {r['pnl_sum']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_follow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法: /follow <wallet>")
        return
    wallet = context.args[0].strip()
    conn = db()
    try:
        conn.execute("INSERT OR REPLACE INTO follows (wallet, ts) VALUES (?,?)", (wallet, now_ts()))
        conn.commit()
        await update.message.reply_text(f"✅ 已關注 `{wallet}`", parse_mode="Markdown")
    finally:
        conn.close()

async def cmd_unfollow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法: /unfollow <wallet>")
        return
    wallet = context.args[0].strip()
    conn = db()
    try:
        conn.execute("DELETE FROM follows WHERE wallet=?", (wallet,))
        conn.commit()
        await update.message.reply_text(f"✅ 已取消關注 `{wallet}`", parse_mode="Markdown")
    finally:
        conn.close()

async def cmd_discover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    discovery_job()
    await update.message.reply_text("已執行探索任務。")

async def cmd_toplong(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(context.args[0]) if len(context.args) >= 1 else 90
        min_trades = int(context.args[1]) if len(context.args) >= 2 else 10
        min_win = float(context.args[2]) if len(context.args) >= 3 else 58.0
        min_pnl = float(context.args[3]) if len(context.args) >= 4 else 300.0
    except Exception:
        days, min_trades, min_win, min_pnl = 90, 10, 58.0, 300.0
    rows = get_window_stats(days)
    rows = [r for r in rows if int(r["trades"]) >= min_trades and float(r["win_rate_pct"]) >= min_win and float(r["pnl_sum"]) >= min_pnl]
    if not rows:
        await update.message.reply_text("該條件下查無結果。")
        return
    lines = [f"📅 {days} 天長期排行 (minTrades={min_trades}, minWin={min_win}%, minPnL={min_pnl})"]
    for i, r in enumerate(rows[:20], start=1):
        lines.append(f"{i}. `{r['wallet']}` | 勝率 {r['win_rate_pct']}% | 回合 {r['trades']} | 淨PnL {r['pnl_sum']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

def start_telegram_polling():
    if not TELEGRAM_TOKEN:
        print("[WARN] TELEGRAM_TOKEN not set; Telegram features disabled.")
        return
    import asyncio
    # >>> 新增這三行：在子執行緒建立並設定 event loop（Py3.12 必要）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # <<<

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.run_polling(drop_pending_updates=True)


# ---------- Scheduler ----------
def periodic_top_push():
    try:
        rows = get_top_wallets(min_trades=3, limit=10)
        if not rows:
            return
        lines = ["🏆 高勝率錢包排行榜（自動推送）"]
        for i, r in enumerate(rows, start=1):
            lines.append(f"{i}. `{r['wallet']}` | 勝率 {r['win_rate_pct']}% ({r['wins']}/{r['trades']}) | 累計PnL {r['pnl_sum']}")
        send_tg("\n".join(lines))
    except Exception:
        logging.exception("periodic_top_push failed")

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    # leaderboard every 30 min
    sched.add_job(periodic_top_push, "cron", minute="*/30")
    if DISCOVERY_ENABLE:
        sched.add_job(discovery_job, "interval", minutes=DISCOVERY_INTERVAL_MIN, next_run_time=None)
    sched.start()

# ---------- Main ----------
if __name__ == "__main__":
    t = threading.Thread(target=start_telegram_polling, daemon=True)
    t.start()
    start_scheduler()
    app.run(host="0.0.0.0", port=PORT)
