#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# WhaleWinRateBot V2 (Railway 一檔搞定)
# - Helius Enhanced Webhook → /helius
# - 以 USDC/wSOL 為基礎資產計算回合勝率與實現PnL
# - 長期贏家探索（30d/90d + 累積門檻）
# - Telegram 指令：/ping /top /stats /follow /unfollow /discover /toplong
# - 修正：Telegram 於主執行緒、Flask 丟子執行緒（避免 set_wakeup_fd 錯誤）
# - 驗證：Authorization header 直接比對 HELIUS_WEBHOOK_SECRET

import os, json, time, logging, sqlite3, threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ====== 環境變數 ======
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ADMIN_CHAT_ID   = os.environ.get("ADMIN_CHAT_ID", "").strip()
HELIUS_SECRET   = os.environ.get("HELIUS_WEBHOOK_SECRET", "").strip()  # 對應 Helius Authentication Header
PORT            = int(os.environ.get("PORT", "8080"))
TZ              = os.environ.get("TZ", "Asia/Taipei")

# 只統計 USDC / wSOL ↔ 代幣 的交換
DEFAULT_QUOTES = [
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "So11111111111111111111111111111111111111112",   # wSOL
]
QUOTE_MINTS = [m.strip() for m in os.environ.get("QUOTE_MINTS", ",".join(DEFAULT_QUOTES)).split(",") if m.strip()]
MIN_TRADE_BASE = float(os.environ.get("MIN_TRADE_BASE", "50"))

# 長期贏家門檻（可用環境變數調整）
DISCOVERY_ENABLE = os.environ.get("DISCOVERY_ENABLE","true").lower()=="true"
DISCOVERY_INTERVAL_MIN = int(os.environ.get("DISCOVERY_INTERVAL_MIN","180"))
D30_MIN_TRADES = int(os.environ.get("D30_MIN_TRADES","5"))
D30_MIN_WINRATE= float(os.environ.get("D30_MIN_WINRATE","55"))
D30_MIN_PNL    = float(os.environ.get("D30_MIN_PNL","100"))
D90_MIN_TRADES = int(os.environ.get("D90_MIN_TRADES","12"))
D90_MIN_WINRATE= float(os.environ.get("D90_MIN_WINRATE","60"))
D90_MIN_PNL    = float(os.environ.get("D90_MIN_PNL","300"))
LIFE_MIN_TRADES= int(os.environ.get("LIFE_MIN_TRADES","30"))
LIFE_MIN_WINRATE=float(os.environ.get("LIFE_MIN_WINRATE","58"))
LIFE_MIN_PNL   = float(os.environ.get("LIFE_MIN_PNL","1000"))
AUTO_FOLLOW_DISCOVERED = os.environ.get("AUTO_FOLLOW_DISCOVERED","false").lower()=="true"

# ====== 基礎設置 ======
bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
DB_PATH = "data.db"

