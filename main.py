import os, asyncio, json, time, threading, random
import requests, httpx, websockets
from collections import deque
from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()

# ========= Env =========
RPC_HTTP_URL = os.getenv("RPC_HTTP_URL", "https://api.mainnet-beta.solana.com")
RPC_WS_URL   = os.getenv("RPC_WS_URL",   "wss://api.mainnet-beta.solana.com")
WS_COMMITMENT = os.getenv("WS_COMMITMENT", "confirmed")  # processed 更快、confirmed 較穩

PROGRAM_IDS = [p.strip() for p in os.getenv("PROGRAM_IDS","").split(",") if p.strip()]

TG_TOKEN = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID","")

# Webhook
HELIUS_WEBHOOK_ENABLED = os.getenv("HELIUS_WEBHOOK_ENABLED","0") == "1"
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY","")

# 可調參數
TX_FETCH_RETRIES   = int(os.getenv("TX_FETCH_RETRIES", "6"))       # getTransaction 重試次數
TX_FETCH_DELAY_MS  = int(os.getenv("TX_FETCH_DELAY_MS", "150"))    # 重試延遲(毫秒)，每次x1.5
HTTP_CONCURRENCY   = int(os.getenv("HTTP_CONCURRENCY", "2"))       # 解析交易同時併發數
WS_CONNECT_OFFSET  = int(os.getenv("WS_CONNECT_OFFSET", "0"))      # 啟動錯峰(秒)

# WS 自動回退（Helius 429 時改用公網 WS）
WS_PUBLIC_FALLBACK = os.getenv("WS_PUBLIC_FALLBACK","1") == "1"
WS_FALLBACK_URL = os.getenv("WS_FALLBACK_URL","wss://api.mainnet-beta.solana.com")
WS_FALLBACK_COOLDOWN_SEC = int(os.getenv("WS_FALLBACK_COOLDOWN_SEC","600"))  # 回退持續秒數

HTTP_SEM = asyncio.Semaphore(max(1, HTTP_CONCURRENCY))

# ========= Label（可自行擴充/修正）=========
PROGRAM_LABELS = {
  "CPMMoo8L3F4NbTegBCKVNunggL7H2pdTHKxQB5qKP1C": "Raydium CPMM",
  "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4 (Legacy)",
  "CAMMCzo5YLb4W4fF8KvHnrgWqKqVhmbLicVido4qvUFM5KAg8cTNwpY2Gff3uctyCc": "Raydium CLMM",
  "whirLbMiicVdio4qvUFM5KAg8cTNwpY2Gff3uctyCc": "Orca Whirlpool",
}

# ========= State =========
SEEN_SIGS = deque(maxlen=20000)
SEEN_SET  = set()

# ========= Helpers =========
def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[TG] 未設定，略過：", text[:120])
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        resp = requests.post(url, data={"chat_id":TG_CHAT, "text":text, "parse_mode":"HTML"}, timeout=8)
        if resp.status_code != 200:
            print("[TG] 送出失敗:", resp.status_code, resp.text)
    except Exception as e:
        print("[TG] 例外:", e)

def program_label(pid: str|None) -> str:
    return PROGRAM_LABELS.get(pid or "", pid or "Unknown Program")

