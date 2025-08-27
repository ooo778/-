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
  "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
  "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4 (Legacy)",
  "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
  "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
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
    # 使用 jsonParsed，提高穩定可讀性
    payload = {
        "jsonrpc":"2.0","id":1,"method":"getTransaction",
        "params":[sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]
    }
    async with httpx.AsyncClient(timeout=6) as client:
        r = await client.post(RPC_HTTP_URL, json=payload)
        j = r.json()
        return j.get("result")

def extract_program_instructions(tx: dict):
    """回傳所有(外層+內層) 指令 (programId, type/parsed, raw)"""
    res = []
    if not tx: 
        return res
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

# --- 判斷邏輯（盡量靠 "parsed.type" 與 programId，而非只看文字）---
INIT_KEYS = {"initialize","initialize2","initialize_pool","init_pool","create_pool","open_position","initialize_tick_array","initialize_config"}
ADDLP_KEYS = {"add_liquidity","addliquidity","deposit","deposit_liquidity","increase_liquidity"}

def _match_type_like(s: str, keys: set[str]) -> bool:
    s = s.lower()
    return any(k in s for k in keys)

def classify_event_by_tx(tx: dict, focus_programs: set[str]) -> tuple[str|None, dict]:
    """
    回傳: (event_type, details)
    event_type in {"NEW_POOL","ADD_LIQUIDITY", None}
    """
    ins_list = extract_program_instructions(tx)
    hit_prog = None
    hit_type = None
    for ins in ins_list:
        # 兩種常見格式： {"programId":"xxx", "parsed":{"type":"initializePool",...}} 或 {"programId":"xxx", "program":"spl-token", ...}
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
            # 繼續掃描看看是否同筆 tx 同時有 initialize 類型，若有以 NEW_POOL 優先
    if hit_type is None:
        # 退而求其次：有時 parsed 不帶 type；檢查 raw logs
        meta = tx.get("meta") or {}
        logs = " ".join((meta.get("logMessages") or [])).lower()
        if any(pid in (tx.get("transaction") or {}).get("message",{}).get("accountKeys",[]) for pid in focus_programs):
            if _match_type_like(logs, INIT_KEYS): 
                hit_type = "NEW_POOL"
            elif _match_type_like(logs, ADDLP_KEYS):
                hit_type = "ADD_LIQUIDITY"
            if hit_type:
                # 猜一個「最可能」的 programId（第一個命中的）
                for k in focus_programs:
                    if k in logs:
                        hit_prog = k
                        break

    return hit_type, {"programId": hit_prog}

def format_sig_link(sig: str) -> str:
    # 放在 TG 訊息裡給你點；（放原始 URL 在訊息屬於程式內容 OK）
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
                subs = []
                # 逐一訂閱，不用 allWithVotes，直接 mentions: [programId]
                for idx, pid in enumerate(PROGRAM_IDS, start=1):
                    sub_req = {
                        "jsonrpc":"2.0","id":idx,"method":"logsSubscribe",
                        "params":[{"mentions":[pid]}, {"commitment":WS_COMMITMENT}]
                    }
                    await ws.send(json.dumps(sub_req))
                    # 不強制等待回覆，因為有些節點延遲回 ack；我們照收通知即可
                print("[WS] Subscribed to", PROGRAM_IDS)

                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    # 專收 logsNotification
                    if msg.get("method") != "logsNotification":
                        continue
                    params = msg.get("params") or {}
                    result = params.get("result") or {}
                    value = result.get("value") or {}
                    sig   = value.get("signature")
                    logs  = value.get("logs") or []

                    if not sig or sig in SEEN_SET:
                        continue

                    # 先快速標註（低延遲），再用 HTTP 取回 tx 進行「穩定判斷」
                    SEEN_SET.add(sig); SEEN_SIGS.append(sig)

                    # 立即觸發 HTTP 解析驗證（不阻塞 WS），並在驗證通過才通知
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
        # Helius 會把多筆 tx 一次推送；逐筆處理
        events = data if isinstance(data, list) else [data]
        handled = 0
        for ev in events:
            # 標準化欄位：enhanced webhook 通常含有 "type"/"events"/"signature"
            sig = ev.get("signature") or ev.get("transaction","")
            if not sig or sig in SEEN_SET:
                continue
            # 立即進入相同的後驗流程
            SEEN_SET.add(sig); SEEN_SIGS.append(sig)
            asyncio.run_coroutine_threadsafe(_post_validate_and_notify(sig, set(PROGRAM_IDS)), loop)
            handled += 1
        return jsonify({"ok": True, "handled":handled}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)

# 啟動
loop = asyncio.new_event_loop()
def start_async_loop():
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_consume())

if __name__ == "__main__":
    # 啟動 WS 消費者（背景執行）
    t = threading.Thread(target=start_async_loop, daemon=True)
    t.start()
    # 啟動 Flask（前景執行）
    run_flask()
