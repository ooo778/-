import os, asyncio, json, time, threading, re
import requests, httpx, websockets
from collections import deque
from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()

RPC_HTTP_URL = os.getenv("RPC_HTTP_URL", "https://api.mainnet-beta.solana.com")
RPC_WS_URL   = os.getenv("RPC_WS_URL", "wss://api.mainnet-beta.solana.com")
WS_COMMITMENT = os.getenv("WS_COMMITMENT", "processed")

PROGRAM_IDS = [p.strip() for p in os.getenv("PROGRAM_IDS","").split(",") if p.strip()]

TG_TOKEN = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID","")

HELIUS_WEBHOOK_ENABLED = os.getenv("HELIUS_WEBHOOK_ENABLED","0") == "1"

# 可擴充的 Program 標籤（顯示用）
PROGRAM_LABELS = {
  "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKoB5qKP1C": "Raydium CPMM",
  "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4 (Legacy)",
  "CAMMCzo5YLb4W4fF8KvHnrgWqKqVhmbLicVido4qvUFM5KAg8cTNwpY2Gff3uctyCc": "Raydium CLMM",
  "whirLbMiicVdio4qvUFM5KAg8cTNwpY2Gff3uctyCc": "Orca Whirlpool",
}

# 去重 + 記憶最近通知，避免洗頻
SEEN_SIGS = deque(maxlen=20000)
SEEN_SET  = set()

def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[TG] 未設定，略過訊息：", text[:120])
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        resp = requests.post(url, data={"chat_id":TG_CHAT, "text":text, "parse_mode":"HTML"}, timeout=6)
        if resp.status_code != 200:
            print("[TG] 送出失敗:", resp.status_code, resp.text)
    except Exception as e:
        print("[TG] 例外:", e)

# --- RPC utils ---
async def rpc_http_get_transaction(sig: str) -> dict | None:
    payload = {
        "jsonrpc":"2.0","id":1,"method":"getTransaction",
        "params":[sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]
    }
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.post(RPC_HTTP_URL, json=payload)
            j = r.json()
            return j.get("result")
    except Exception as e:
        print("[RPC] getTransaction 失敗:", e)
        return None

def extract_program_instructions(tx: dict):
    if not tx: 
        return []
    res = []
    msg = (tx.get("transaction") or {}).get("message") or {}
    outer = msg.get("instructions") or []
    for ins in outer:
        res.append(ins)
    meta = tx.get("meta") or {}
    inner = meta.get("innerInstructions") or []
    for grp in inner:
        for ins in grp.get("instructions",[]):
            res.append(ins)
    return res

# --- 判斷邏輯 ---
INIT_KEYS = {"initialize","initialize2","initialize_pool","init_pool","create_pool","open_position","initialize_tick_array","initialize_config"}
ADDLP_KEYS = {"add_liquidity","addliquidity","deposit","deposit_liquidity","increase_liquidity"}

def _match_type_like(s: str, keys: set[str]) -> bool:
    s = s.lower()
    return any(k in s for k in keys)

def classify_event_by_tx(tx: dict, focus_programs: set[str]) -> tuple[str|None, dict]:
    if not tx:
        return None, {}
    ins_list = extract_program_instructions(tx)
    hit_prog = None
    hit_type = None
    for ins in ins_list:
        pid = ins.get("programId")
        if not pid or pid not in focus_programs:
            continue
        parsed = ins.get("parsed") or {}
        t = (parsed.get("type") or parsed.get("instruction")) or ""
        if _match_type_like(str(t), INIT_KEYS):
            hit_prog, hit_type = pid, "NEW_POOL"
            break
        if _match_type_like(str(t), ADDLP_KEYS):
            hit_prog, hit_type = pid, "ADD_LIQUIDITY"
    if hit_type is None:
        meta = tx.get("meta") or {}
        logs = " ".join((meta.get("logMessages") or [])).lower()
        if any(pid in (tx.get("transaction") or {}).get("message",{}).get("accountKeys",[]) for pid in focus_programs):
            if _match_type_like(logs, INIT_KEYS): 
                hit_type = "NEW_POOL"
            elif _match_type_like(logs, ADDLP_KEYS):
                hit_type = "ADD_LIQUIDITY"
            if hit_type:
                for k in focus_programs:
                    if k in logs:
                        hit_prog = k
                        break
    return hit_type, {"programId": hit_prog}

def format_sig_link(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"

def program_label(pid: str|None) -> str:
    return PROGRAM_LABELS.get(pid or "", pid or "Unknown Program")

# --- WebSocket 訂閱 ---
async def ws_consume():
    assert PROGRAM_IDS, "PROGRAM_IDS 不可為空"
    focus = set(PROGRAM_IDS)
    while True:
        try:
            async with websockets.connect(RPC_WS_URL, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=2000) as ws:
                for idx, pid in enumerate(PROGRAM_IDS, start=1):
                    sub_req = {
                        "jsonrpc":"2.0","id":idx,"method":"logsSubscribe",
                        "params":[{"mentions":[pid]}, {"commitment":WS_COMMITMENT}]
                    }
                    await ws.send(json.dumps(sub_req))
                print("[WS] Subscribed to", PROGRAM_IDS)

                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if msg.get("method") != "logsNotification":
                        continue
                    params = msg.get("params") or {}
                    result = params.get("result") or {}
                    value = result.get("value") or {}
                    sig   = value.get("signature")

                    if not sig or sig in SEEN_SET:
                        continue

                    SEEN_SET.add(sig); SEEN_SIGS.append(sig)
                    asyncio.create_task(_post_validate_and_notify(sig, focus))
        except Exception as e:
            print("[WS] 連線中斷：", e, "5 秒後重連")
            await asyncio.sleep(5)

async def _post_validate_and_notify(sig: str, focus: set[str]):
    try:
        tx = await rpc_http_get_transaction(sig)
        if not tx:
            print(f"[VALIDATE] 交易 {sig} 沒有拿到資料，略過")
            return

        ev_type, details = classify_event_by_tx(tx, focus)
        if not ev_type:
            return  

        pid = details.get("programId")
        label = program_label(pid)
        head = "🆕 新池建立" if ev_type=="NEW_POOL" else "➕ 加入流動性"
        text = (
            f"{head}  <b>{label}</b>\n"
            f"Sig: <code>{sig}</code>\n"
            f"{format_sig_link(sig)}\n"
            f"(來源: WS {WS_COMMITMENT}，已用 HTTP 驗證)"
        )
        tg_send(text)

    except Exception as e:
        print("[POST-VALIDATE] 解析失敗:", sig, e)

# --- Flask（健康檢查 + Helius Webhook 入口）---
app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/helius")
def helius_hook():
    if not HELIUS_WEBHOOK_ENABLED:
        return jsonify({"ok": False, "reason":"webhook disabled"}), 403
    try:
        data = request.get_json(force=True, silent=True) or {}
        events = data if isinstance(data, list) else [data]
        handled = 0
        for ev in events:
            sig = ev.get("signature") or ev.get("transaction","")
            if not sig or sig in SEEN_SET:
                continue
            SEEN_SET.add(sig); SEEN_SIGS.append(sig)
            asyncio.run_coroutine_threadsafe(_post_validate_and_notify(sig, set(PROGRAM_IDS)), loop)
            handled += 1
        return jsonify({"ok": True, "handled":handled}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)

loop = asyncio.new_event_loop()
def start_async_loop():
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_consume())

if __name__ == "__main__":
    t = threading.Thread(target=start_async_loop, daemon=True)
    t.start()
    run_flask()