def format_sig_link(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"

# ========= RPC =========
async def rpc_http_get_transaction_once(sig: str) -> dict | None:
    payload = {
        "jsonrpc":"2.0","id":1,"method":"getTransaction",
        "params":[sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(RPC_HTTP_URL, json=payload)
            j = r.json()
            if "error" in j:
                print(f"[RPC] getTransaction error for {sig}: {j.get('error')}")
            return j.get("result")
    except Exception as e:
        print("[RPC] getTransaction 失敗:", e)
        return None

async def rpc_http_get_signature_status(sig: str) -> str | None:
    payload = {"jsonrpc":"2.0","id":1,"method":"getSignatureStatuses","params":[[sig], {"searchTransactionHistory": True}]}
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.post(RPC_HTTP_URL, json=payload)
            arr = (r.json().get("result", {}).get("value") or [])
            if arr and arr[0]:
                return (arr[0].get("confirmationStatus") or "").lower()
    except Exception as e:
        print("[RPC] getSignatureStatuses 失敗:", e)
    return None

async def rpc_http_get_transaction(sig: str) -> dict | None:
    """多次重試 + 指數退避；穿插查狀態以利診斷"""
    delay = TX_FETCH_DELAY_MS / 1000.0
    for i in range(TX_FETCH_RETRIES):
        tx = await rpc_http_get_transaction_once(sig)
        if tx:
            return tx
        if i in (0, 2, 4):  # 偶爾印一下狀態
            st = await rpc_http_get_signature_status(sig)
            if st:
                print(f"[VALIDATE] 交易 {sig} 狀態：{st}（第 {i+1} 次嘗試）")
        await asyncio.sleep(delay)
        delay *= 1.5
    return None

# ========= 解析邏輯 =========
INIT_KEYS  = {"initialize","initialize2","initialize_pool","init_pool","create_pool","open_position","initialize_tick_array","initialize_config"}
ADDLP_KEYS = {"add_liquidity","addliquidity","deposit","deposit_liquidity","increase_liquidity"}

def _match_type_like(s: str, keys: set[str]) -> bool:
    s = (s or "").lower()
    return any(k in s for k in keys)

def extract_program_instructions(tx: dict):
    if not tx:
        return []
    res = []
    msg = (tx.get("transaction") or {}).get("message") or {}
    for ins in (msg.get("instructions") or []):
        res.append(ins)
    meta = tx.get("meta") or {}
    for grp in (meta.get("innerInstructions") or []):
        for ins in grp.get("instructions", []):
            res.append(ins)
    return res

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
        if _match_type_like(t, INIT_KEYS):
            hit_prog, hit_type = pid, "NEW_POOL"; break
        if _match_type_like(t, ADDLP_KEYS):
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
                        hit_prog = k; break
    return hit_type, {"programId": hit_prog}

# ========= WS 預篩（先看 log 再決定要不要查 HTTP）=========
def logs_hint_is_candidate(logs: list[str]) -> bool:
    s = " ".join((logs or [])).lower()
    if not s:
        return False
    if _match_type_like(s, INIT_KEYS) or _match_type_like(s, ADDLP_KEYS):
        return True
    return False

# ========= WebSocket（含自動回退）=========
async def ws_consume():
    if not PROGRAM_IDS:
        raise RuntimeError("PROGRAM_IDS 不可為空")
    focus = set(PROGRAM_IDS)

    if WS_CONNECT_OFFSET > 0:
        await asyncio.sleep(WS_CONNECT_OFFSET)

    backoff, backoff_max = 5, 120
    fallback_until = 0

    def show_url(u: str) -> str:
        return (u.split("?")[0] if "?" in u else u)

    while True:
        # 回退期間改用公網 WS；過了再嘗試回 Helius
        use_url = WS_FALLBACK_URL if (WS_PUBLIC_FALLBACK and time.time() < fallback_until) else RPC_WS_URL

        try:
            print("[WS] connecting to:", show_url(use_url))
            async with websockets.connect(
                use_url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=2000
            ) as ws:
                backoff = 5
                # 訂閱 target programs
                for idx, pid in enumerate(PROGRAM_IDS, start=1):
                    await ws.send(json.dumps({
                        "jsonrpc":"2.0","id":idx,"method":"logsSubscribe",
                        "params":[{"mentions":[pid]}, {"commitment":WS_COMMITMENT}]
                    }))
                print("[WS] Subscribed to", PROGRAM_IDS)

                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if msg.get("method") != "logsNotification":
                        continue
                    val = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
                    sig  = val.get("signature")
                    logs = val.get("logs") or []

                    # 預篩 + 去重
                    if not sig or sig in SEEN_SET or not logs_hint_is_candidate(logs):
                        continue
                    SEEN_SET.add(sig); SEEN_SIGS.append(sig)
                    asyncio.create_task(_post_validate_and_notify(sig, focus))

        except websockets.exceptions.InvalidStatusCode as e:
            code = getattr(e, "status_code", None)
            # Helius 429 → 啟動回退
            if code == 429 and WS_PUBLIC_FALLBACK and "helius" in use_url:
                fallback_until = time.time() + WS_FALLBACK_COOLDOWN_SEC
                print(f"[WS] 429，啟用回退：改用 {show_url(WS_FALLBACK_URL)}，{WS_FALLBACK_COOLDOWN_SEC}s 後嘗試恢復 Helius")
            wait = max(1.0, backoff + random.uniform(-0.2*backoff, 0.2*backoff))
            print(f"[WS] 連線被拒 (HTTP {code})，{wait:.1f}s 後重連")
            await asyncio.sleep(wait); backoff = min(backoff * 2, backoff_max)

        except Exception as e:
            wait = max(1.0, backoff + random.uniform(-0.2*backoff, 0.2*backoff))
            print(f"[WS] 連線中斷：{e}，{wait:.1f}s 後重連")
            await asyncio.sleep(wait); backoff = min(backoff * 2, backoff_max)

async def _post_validate_and_notify(sig: str, focus: set[str]):
    try:
        # 併發限制，避免打爆 RPC
        async with HTTP_SEM:
            tx = await rpc_http_get_transaction(sig)
        if not tx:
            print(f"[VALIDATE] 交易 {sig} 沒有拿到資料；略過")
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
            f"(來源: WS {WS_COMMITMENT}；HTTP 驗證完成)"
        )
        tg_send(text)

    except Exception as e:
        print("[POST-VALIDATE] 解析失敗:", sig, e)

# ========= Flask（健康檢查 + Helius Webhook）=========
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

# ========= Start =========
loop = asyncio.new_event_loop()
def start_async_loop():
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_consume())

if __name__ == "__main__":
    # 若不想啟動 WS（Webhook-only），把 RPC_WS_URL 設成空字串即可
    if RPC_WS_URL.strip():
        t = threading.Thread(target=start_async_loop, daemon=True)
        t.start()
    run_flask()