def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db(); cur=c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS swaps(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signature TEXT UNIQUE, ts INTEGER, wallet TEXT, direction TEXT,
        base_mint TEXT, base_amount REAL, token_mint TEXT, token_amount REAL);""")
    cur.execute("""CREATE TABLE IF NOT EXISTS positions(
        wallet TEXT, token_mint TEXT, qty REAL DEFAULT 0, cost_base REAL DEFAULT 0,
        PRIMARY KEY(wallet, token_mint));""")
    cur.execute("""CREATE TABLE IF NOT EXISTS realized(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, wallet TEXT, token_mint TEXT, pnl_base REAL, base_mint TEXT, is_win INTEGER);""")
    cur.execute("""CREATE TABLE IF NOT EXISTS follows(wallet TEXT PRIMARY KEY, ts INTEGER);""")
    cur.execute("""CREATE TABLE IF NOT EXISTS announced(wallet TEXT PRIMARY KEY, ts INTEGER);""")
    c.commit(); c.close()
init_db()

def now_ts(): return int(time.time())
def safe_float(v, d=0.0):
    try:
        if v is None: return d
        if isinstance(v,(int,float)): return float(v)
        return float(str(v).replace(",",""))
    except: return d
def human_ts(ts):
    try: return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except: return str(ts)

def send_tg(text: str, chat_id: Optional[str]=None):
    if not bot: return
    try:
        bot.send_message(chat_id or TELEGRAM_CHAT_ID, text, disable_web_page_preview=True)
    except Exception as e:
        logging.exception("send_tg failed")
        if ADMIN_CHAT_ID:
            try: bot.send_message(ADMIN_CHAT_ID, f"[Error] {e}")
            except: pass

# ====== Helius Authorization 驗證 ======
def verify_signature(req) -> bool:
    if not HELIUS_SECRET:
        return True
    return req.headers.get("Authorization", "") == HELIUS_SECRET

# ====== 交易處理 / PnL ======
def upsert_position(conn, wallet, token_mint, qty_delta, base_delta):
    cur=conn.cursor(); cur.execute("SELECT qty,cost_base FROM positions WHERE wallet=? AND token_mint=?", (wallet,token_mint))
    row=cur.fetchone(); qty=safe_float(row["qty"]) if row else 0.0; cost=safe_float(row["cost_base"]) if row else 0.0
    qty_new=qty+qty_delta; cost_new=cost+base_delta
    if abs(qty_new)<1e-9: qty_new=0.0
    if abs(cost_new)<1e-9: cost_new=0.0
    cur.execute("INSERT OR REPLACE INTO positions(wallet,token_mint,qty,cost_base) VALUES(?,?,?,?)",
                (wallet, token_mint, qty_new, cost_new)); conn.commit()

def realize_sell(conn, wallet, token_mint, sell_qty, base_received, base_mint):
    cur=conn.cursor(); cur.execute("SELECT qty,cost_base FROM positions WHERE wallet=? AND token_mint=?", (wallet,token_mint))
    row=cur.fetchone(); qty=safe_float(row["qty"]) if row else 0.0; cost=safe_float(row["cost_base"]) if row else 0.0
    if sell_qty<=0 or base_received<=0 or qty<=0:
        upsert_position(conn, wallet, token_mint, -sell_qty, 0.0); return 0.0
    portion=min(1.0, sell_qty/qty); cost_portion=cost*portion
    pnl=base_received - cost_portion
    upsert_position(conn, wallet, token_mint, -sell_qty, -cost_portion)
    cur.execute("INSERT INTO realized(ts,wallet,token_mint,pnl_base,base_mint,is_win) VALUES(?,?,?,?,?,?)",
                (now_ts(), wallet, token_mint, pnl, base_mint, 1 if pnl>0 else 0)); conn.commit()
    return pnl

def insert_swap(conn, sig, ts, wallet, direction, base_mint, base_amount, token_mint, token_amount):
    try:
        conn.execute("""INSERT INTO swaps(signature,ts,wallet,direction,base_mint,base_amount,token_mint,token_amount)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (sig,ts,wallet,direction,base_mint,base_amount,token_mint,token_amount))
        conn.commit(); return True
    except sqlite3.IntegrityError:
        return False

def parse_swap_from_helius_obj(obj: Dict[str,Any]) -> Optional[Dict[str,Any]]:
    try:
        signature = obj.get("signature") or (obj.get("transaction",{}).get("signatures",[None])[0])
        ts = int(obj.get("timestamp") or obj.get("blockTime") or now_ts())
        events = obj.get("events") or {}
        swap = events.get("swap") or obj.get("swap") or {}
        if not swap and isinstance(events, list):
            for ev in events:
                if isinstance(ev,dict) and ev.get("type","").upper()=="SWAP":
                    swap = ev.get("info",{}) or ev; break
        user_wallet = (swap.get("userAccount") or swap.get("authority") or swap.get("user") or
                       (obj.get("accountData",[{}])[0].get("account") if obj.get("accountData") else None) or
                       obj.get("feePayer") or obj.get("source"))
        token_in_mint  = swap.get("tokenIn")  or swap.get("mintIn") or swap.get("fromMint")
        token_out_mint = swap.get("tokenOut") or swap.get("mintOut") or swap.get("toMint")
        token_in_amt   = safe_float(swap.get("amountIn") or swap.get("tokenInAmount"))
        token_out_amt  = safe_float(swap.get("amountOut") or swap.get("tokenOutAmount"))
        if not (user_wallet and token_in_mint and token_out_mint and (token_in_amt>0 or token_out_amt>0)): return None

        if token_in_mint in QUOTE_MINTS and token_out_mint not in QUOTE_MINTS:
            direction="BUY"; base_mint=token_in_mint; base_amt=token_in_amt; token_mint=token_out_mint; token_amt=token_out_amt
        elif token_out_mint in QUOTE_MINTS and token_in_mint not in QUOTE_MINTS:
            direction="SELL"; base_mint=token_out_mint; base_amt=token_out_amt; token_mint=token_in_mint; token_amt=token_in_amt
        else:
            return None

        if base_amt < MIN_TRADE_BASE: return None

        return {"signature":signature,"ts":ts,"wallet":str(user_wallet),"direction":direction,
                "base_mint":base_mint,"base_amount":float(base_amt),"token_mint":token_mint,"token_amount":float(token_amt)}
    except Exception:
        logging.exception("parse_swap_from_helius_obj failed"); return None

