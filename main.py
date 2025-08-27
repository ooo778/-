import os, asyncio, json, time, threading, random
import requests, httpx, websockets
from collections import deque
from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()

# ========= 基本設定 =========
RPC_HTTP_URL = os.getenv("RPC_HTTP_URL", "https://api.mainnet-beta.solana.com")
RPC_WS_URL   = os.getenv("RPC_WS_URL",   "wss://api.mainnet-beta.solana.com")
WS_COMMITMENT = os.getenv("WS_COMMITMENT", "processed")  # processed 更快 / confirmed 較穩

# 監控的 DEX Program（逗號分隔）
PROGRAM_IDS = [p.strip() for p in os.getenv("PROGRAM_IDS","").split(",") if p.strip()]

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID","")

# Webhook（可選）
HELIUS_WEBHOOK_ENABLED = os.getenv("HELIUS_WEBHOOK_ENABLED","0") == "1"
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY","")

# ========= 行為調參 =========
TX_FETCH_RETRIES   = int(os.getenv("TX_FETCH_RETRIES", "6"))     # getTransaction 重試
TX_FETCH_DELAY_MS  = int(os.getenv("TX_FETCH_DELAY_MS", "150"))  # 每次延遲(毫秒)，每輪×1.5
HTTP_CONCURRENCY   = int(os.getenv("HTTP_CONCURRENCY", "2"))     # 同時解析上限
WS_CONNECT_OFFSET  = int(os.getenv("WS_CONNECT_OFFSET", "0"))    # 啟動錯峰(秒)

# WS/HTTP 自動回退
WS_PUBLIC_FALLBACK = os.getenv("WS_PUBLIC_FALLBACK","1") == "1"
WS_FALLBACK_URL = os.getenv("WS_FALLBACK_URL","wss://api.mainnet-beta.solana.com")
WS_FALLBACK_COOLDOWN_SEC = int(os.getenv("WS_FALLBACK_COOLDOWN_SEC","600"))

HTTP_PUBLIC_FALLBACK = os.getenv("HTTP_PUBLIC_FALLBACK","1") == "1"
HTTP_FALLBACK_URL = os.getenv("HTTP_FALLBACK_URL","https://api.mainnet-beta.solana.com")
HTTP_FALLBACK_COOLDOWN_SEC = int(os.getenv("HTTP_FALLBACK_COOLDOWN_SEC","600"))
_http_fallback_until = 0

# ========= 預警 / 一鍵下單 =========
PRELIM_ALERT  = os.getenv("PRELIM_ALERT", "1") == "1"     # 先發預警
PRELIM_STRICT = os.getenv("PRELIM_STRICT","1") == "1"     # 嚴格預警（避免洗頻）
PRELIM_LINKS  = os.getenv("PRELIM_LINKS","1") == "1"      # 預警後快速補一鍵連結
FAST_TX_TIMEOUT_MS = int(os.getenv("FAST_TX_TIMEOUT_MS","800"))

SHOW_LATENCY = os.getenv("SHOW_LATENCY", "1") == "1"      # 正式訊息顯示延遲(ms)

# 一鍵下單（基礎幣、金額、slippage）
JUP_BASE = os.getenv("JUP_BASE", "So11111111111111111111111111111111111111112")  # wSOL
QUOTED_BASES = os.getenv(
    "QUOTED_BASES",
    "So11111111111111111111111111111111111111112,EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1"
).split(",")

JUP_AMOUNT = os.getenv("JUP_AMOUNT", "0.25")          # 買入金額（單位=JUP_BASE）
JUP_SLIPPAGE_BPS = os.getenv("JUP_SLIPPAGE_BPS", "300")  # 300=3%
JUP_URL_BASE = os.getenv("JUP_URL_BASE","https://jup.ag/swap")
RAY_URL_BASE = os.getenv("RAY_URL_BASE","https://raydium.io/swap/")

# 一鍵賣出（可選）：若設數字則帶入 amount，留空則不帶 amount（進頁面後自己點 MAX）
SELL_AMOUNT = os.getenv("SELL_AMOUNT","")  # 例：賣 100 顆，或留空
SELL_SLIPPAGE_BPS = os.getenv("SELL_SLIPPAGE_BPS", JUP_SLIPPAGE_BPS)

