import os, asyncio, json, time, threading, re, random
import requests, httpx, websockets
from collections import deque
from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()

# ========= Env =========
RPC_HTTP_URL = os.getenv("RPC_HTTP_URL", "https://api.mainnet-beta.solana.com")
RPC_WS_URL   = os.getenv("RPC_WS_URL", "wss://api.mainnet-beta.solana.com")
WS_COMMITMENT = os.getenv("WS_COMMITMENT", "confirmed")  # 建議 confirmed 比較穩

PROGRAM_IDS = [p.strip() for p in os.getenv("PROGRAM_IDS","").split(",") if p.strip()]

TG_TOKEN = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID","")

HELIUS_WEBHOOK_ENABLED = os.getenv("HELIUS_WEBHOOK_ENABLED","0") == "1"

# 可調參數
TX_FETCH_RETRIES   = int(os.getenv("TX_FETCH_RETRIES", "8"))       # 最多重試次數
TX_FETCH_DELAY_MS  = int(os.getenv("TX_FETCH_DELAY_MS", "200"))    # 每次重試延遲（毫秒，會遞增）
WS_CONNECT_OFFSET  = int(os.getenv("WS_CONNECT_OFFSET", "0"))      # 啟動前錯峰延遲（秒）

# ========= Labels (可選) =========
PROGRAM_LABELS = {
  "CPMMoo8L3F4NbTegBCKVNunggL7H2pdTHKxQB5qKP1C": "Raydium CPMM",
  "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4 (Legacy)",
  "CAMMCzo5YLb4W4fF8KvHnrgWqKqVhmbLicVido4qvUFM5KAg8cTNwpY2Gff3uctyCc": "Raydium CLMM",
  "whirLbMiicVdio4qvUFM5KAg8cTNwpY2Gff3uctyCc": "Orca Whirlpool",
}

# ========= State =========
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

# ========= RPC utils =========
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
                print(f"[RPC] getTransaction error for {sig}:", j.get("error"))
            return j.get("result")
    except Exception as e:
        print("[RPC] getTransaction 失敗:", e)
        return None

async def rpc_http_get_signature_status(sig: str) -> str | None:
    payload = {"jsonrpc":"2.0","id":1,"method":"getSignatureStatuses","params":[[sig], {"searchTransactionHistory": True}]}
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.post(RPC_HTTP_URL, json=payload)
            j = r.json().get("result", {})
            arr = (j.get("value") or [])
            if arr and arr[0]:
                return (arr[0].get("confirmationStatus") or "").lower()
            return None
    except Exception as e:
        print("[RPC] getSignatureStatuses 失敗:", e)
        return None

async def rpc_http_get_transaction(sig: str) -> dict | None:
    """
    多次重試 + 漸進延遲；若狀態仍是 processed 就再等一下
    """
    delay = TX_FETCH_DELAY_MS / 1000.0
    for i in range(TX_FETCH_RETRIES):
        tx = await rpc_http_get_transaction_once(sig)
        if tx:
            return tx
        # 看一下目前確認狀態（非必要，但可幫助診斷）
        if i == 0 or i % 2 == 1:
            st = await rpc_http_get_signature_status(sig)
            if st:
                print(f"[VALIDATE] 交易 {sig} 狀態：{st}（第 {i+1} 次嘗試）")
        await asyncio.sleep(delay)
        delay *= 1.5  # 指數退避
    return None

# ========= 判斷邏輯 =========
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
    outer = msg.get("instructions") or []
    for ins in outer:
        res.append(ins)
    meta = tx.get("meta") or {}
    inner = meta.get("innerInstructions") or []
    for grp in inner:
        for ins in grp.get("instructions",[]):
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
            hit_prog, hit_type = pid, "NEW_POOL"
            break
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
                        hit_prog = k
                        break
    return hit_type, {"programId": hit_prog}

def format_sig_link(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"

def program_label(pid: str|None) -> str:
    return PROGRAM_LABELS.get(pid or "", pid or "Unknown Program")

# ========= WS 預篩（減少無效 HTTP 查詢）=========
def logs_hint_is_candidate(logs: list[str]) -> bool:
    s = " ".join((logs or [])).lower()
    if not s:
        return False
    if _match_type_like(s, INIT_KEYS) or _match_type_like(s, ADDLP_KEYS):
        return True
    # 某些 DEX 會有較隱晦的字；需要時再加
    return False

# ========= WebSocket 訂閱 =========
async def ws_consume():
    if not PROGRAM_IDS:
        raise RuntimeError("PROGRAM_IDS 不可為空")
    focus = set(PROGRAM_IDS)

    if WS_CONNECT_OFFSET > 0:
        await asyncio.sleep(WS_CONNECT_OFFSET)

    backoff = 5
    backoff_max = 120

    def jitter(sec: int) -> float:
        return max(1.0, sec + random.uniform(-sec*0.2, sec*0.2))

    while True:
        try:
            print("[WS] connecting to:", (RPC_WS_URL.split("?")[0] if "?" in RPC_WS_URL else RPC_WS_URL))
            async with websockets.connect(
                RPC_WS_URL,
                ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=2000
            ) as ws:
                backoff = 5

                # 訂閱
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
                    val = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
                    sig  = val.get("signature")
                    logs = val.get("logs") or []

                    # 先做 logs 預篩，減少無效 HTTP 開銷
                    if not sig or sig in SEEN_SET or not logs_hint_is_candidate(logs):
                        continue

                    SEEN_SET.add(sig); SEEN_SIGS.append(sig)
                    asyncio.create_task(_post_validate_and_notify(sig, focus))

        except websockets.exceptions.InvalidStatusCode as e:
            code = getattr(e, "status_code", None)
            wait = jitter(backoff)
            print(f"[WS] 連線被拒 (HTTP {code})，{wait:.1f}s 後重連")
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, backoff_max)

        except Exception as e:
            wait = jitter(backoff)
            print(f"[WS] 連線中斷：{e}，{wait:.1f}s 後重連")
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, backoff_max)

async def _post_validate_and_notify(sig: str, focus: set[str]):
    try:
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
    # 如不想啟動 WS，可把 RPC_WS_URL 設為空字串 ""，改成僅 webhook 模式
    if RPC_WS_URL.strip():
        t = threading.Thread(target=start_async_loop, daemon=True)
        t.start()
    run_flask()