# ====== Flask 路由 ======
@app.get("/health")
def health(): return jsonify({"ok":True,"time":now_ts()})

@app.post("/helius")
def helius_webhook():
    if not verify_signature(request): return ("Forbidden",403)
    payload = request.get_json(force=True, silent=True)
    if payload is None: return jsonify({"ok":False,"msg":"no json"}), 400
    items = payload if isinstance(payload,list) else (payload.get("data") if isinstance(payload,dict) and isinstance(payload.get("data"),list) else [payload])

    conn=db(); new_swaps=0; realized_msgs=[]
    for obj in items:
        data = parse_swap_from_helius_obj(obj)
        if not data: continue
        if not insert_swap(conn, data["signature"], data["ts"], data["wallet"], data["direction"],
                           data["base_mint"], data["base_amount"], data["token_mint"], data["token_amount"]):
            continue
        new_swaps += 1

        if data["direction"]=="BUY":
            upsert_position(conn, data["wallet"], data["token_mint"], data["token_amount"], data["base_amount"])
        else:
            pnl = realize_sell(conn, data["wallet"], data["token_mint"], data["token_amount"], data["base_amount"], data["base_mint"])
            if abs(pnl)>0:
                sign="✅" if pnl>0 else "❌"
                realized_msgs.append(f"{sign} 實現損益 {pnl:.2f} ({'USDC' if data['base_mint']==DEFAULT_QUOTES[0] else 'SOL'})\n"
                                     f"錢包: `{data['wallet']}`\n代幣: {data['token_mint']}\n成交: {data['base_amount']:.2f}\n時間: {human_ts(data['ts'])}")

        # 關注的錢包即時通知
        cur=conn.cursor(); cur.execute("SELECT 1 FROM follows WHERE wallet=?", (data["wallet"],))
        if cur.fetchone():
            emoji="🟢" if data["direction"]=="BUY" else "🔴"
            base_symbol="USDC" if data["base_mint"]==DEFAULT_QUOTES[0] else "SOL"
            send_tg(f"{emoji} {data['direction']} 觸發\n錢包: `{data['wallet']}`\n代幣: {data['token_mint']}\n金額: {data['base_amount']:.2f} {base_symbol}\n時間: {human_ts(data['ts'])}")

    conn.close()
    for m in realized_msgs: send_tg(m)
    return jsonify({"ok":True,"received":len(items),"new_swaps":new_swaps})

# ====== 排行 / 視窗統計 ======
def get_top_wallets(min_trades=3, limit=10):
    c=db(); cur=c.cursor()
    cur.execute("""SELECT wallet,SUM(is_win) wins,COUNT(*) trades,
                          ROUND(100.0*SUM(is_win)/COUNT(*),2) win_rate_pct,
                          ROUND(SUM(pnl_base),2) pnl_sum,
                          COUNT(DISTINCT token_mint) tokens
                   FROM realized GROUP BY wallet
                   HAVING trades>=? ORDER BY win_rate_pct DESC, pnl_sum DESC LIMIT ?""",
                (min_trades,limit))
    rows=cur.fetchall(); c.close(); return rows

def get_window_stats(days):
    since = now_ts() - days*86400
    c=db(); cur=c.cursor()
    cur.execute("""SELECT wallet,SUM(is_win) wins,COUNT(*) trades,
                          ROUND(100.0*SUM(is_win)/COUNT(*),2) win_rate_pct,
                          ROUND(SUM(pnl_base),2) pnl_sum
                   FROM realized WHERE ts>=? GROUP BY wallet
                   ORDER BY win_rate_pct DESC, pnl_sum DESC""", (since,))
    rows=cur.fetchall(); c.close(); return rows