# ========= 可選：只推優質濾網 =========
GOOD_ONLY = os.getenv("GOOD_ONLY","0") == "1"                  # 1=只推優質
REQUIRE_AUTH_NONE = os.getenv("REQUIRE_AUTH_NONE","1") == "1"  # Mint/Freeze authority 必須 None
MAX_PRICE_IMPACT_BPS = int(os.getenv("MAX_PRICE_IMPACT_BPS","1500"))  # 15%
MAX_TOP10_HOLDER_PCT = int(os.getenv("MAX_TOP10_HOLDER_PCT","60"))    # 60%
JUP_QUOTE_URL = os.getenv("JUP_QUOTE_URL","https://quote-api.jup.ag/v6/quote")
JUP_TEST_IN_LAMPORTS = int(os.getenv("JUP_TEST_IN_LAMPORTS","50000000"))  # 0.05 SOL

HTTP_SEM = asyncio.Semaphore(max(1, HTTP_CONCURRENCY))

# ========= Label =========
PROGRAM_LABELS = {
  "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
  "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4 (Legacy)",
  "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
  "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
}

# ========= State =========
SEEN_SIGS = deque(maxlen=20000)
SEEN_SET  = set()

# ========= Utils =========
def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[TG] 未設定，略過：", text[:160]); return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r = requests.post(url, data={"chat_id":TG_CHAT, "text":text, "parse_mode":"HTML"}, timeout=8)
        if r.status_code != 200:
            print("[TG] 送出失敗:", r.status_code, r.text)
    except Exception as e:
        print("[TG] 例外:", e)

def program_label(pid: str|None) -> str:
    return PROGRAM_LABELS.get(pid or "", pid or "Unknown Program")