def filter_long_term_winners():
    life={r["wallet"]:r for r in get_top_wallets(1,100000)}
    d30={r["wallet"]:r for r in get_window_stats(30)}
    d90={r["wallet"]:r for r in get_window_stats(90)}
    winners=[]
    for w,lr in life.items():
        if int(lr["trades"])<LIFE_MIN_TRADES or float(lr["win_rate_pct"])<LIFE_MIN_WINRATE or float(lr["pnl_sum"])<LIFE_MIN_PNL:
            continue
        r30=d30.get(w); r90=d90.get(w)
        if not r30 or not r90: continue
        if (int(r30["trades"])>=D30_MIN_TRADES and float(r30["win_rate_pct"])>=D30_MIN_WINRATE and float(r30["pnl_sum"])>=D30_MIN_PNL and
            int(r90["trades"])>=D90_MIN_TRADES and float(r90["win_rate_pct"])>=D90_MIN_WINRATE and float(r90["pnl_sum"])>=D90_MIN_PNL):
            winners.append({"wallet":w,"life":lr,"d30":r30,"d90":r90})
    return winners

def format_winner_msg(w):
    return ("🧭 發現長期贏家\n"
            f"錢包: `{w['wallet']}`\n"
            f"30d 勝率: {w['d30']['win_rate_pct']}% / 回合 {w['d30']['trades']} / 淨PnL {w['d30']['pnl_sum']}\n"
            f"90d 勝率: {w['d90']['win_rate_pct']}% / 回合 {w['d90']['trades']} / 淨PnL {w['d90']['pnl_sum']}\n"
            f"總累積: 勝率 {w['life']['win_rate_pct']}% / 回合 {w['life']['trades']} / 淨PnL {w['life']['pnl_sum']}\n"
            "（用 `/follow <wallet>` 追蹤）")

def discovery_job():
    try:
        c=db(); cur=c.cursor()
        winners = filter_long_term_winners()
        cur.execute("SELECT wallet FROM announced")
        announced={r["wallet"] for r in cur.fetchall()}
        new_winners=[w for w in winners if w["wallet"] not in announced]
        for w in new_winners:
            send_tg(format_winner_msg(w))
            if AUTO_FOLLOW_DISCOVERED:
                try:
                    c.execute("INSERT OR REPLACE INTO follows(wallet,ts) VALUES(?,?)",(w["wallet"],now_ts())); c.commit()
                    send_tg(f"✅ 已自動追蹤 `{w['wallet']}`")
                except: logging.exception("auto-follow failed")
            try:
                c.execute("INSERT OR REPLACE INTO announced(wallet,ts) VALUES(?,?)",(w["wallet"],now_ts())); c.commit()
            except: logging.exception("announce mark failed")
        c.close()
    except: logging.exception("discovery_job failed")

def periodic_top_push():
    try:
        rows=get_top_wallets(3,10)
        if not rows: return
        lines=["🏆 高勝率錢包排行榜（自動推送）"]
        for i,r in enumerate(rows,1):
            lines.append(f"{i}. `{r['wallet']}` | 勝率 {r['win_rate_pct']}% ({r['wins']}/{r['trades']}) | 累計PnL {r['pnl_sum']}")
        send_tg("\n".join(lines))
    except: logging.exception("periodic_top_push failed")

# ====== Telegram 指令 ======
async def cmd_ping(u:Update,c:ContextTypes.DEFAULT_TYPE): await u.message.reply_text("pong")