def format_sig_link(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"

def _mint_symbol(m: str) -> str:
    if m == "So11111111111111111111111111111111111111112": return "SOL"
    if m == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1": return "USDC"
    return m[:4] + "…" + m[-4:]

# ========= HTTP RPC（含回退）=========
def _current_http_url():
    return HTTP_FALLBACK_URL if (HTTP_PUBLIC_FALLBACK and time.time() < _http_fallback_until) else RPC_HTTP_URL

async def _http_post(payload: dict) -> dict:
    """
    帶最小間隔 + 429 全域退讓 + 多重回退輪替
    """
    global _http_fallback_until, _http_last_call, _http_global_backoff_until, _http_fallback_idx

    # 429 全域退讓
    now = time.time()
    if now < _http_global_backoff_until:
        await asyncio.sleep(_http_global_backoff_until - now)

    # 節流：控制 QPS
    if HTTP_MIN_INTERVAL_MS > 0:
        wait = max(0.0, (HTTP_MIN_INTERVAL_MS/1000.0) - (now - _http_last_call))
        if wait > 0: await asyncio.sleep(wait)

    # 選擇端點：主 or 回退池
    if HTTP_PUBLIC_FALLBACK and time.time() < _http_fallback_until and HTTP_FALLBACK_URLS:
        use_url = HTTP_FALLBACK_URLS[_http_fallback_idx % len(HTTP_FALLBACK_URLS)]
    else:
        use_url = RPC_HTTP_URL

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(use_url, json=payload)
            _http_last_call = time.time()
            j = r.json()
            err = j.get("error")
            if not err:
                return j

            code = err.get("code")
            msg  = (err.get("message") or "").lower()

            # 命中限流：全域退讓 + 切到下一個回退端點
            if (code in (-32429, 429)) or ("too many" in msg) or ("max usage" in msg):
                if HTTP_429_BACKOFF_MS > 0:
                    _http_global_backoff_until = time.time() + (HTTP_429_BACKOFF_MS/1000.0)
                    print(f"[HTTP] 429，暫停 {HTTP_429_BACKOFF_MS}ms")
                if HTTP_PUBLIC_FALLBACK and HTTP_FALLBACK_URLS:
                    _http_fallback_until = time.time() + HTTP_FALLBACK_COOLDOWN_SEC
                    _http_fallback_idx = (_http_fallback_idx + 1) % len(HTTP_FALLBACK_URLS)
                    print(f"[HTTP] 主節點限流，輪替到 {HTTP_FALLBACK_URLS[_http_fallback_idx]} {HTTP_FALLBACK_COOLDOWN_SEC}s")
            return j
    except Exception as e:
        print("[HTTP] request 失敗:", e)
        return {"error": {"message": str(e)}}


async def rpc_http_get_transaction_once(sig: str) -> dict | None:
    j = await _http_post({
        "jsonrpc":"2.0","id":1,"method":"getTransaction",
        "params":[sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]
    })
    if "error" in j:
        print(f"[RPC] getTransaction error for {sig}: {j['error']}"); return None
    return j.get("result")

async def rpc_http_get_signature_status(sig: str) -> str | None:
    j = await _http_post({
        "jsonrpc":"2.0","id":1,"method":"getSignatureStatuses",
        "params":[[sig], {"searchTransactionHistory": True}]
    })
    if "error" in j:
        print(f"[RPC] getSignatureStatuses error for {sig}: {j['error']}"); return None
    arr = (j.get("result", {}).get("value") or [])
    if arr and arr[0]:
        return (arr[0].get("confirmationStatus") or "").lower()
    return None

async def rpc_http_get_transaction(sig: str) -> dict | None:
    delay = TX_FETCH_DELAY_MS / 1000.0
    for i in range(TX_FETCH_RETRIES):
        tx = await rpc_http_get_transaction_once(sig)
        if tx: return tx
        if i in (0,2,4):
            st = await rpc_http_get_signature_status(sig)
            if st: print(f"[VALIDATE] 交易 {sig} 狀態：{st}（第 {i+1} 次）")
        await asyncio.sleep(delay); delay *= 1.5
    return None

# ========= 解析邏輯 =========
# 正式分類用
INIT_KEYS  = {"initialize","initialize2","initialize_pool","init_pool","create_pool","open_position","initialize_tick_array","initialize_config"}
ADDLP_KEYS = {"add_liquidity","deposit_liquidity","increase_liquidity"}  # （不包含 'deposit'）

# 預警用（更嚴格，避免洗頻）
PRELIM_INIT_KEYS  = {"initialize","initialize2","initialize_pool","create_pool","open_position"}
PRELIM_ADDLP_KEYS = {"add_liquidity","deposit_liquidity","increase_liquidity"}

def _match_type_like(s: str, keys: set[str]) -> bool:
    s = (s or "").lower()
    return any(k in s for k in keys)

def extract_program_instructions(tx: dict):
    if not tx: return []
    res = []
    msg = (tx.get("transaction") or {}).get("message") or {}
    for ins in (msg.get("instructions") or []): res.append(ins)
    meta = tx.get("meta") or {}
    for grp in (meta.get("innerInstructions") or []):
        for ins in grp.get("instructions", []): res.append(ins)
    return res

def classify_event_by_tx(tx: dict, focus: set[str]) -> tuple[str|None, dict]:
    if not tx: return None, {}
    hit_prog = None; hit_type = None
    for ins in extract_program_instructions(tx):
        pid = ins.get("programId")
        if not pid or pid not in focus: continue
        parsed = ins.get("parsed") or {}
        t = (parsed.get("type") or parsed.get("instruction")) or ""
        if _match_type_like(t, INIT_KEYS):  hit_prog, hit_type = pid, "NEW_POOL"; break
        if _match_type_like(t, ADDLP_KEYS): hit_prog, hit_type = pid, "ADD_LIQUIDITY"
    if not hit_type:
        meta = tx.get("meta") or {}
        logs = " ".join((meta.get("logMessages") or [])).lower()
        if any(pid in (tx.get("transaction") or {}).get("message",{}).get("accountKeys",[]) for pid in focus):
            if _match_type_like(logs, INIT_KEYS):  hit_type = "NEW_POOL"
            elif _match_type_like(logs, ADDLP_KEYS): hit_type = "ADD_LIQUIDITY"
            if hit_type:
                for k in focus:
                    if k in logs: hit_prog = k; break
    return hit_type, {"programId": hit_prog}

def logs_hint_is_candidate(logs: list[str]) -> bool:
    s = " ".join((logs or []))
    if not s: return False
    if PRELIM_STRICT:
        return _match_type_like(s, PRELIM_INIT_KEYS | PRELIM_ADDLP_KEYS)
    else:
        return _match_type_like(s, INIT_KEYS | ADDLP_KEYS)

# ========= 交易對推測 & 濾網輔助 =========
def guess_pair_from_tx(tx: dict) -> tuple[str|None, str|None]:
    """盡量讓 base 為 QUOTED_BASES（wSOL/USDC），另一邊當 quote。"""
    if not tx: return (None, None)
    keys = (tx.get("transaction") or {}).get("message",{}).get("accountKeys",[]) or []
    mints = [k.get("pubkey") if isinstance(k,dict) else k for k in keys]

    base = None
    for b in QUOTED_BASES:
        if b in mints:
            base = b; break
    if not base: return (None, None)

    quote = None
    for pk in mints:
        if pk != base and pk not in QUOTED_BASES:
            quote = pk; break
    return (base, quote)

async def get_mint_info(mint_pubkey: str) -> dict | None:
    j = await _http_post({
        "jsonrpc":"2.0","id":1,"method":"getAccountInfo",
        "params":[mint_pubkey, {"encoding":"jsonParsed"}]
    })
    if "error" in j: return None
    v = (j.get("result") or {}).get("value") or {}
    return (v.get("data") or {}).get("parsed",{}).get("info")

async def get_top_holders_pct(mint_pubkey: str) -> float | None:
    j = await _http_post({
        "jsonrpc":"2.0","id":1,"method":"getTokenLargestAccounts",
        "params":[mint_pubkey, {"commitment":"confirmed"}]
    })
    if "error" in j: return None
    vals = (j.get("result") or {}).get("value") or []
    top = sum([float(x.get("uiAmount",0)) for x in vals[:10]])
    info = await get_mint_info(mint_pubkey)
    if not info: return None
    supply = float(info.get("supply",0)) / (10 ** int(info.get("decimals",0)))
    if supply <= 0: return None
    return (top / supply) * 100.0

async def jup_has_reasonable_route(mint_in: str, mint_out: str, in_amount: int) -> tuple[bool, int]:
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            params = {"inputMint": mint_in, "outputMint": mint_out, "amount": in_amount, "slippageBps": 200}
            r = await client.get(JUP_QUOTE_URL, params=params)
            q = r.json()
            routes = q.get("data") or []
            if not routes: return (False, 99999)
            rt = routes[0]
            if not float(rt.get("inAmount",0)) or not float(rt.get("outAmount",0)):
                return (False, 99999)
            price_impact_bps = int(rt.get("priceImpactPct", 0)*10000) if "priceImpactPct" in rt else 0
            return (True, price_impact_bps)
    except Exception as e:
        print("[JUP] quote 失敗:", e)
        return (False, 99999)

async def is_good_opportunity(tx: dict) -> tuple[bool, str]:
    base, quote = guess_pair_from_tx(tx)
    if not base or not quote: return (False, "pair_not_supported")
    if base not in QUOTED_BASES and quote not in QUOTED_BASES:
        return (False, "no_whitelisted_base")
    if base not in QUOTED_BASES:
        base, quote = quote, base
    if REQUIRE_AUTH_NONE:
        mi = await get_mint_info(quote)
        if not mi: return (False, "mint_info_unavailable")
        if mi.get("mintAuthority") is not None or mi.get("freezeAuthority") is not None:
            return (False, "mint_or_freeze_not_none")
    ok, imp_bps = await jup_has_reasonable_route(base, quote, JUP_TEST_IN_LAMPORTS)
    if not ok: return (False, "no_jup_route")
    if imp_bps and imp_bps > MAX_PRICE_IMPACT_BPS:
        return (False, f"price_impact_too_high_{imp_bps}")
    pct = await get_top_holders_pct(quote)
    if pct is None or pct > MAX_TOP10_HOLDER_PCT:
        return (False, f"top10_holder_{pct or 'NA'}")
    return (True, "ok")

# ========= 一鍵下單/賣出連結 =========
def build_trade_links(base_mint: str, quote_mint: str) -> tuple[str, str, str, str]:
    """回傳 (buy_jup, buy_ray, sell_jup, sell_ray)"""
    # 買入（base -> quote）
    buy_jup = (
        f"{JUP_URL_BASE}/{_mint_symbol(base_mint)}-{_mint_symbol(quote_mint)}"
        f"?inputMint={base_mint}&outputMint={quote_mint}"
        f"&amount={JUP_AMOUNT}&slippageBps={JUP_SLIPPAGE_BPS}"
    )
    buy_ray = f"{RAY_URL_BASE}?inputCurrency={base_mint}&outputCurrency={quote_mint}&fixed=in"

    # 賣出（quote -> base）
    sell_jup = (
        f"{JUP_URL_BASE}/{_mint_symbol(quote_mint)}-{_mint_symbol(base_mint)}"
        f"?inputMint={quote_mint}&outputMint={base_mint}"
        f"{f'&amount={SELL_AMOUNT}' if SELL_AMOUNT else ''}"
        f"&slippageBps={SELL_SLIPPAGE_BPS}"
    )
    sell_ray = f"{RAY_URL_BASE}?inputCurrency={quote_mint}&outputCurrency={base_mint}&fixed=in"
    return buy_jup, buy_ray, sell_jup, sell_ray

# ========= PRELIM 快速補鏈結 =========
async def rpc_quick_get_transaction(sig: str, timeout_ms: int = FAST_TX_TIMEOUT_MS) -> dict | None:
    url = _current_http_url()
    payload = {"jsonrpc":"2.0","id":1,"method":"getTransaction",
               "params":[sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]}
    try:
        async with httpx.AsyncClient(timeout=max(0.2, timeout_ms/1000)) as client:
            r = await client.post(url, json=payload)
            j = r.json()
            return j.get("result")
    except Exception:
        return None

async def _prelim_try_links(sig: str):
    tx = await rpc_quick_get_transaction(sig)
    if not tx: 
        return
    base, quote = guess_pair_from_tx(tx)
    if not base and JUP_BASE:
        base = JUP_BASE
    if base and quote:
        buy_jup, buy_ray, sell_jup, sell_ray = build_trade_links(base, quote)
        tg_send(f"[PRELIM-LINK] 一鍵下單/賣出\n• 買 Jupiter：{buy_jup}\n• 買 Raydium：{buy_ray}\n• 賣 Jupiter：{sell_jup}\n• 賣 Raydium：{sell_ray}\n{format_sig_link(sig)}")

# ========= WebSocket（含 PRELIM & 回退）=========
async def ws_consume():
    if not PROGRAM_IDS: raise RuntimeError("PROGRAM_IDS 不可為空")
    focus = set(PROGRAM_IDS)
    if WS_CONNECT_OFFSET > 0: await asyncio.sleep(WS_CONNECT_OFFSET)

    backoff, backoff_max = 5, 120
    fallback_until = 0

    def show(u: str) -> str: return (u.split("?")[0] if "?" in u else u)

    while True:
        use_url = WS_FALLBACK_URL if (WS_PUBLIC_FALLBACK and time.time() < fallback_until) else RPC_WS_URL
        try:
            print("[WS] connecting to:", show(use_url))
            async with websockets.connect(use_url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=2000) as ws:
                backoff = 5
                for idx, pid in enumerate(PROGRAM_IDS, start=1):
                    await ws.send(json.dumps({
                        "jsonrpc":"2.0","id":idx,"method":"logsSubscribe",
                        "params":[{"mentions":[pid]}, {"commitment":WS_COMMITMENT}]
                    }))
                print("[WS] Subscribed to", PROGRAM_IDS)
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("method") != "logsNotification": continue
                    val = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
                    sig, logs = val.get("signature"), (val.get("logs") or [])
                    ws_ts = time.time()
                    if not sig or sig in SEEN_SET or not logs_hint_is_candidate(logs): continue
                    SEEN_SET.add(sig); SEEN_SIGS.append(sig)
                    if PRELIM_ALERT:
                        tg_send(f"[PRELIM] DEX 事件(偵測到)．簽名 <code>{sig}</code>\n{format_sig_link(sig)}")
                        if PRELIM_LINKS:
                            asyncio.create_task(_prelim_try_links(sig))
                    asyncio.create_task(_post_validate_and_notify(sig, focus, ws_ts))
        except websockets.exceptions.InvalidStatusCode as e:
            code = getattr(e, "status_code", None)
            if code == 429 and WS_PUBLIC_FALLBACK and "helius" in use_url:
                fallback_until = time.time() + WS_FALLBACK_COOLDOWN_SEC
                print(f"[WS] 429，回退到 {show(WS_FALLBACK_URL)} {WS_FALLBACK_COOLDOWN_SEC}s")
            wait = max(1.0, backoff + random.uniform(-0.2*backoff, 0.2*backoff))
            print(f"[WS] 連線被拒 (HTTP {code})，{wait:.1f}s 後重連")
            await asyncio.sleep(wait); backoff = min(backoff * 2, backoff_max)
        except Exception as e:
            wait = max(1.0, backoff + random.uniform(-0.2*backoff, 0.2*backoff))
            print(f"[WS] 連線中斷：{e}，{wait:.1f}s 後重連")
            await asyncio.sleep(wait); backoff = min(backoff * 2, backoff_max)

async def _post_validate_and_notify(sig: str, focus: set[str], ws_ts: float | None = None):
    try:
        async with HTTP_SEM:
            tx = await rpc_http_get_transaction(sig)
        if not tx:
            print(f"[VALIDATE] 交易 {sig} 沒有拿到資料；略過")
            return

        ev_type, details = classify_event_by_tx(tx, focus)

        # —— 你要的一行除錯：為什麼沒有正式訊息 —— #
        if not ev_type:
            print(f"[CLASSIFY] skip {sig}: not NEW_POOL/ADD_LIQUIDITY")
            return
        # ----------------------------------------- #

        # （可選）只推優質
        if GOOD_ONLY:
            ok, why = await is_good_opportunity(tx)
            if not ok:
                print(f"[FILTER] drop {sig} because {why}")
                return

        pid = details.get("programId")
        label = program_label(pid)

        # 推測交易對
        base, quote = guess_pair_from_tx(tx)
        if not base or base not in QUOTED_BASES:
            base = JUP_BASE
        if not quote or quote in QUOTED_BASES:
            keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys", []) or []
            mints = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
            quote = next((k for k in mints if k not in QUOTED_BASES), None)

        buy_jup = buy_ray = sell_jup = sell_ray = ""
        if base and quote:
            buy_jup, buy_ray, sell_jup, sell_ray = build_trade_links(base, quote)

        head = "🆕 新池建立" if ev_type == "NEW_POOL" else "➕ 加入流動性"
        lat_ms = f"\n(延遲: {int((time.time() - ws_ts) * 1000)}ms)" if (SHOW_LATENCY and ws_ts) else ""
        text = (
            f"{head}  <b>{label}</b>\n"
            f"Sig: <code>{sig}</code>\n{format_sig_link(sig)}\n"
            f"(已驗證{' + 過濾通過' if GOOD_ONLY else ''}){lat_ms}"
        )
        if buy_jup:
            text += (
                f"\n\n<b>一鍵下單</b>\n• 買 Jupiter：{buy_jup}\n• 買 Raydium：{buy_ray}"
                f"\n<b>一鍵賣出</b>\n• 賣 Jupiter：{sell_jup}\n• 賣 Raydium：{sell_ray}"
            )
        tg_send(text)
    except Exception as e:
        print("[POST-VALIDATE] 解析失敗:", sig, e)

# ========= Flask =========
app = Flask(__name__)

@app.get("/healthz")
def healthz(): return "ok", 200

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
            if not sig or sig in SEEN_SET: continue
            SEEN_SET.add(sig); SEEN_SIGS.append(sig)
            asyncio.run_coroutine_threadsafe(_post_validate_and_notify(sig, set(PROGRAM_IDS), time.time()), loop)
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
    if RPC_WS_URL.strip():
        t = threading.Thread(target=start_async_loop, daemon=True)
        t.start()
    run_flask()