async def cmd_top(u:Update,c:ContextTypes.DEFAULT_TYPE):
    try:
        min_trades=int(c.args[0]) if len(c.args)>=1 else 3
        limit=int(c.args[1]) if len(c.args)>=2 else 10
    except: min_trades,limit=3,10
    rows=get_top_wallets(min_trades,limit)
    if not rows: return await u.message.reply_text("目前沒有達標的錢包紀錄。")
    lines=["🏆 高勝率錢包排行榜"]
    for i,r in enumerate(rows,1):
        lines.append(f"{i}. `{r['wallet']}` | 勝率 {r['win_rate_pct']}% ({r['wins']}/{r['trades']}) | 累計PnL {r['pnl_sum']} | 幣種 {r['tokens']}")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_stats(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not c.args: return await u.message.reply_text("用法: /stats <wallet>")
    wallet=c.args[0].strip()
    cdb=db(); cur=cdb.cursor()
    cur.execute("""SELECT SUM(is_win) wins, COUNT(*) trades, ROUND(100.0*SUM(is_win)/COUNT(*),2) win_rate_pct,
                          ROUND(SUM(pnl_base),2) pnl_sum FROM realized WHERE wallet=?""",(wallet,))
    summary=cur.fetchone()
    cur.execute("""SELECT token_mint, SUM(CASE WHEN pnl_base>0 THEN 1 ELSE 0 END) wins,
                          COUNT(*) trades, ROUND(SUM(pnl_base),2) pnl_sum
                   FROM realized WHERE wallet=? GROUP BY token_mint ORDER BY pnl_sum DESC LIMIT 10""",(wallet,))
    per_token=cur.fetchall(); cdb.close()
    if not summary or summary["trades"] is None: return await u.message.reply_text("查無此錢包的實現紀錄。")
    lines=[f"📊 錢包 `{wallet}`", f"勝率: {summary['win_rate_pct']}% ({summary['wins']}/{summary['trades']})", f"累計PnL: {summary['pnl_sum']}", "前10代幣："]
    for r in per_token: lines.append(f"- {r['token_mint']} | 勝 {r['wins']}/{r['trades']} | PnL {r['pnl_sum']}")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_follow(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not c.args: return await u.message.reply_text("用法: /follow <wallet>")
    wallet=c.args[0].strip(); cdb=db()
    try:
        cdb.execute("INSERT OR REPLACE INTO follows(wallet,ts) VALUES(?,?)",(wallet,now_ts())); cdb.commit()
        await u.message.reply_text(f"✅ 已關注 `{wallet}`", parse_mode="Markdown")
    finally: cdb.close()

async def cmd_unfollow(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not c.args: return await u.message.reply_text("用法: /unfollow <wallet>")
    wallet=c.args[0].strip(); cdb=db()
    try:
        cdb.execute("DELETE FROM follows WHERE wallet=?",(wallet,)); cdb.commit()
        await u.message.reply_text(f"✅ 已取消關注 `{wallet}`", parse_mode="Markdown")
    finally: cdb.close()

async def cmd_discover(u:Update,c:ContextTypes.DEFAULT_TYPE):
    discovery_job(); await u.message.reply_text("已執行探索任務。")

async def cmd_toplong(u:Update,c:ContextTypes.DEFAULT_TYPE):
    try:
        days=int(c.args[0]) if len(c.args)>=1 else 90
        min_trades=int(c.args[1]) if len(c.args)>=2 else 10
        min_win=float(c.args[2]) if len(c.args)>=3 else 58.0
        min_pnl=float(c.args[3]) if len(c.args)>=4 else 300.0
    except: days,min_trades,min_win,min_pnl=90,10,58.0,300.0
    rows=[r for r in get_window_stats(days) if int(r["trades"])>=min_trades and float(r["win_rate_pct"])>=min_win and float(r["pnl_sum"])>=min_pnl]
    if not rows: return await u.message.reply_text("該條件下查無結果。")
    lines=[f"📅 {days} 天長期排行 (minTrades={min_trades}, minWin={min_win}%, minPnL={min_pnl})"]
    for i,r in enumerate(rows[:20],1):
        lines.append(f"{i}. `{r['wallet']}` | 勝率 {r['win_rate_pct']}% | 回合 {r['trades']} | 淨PnL {r['pnl_sum']}")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

# ====== Scheduler / Flask Thread / Telegram Main ======
def start_scheduler():
    sched=BackgroundScheduler(timezone="UTC")
    sched.add_job(periodic_top_push, "cron", minute="*/30")
    if DISCOVERY_ENABLE:
        sched.add_job(discovery_job, "interval", minutes=DISCOVERY_INTERVAL_MIN, next_run_time=None)
    sched.start()

def run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

if __name__ == "__main__":
    start_scheduler()
    if TELEGRAM_TOKEN:
        # Flask 放子執行緒；Telegram 留在主執行緒避免 set_wakeup_fd 錯誤
        threading.Thread(target=run_flask, daemon=True).start()
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        application.add_handler(CommandHandler("ping", cmd_ping))
        application.add_handler(CommandHandler("top", cmd_top))
        application.add_handler(CommandHandler("stats", cmd_stats))
        application.add_handler(CommandHandler("follow", cmd_follow))
        application.add_handler(CommandHandler("unfollow", cmd_unfollow))
        application.add_handler(CommandHandler("discover", cmd_discover))
        application.add_handler(CommandHandler("toplong", cmd_toplong))
        application.run_polling(drop_pending_updates=True)
    else:
        # 沒設 Telegram Token：只跑 Flask（仍可接收 Helius）
        run_flask()
