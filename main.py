import os, asyncio, json, time, threading, random
from collections import deque
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import requests, httpx, websockets

load_dotenv()

# =========================== 基本設定 ===========================
RPC_WS_URL   = os.getenv("RPC_WS_URL", "wss://api.mainnet-beta.solana.com")
WS_COMMITMENT = os.getenv("WS_COMMITMENT", "processed")  # processed / confirmed
DISABLE_WS = os.getenv("DISABLE_WS", "0") == "1"

# HTTP 主端點（建議官方），回退端點（建議放 Helius/Alchemy/QuickNode 等可保證 JSON 的服務）
RPC_HTTP_URL = os.getenv("RPC_HTTP_URL", "https://api.mainnet-beta.solana.com")
HTTP_PUBLIC_FALLBACK = os.getenv("HTTP_PUBLIC_FALLBACK", "1") == "1"
HTTP_FALLBACK_URLS = [u.strip() for u in os.getenv(
    "HTTP_FALLBACK_URLS", "https://mainnet.helius-rpc.com/?api-key=<YOUR_KEY>"
).split(",") if u.strip() and "<YOUR_KEY>" not in u]
HTTP_FALLBACK_COOLDOWN_SEC = int(os.getenv("HTTP_FALLBACK_COOLDOWN_SEC", "20"))

# 各項節流/退讓
HTTP_MIN_INTERVAL_MS = int(os.getenv("HTTP_MIN_INTERVAL_MS", "1200"))
HTTP_429_BACKOFF_MS  = int(os.getenv("HTTP_429_BACKOFF_MS", "1800"))

# 監控 Program
PROGRAM_IDS = [p.strip() for p in os.getenv("PROGRAM_IDS", "").split(",") if p.strip()]

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# =========================== 行為調參 ===========================
TX_FETCH_RETRIES   = int(os.getenv("TX_FETCH_RETRIES", "2"))
TX_FETCH_DELAY_MS  = int(os.getenv("TX_FETCH_DELAY_MS", "700"))

PROCESS_QPS = float(os.getenv("PROCESS_QPS", "0.6"))
MAX_QUEUE   = int(os.getenv("MAX_QUEUE", "300"))

WATCH_NEW_POOL  = os.getenv("WATCH_NEW_POOL", "1") == "1"
WATCH_ADDLP     = os.getenv("WATCH_ADDLP",  "0") == "1"

PRELIM_ALERT = os.getenv("PRELIM_ALERT", "0") == "1"
PRELIM_LINKS = os.getenv("PRELIM_LINKS", "0") == "1"

# =========================== 一鍵下單/賣出 ===========================
JUP_BASE = os.getenv("JUP_BASE", "So11111111111111111111111111111111111111112")  # wSOL
QUOTED_BASES = os.getenv(
    "QUOTED_BASES",
    "So11111111111111111111111111111111111111112,EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1"
).split(",")

JUP_AMOUNT = os.getenv("JUP_AMOUNT", "0.25")
JUP_SLIPPAGE_BPS = os.getenv("JUP_SLIPPAGE_BPS", "300")
SELL_AMOUNT = os.getenv("SELL_AMOUNT", "")
SELL_SLIPPAGE_BPS = os.getenv("SELL_SLIPPAGE_BPS", JUP_SLIPPAGE_BPS)

JUP_URL_BASE = os.getenv("JUP_URL_BASE", "https://jup.ag/swap")
RAY_URL_BASE = os.getenv("RAY_URL_BASE", "https://raydium.io/swap/")

# =========================== 可選濾網（預設關閉） ===========================
GOOD_ONLY = os.getenv("GOOD_ONLY", "0") == "1"
REQUIRE_AUTH_NONE = os.getenv("REQUIRE_AUTH_NONE", "1") == "1"
MAX_PRICE_IMPACT_BPS = int(os.getenv("MAX_PRICE_IMPACT_BPS", "1500"))
MAX_TOP10_HOLDER_PCT = int(os.getenv("MAX_TOP10_HOLDER_PCT", "60"))
JUP_QUOTE_URL = os.getenv("JUP_QUOTE_URL", "https://quote-api.jup.ag/v6/quote")
JUP_TEST_IN_LAMPORTS = int(os.getenv("JUP_TEST_IN_LAMPORTS", "50000000"))  # 0.05 SOL

# =========================== 標籤 ===========================
PROGRAM_LABELS = {
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4 (Legacy)",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
}

# =========================== 狀態 ===========================
SEEN_SIGS = deque(maxlen=20000)
SEEN_SET  = set()
REQUEUED_ONCE = set()

# =========================== Provider 管理（每 provider 各自冷卻） ===========================
PROVIDERS = [RPC_HTTP_URL] + [u for u in HTTP_FALLBACK_URLS if u and u != RPC_HTTP_URL]
_prov_cooldown = {u: 0.0 for u in PROVIDERS}  # 各 provider 的冷卻截止時間
_http_last_call = 0.0
_http_global_backoff_until = 0.0

# =========================== 小工具 ===========================
def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[TG] 未設定，略過：", text[:160]); return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r = requests.post(url, data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=8)
        if r.status_code != 200:
            print("[TG] 送出失敗:", r.status_code, r.text)
    except Exception as e:
        print("[TG] 例外:", e)

def program_label(pid): return PROGRAM_LABELS.get(pid or "", pid or "Unknown Program")
def format_sig_link(sig: str) -> str: return f"https://solscan.io/tx/{sig}"
def _mint_symbol(m: str) -> str:
    if m == "So11111111111111111111111111111111111111112": return "SOL"
    if m == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1": return "USDC"
    return m[:4] + "…" + m[-4:]

# =========================== HTTP 請求（節流 + 429 全域退讓 + 每 provider 冷卻 + 強韌解析） ===========================
async def _http_post(payload: dict) -> dict:
    global _http_last_call, _http_global_backoff_until, _prov_cooldown

    now = time.time()
    if now < _http_global_backoff_until:
        await asyncio.sleep(_http_global_backoff_until - now)

    if HTTP_MIN_INTERVAL_MS > 0:
        wait = max(0.0, (HTTP_MIN_INTERVAL_MS/1000.0) - (now - _http_last_call))
        if wait > 0: await asyncio.sleep(wait)

    # 選 provider：主 > 其他；若都在冷卻，等最近解凍的那個
    order = [RPC_HTTP_URL] + [u for u in PROVIDERS if u != RPC_HTTP_URL]
    now = time.time()
    ready = [u for u in order if now >= _prov_cooldown.get(u, 0.0)]
    if not ready:
        soonest = min(order, key=lambda u: _prov_cooldown.get(u, 0.0))
        wait = max(0.0, _prov_cooldown.get(soonest, 0.0) - now)
        if wait > 0:
            print(f"[HTTP] 所有 provider 冷卻中，等待 {int(wait*1000)}ms")
            await asyncio.sleep(wait)
        use_url = soonest
    else:
        use_url = ready[0]

    try:
        async with httpx.AsyncClient(timeout=8, headers={"Content-Type": "application/json"}) as client:
            r = await client.post(use_url, json=payload)
            _http_last_call = time.time()

            # 強韌解析：確保回 dict
            try:
                j = r.json()
            except Exception:
                txt = (r.text or "").strip()
                j = {"error": {"code": r.status_code, "message": f"non-json response: {txt[:160]}"}}
            if not isinstance(j, dict):
                j = {"error": {"code": r.status_code, "message": str(j)}}
            if r.status_code >= 400 and "error" not in j:
                j = {"error": {"code": r.status_code, "message": "http error"}}

            # 針對「該 provider」冷卻；同時觸發全域退讓
            err = j.get("error")
            if err:
                code = err.get("code")
                msg  = (err.get("message") or "").lower()
                is_rate = (code in (-32429, 429)) or ("too many" in msg) or ("max usage" in msg)
                is_bad  = ("non-json" in msg) or ("request failure" in msg) or ("http error" in msg)
                if is_rate or is_bad:
                    _prov_cooldown[use_url] = time.time() + HTTP_FALLBACK_COOLDOWN_SEC
                    print(f"[HTTP] provider 冷卻：{use_url}  {HTTP_FALLBACK_COOLDOWN_SEC}s")
                    if HTTP_429_BACKOFF_MS > 0:
                        _http_global_backoff_until = time.time() + (HTTP_429_BACKOFF_MS/1000.0)
                        print(f"[HTTP] 全域暫停 {HTTP_429_BACKOFF_MS}ms")
            return j
    except Exception as e:
        _prov_cooldown[use_url] = time.time() + HTTP_FALLBACK_COOLDOWN_SEC
        return {"error": {"message": f"request failure: {e}"}}

# =========================== RPC 包裝 ===========================
async def rpc_http_get_transaction_once(sig: str):
    j = await _http_post({
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
    })
    if "error" in j:
        print(f"[RPC] getTransaction error for {sig}: {j['error']}")
        return None
    return j.get("result")

async def rpc_http_get_signature_status(sig: str):
    j = await _http_post({
        "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
        "params": [[sig], {"searchTransactionHistory": True}]
    })
    if "error" in j:
        print(f"[RPC] getSignatureStatuses error for {sig}: {j['error']}")
        return None
    arr = (j.get("result", {}).get("value") or [])
    if arr and arr[0]:
        return (arr[0].get("confirmationStatus") or "").lower()
    return None

async def rpc_http_get_transaction(sig: str):
    # 1) 先等到 confirmed/finalized（最多 6 次，指數回退）
    delay = 0.5
    for _ in range(6):
        st = await rpc_http_get_signature_status(sig)
        if st in ("confirmed", "finalized"):
            break
        await asyncio.sleep(delay)
        delay *= 1.4

    # 2) 再拉交易（小次數，指數回退）
    delay = TX_FETCH_DELAY_MS / 1000.0
    for _ in range(max(1, TX_FETCH_RETRIES)):
        tx = await rpc_http_get_transaction_once(sig)
        if tx:
            return tx
        await asyncio.sleep(delay)
        delay *= 1.5
    return None

# =========================== 事件判別 ===========================
INIT_KEYS  = {"initialize", "initialize2", "initialize_pool", "init_pool", "create_pool", "open_position", "initialize_tick_array", "initialize_config"}
ADDLP_KEYS = {"add_liquidity", "deposit_liquidity", "increase_liquidity"}

def _match_type_like(s: str, keys: set) -> bool:
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

def classify_event_by_tx(tx: dict, focus: set):
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
        if any(pid in (tx.get("transaction") or {}).get("message", {}).get("accountKeys", []) for pid in focus):
            if _match_type_like(logs, INIT_KEYS):  hit_type = "NEW_POOL"
            elif _match_type_like(logs, ADDLP_KEYS): hit_type = "ADD_LIQUIDITY"
            if hit_type:
                for k in focus:
                    if k in logs: hit_prog = k; break
    return hit_type, {"programId": hit_prog}

def logs_hint_is_candidate(logs: list) -> bool:
    s = " ".join((logs or [])).lower()
    if not s: return False
    hit_init  = any(k in s for k in INIT_KEYS)
    hit_addlp = any(k in s for k in ADDLP_KEYS)
    if WATCH_NEW_POOL and hit_init: return True
    if WATCH_ADDLP   and hit_addlp: return True
    return False

# =========================== 交易對 / 濾網輔助 ===========================
def guess_pair_from_tx(tx: dict):
    if not tx: return (None, None)
    keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys", []) or []
    mints = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    base = None
    for b in QUOTED_BASES:
        if b in mints: base = b; break
    if not base: return (None, None)
    quote = None
    for pk in mints:
        if pk != base and pk not in QUOTED_BASES:
            quote = pk; break
    return (base, quote)

async def get_mint_info(mint_pubkey: str):
    j = await _http_post({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                          "params": [mint_pubkey, {"encoding": "jsonParsed"}]})
    if "error" in j: return None
    v = (j.get("result") or {}).get("value") or {}
    return (v.get("data") or {}).get("parsed", {}).get("info")

async def get_top_holders_pct(mint_pubkey: str):
    j = await _http_post({"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
                          "params": [mint_pubkey, {"commitment": "confirmed"}]})
    if "error" in j: return None
    vals = (j.get("result") or {}).get("value") or []
    top = sum([float(x.get("uiAmount", 0)) for x in vals[:10]])
    info = await get_mint_info(mint_pubkey)
    if not info: return None
    supply = float(info.get("supply", 0)) / (10 ** int(info.get("decimals", 0)))
    if supply <= 0: return None
    return (top / supply) * 100.0

async def jup_has_reasonable_route(mint_in: str, mint_out: str, in_amount: int):
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            params = {"inputMint": mint_in, "outputMint": mint_out, "amount": in_amount, "slippageBps": 200}
            r = await client.get(JUP_QUOTE_URL, params=params)
            q = r.json()
            routes = q.get("data") or []
            if not routes: return (False, 99999)
            rt = routes[0]
            if not float(rt.get("inAmount", 0)) or not float(rt.get("outAmount", 0)):
                return (False, 99999)
            price_impact_bps = int(rt.get("priceImpactPct", 0) * 10000) if "priceImpactPct" in rt else 0
            return (True, price_impact_bps)
    except Exception as e:
        print("[JUP] quote 失敗:", e)
        return (False, 99999)

async def is_good_opportunity(tx: dict):
    base, quote = guess_pair_from_tx(tx)
    if not base or not quote: return (False, "pair_not_supported")
    if base not in QUOTED_BASES and quote not in QUOTED_BASES: return (False, "no_whitelisted_base")
    if base not in QUOTED_BASES: base, quote = quote, base
    if REQUIRE_AUTH_NONE:
        mi = await get_mint_info(quote)
        if not mi: return (False, "mint_info_unavailable")
        if mi.get("mintAuthority") is not None or mi.get("freezeAuthority") is not None:
            return (False, "mint_or_freeze_not_none")
    ok, imp_bps = await jup_has_reasonable_route(base, quote, JUP_TEST_IN_LAMPORTS)
    if not ok: return (False, "no_jup_route")
    if imp_bps and imp_bps > MAX_PRICE_IMPACT_BPS: return (False, f"price_impact_too_high_{imp_bps}")
    pct = await get_top_holders_pct(quote)
    if pct is None or pct > MAX_TOP10_HOLDER_PCT: return (False, f"top10_holder_{pct or 'NA'}")
    return (True, "ok")

# =========================== 一鍵連結 ===========================
def build_trade_links(base_mint: str, quote_mint: str):
    buy_jup = (
        f"{JUP_URL_BASE}/{_mint_symbol(base_mint)}-{_mint_symbol(quote_mint)}"
        f"?inputMint={base_mint}&outputMint={quote_mint}"
        f"&amount={JUP_AMOUNT}&slippageBps={JUP_SLIPPAGE_BPS}"
    )
    buy_ray = f"{RAY_URL_BASE}?inputCurrency={base_mint}&outputCurrency={quote_mint}&fixed=in"
    sell_jup = (
        f"{JUP_URL_BASE}/{_mint_symbol(quote_mint)}-{_mint_symbol(base_mint)}"
        f"?inputMint={quote_mint}&outputMint={base_mint}"
        f"{f'&amount={SELL_AMOUNT}' if SELL_AMOUNT else ''}"
        f"&slippageBps={SELL_SLIPPAGE_BPS}"
    )
    sell_ray = f"{RAY_URL_BASE}?inputCurrency={quote_mint}&outputCurrency={base_mint}&fixed=in"
    return buy_jup, buy_ray, sell_jup, sell_ray

# =========================== 正式處理 ===========================
async def _post_validate_and_notify(sig: str, focus: set, ws_ts: float = None, tx_override: dict | None = None):
    try:
        tx = tx_override or await rpc_http_get_transaction(sig)
        if not tx:
            if sig not in REQUEUED_ONCE:
                REQUEUED_ONCE.add(sig)
                print(f"[VALIDATE] 交易 {sig} 暫時取不到，2s 後重排一次")
                await asyncio.sleep(2.0)
                try: PROCESS_QUEUE.put_nowait((sig, ws_ts or time.time()))
                except asyncio.QueueFull: pass
                return
            print(f"[VALIDATE] 交易 {sig} 二次仍失敗；略過")
            return

        ev_type, details = classify_event_by_tx(tx, focus)
        if not ev_type:
            print(f"[CLASSIFY] skip {sig}: not NEW_POOL/ADD_LIQUIDITY")
            return
        if ev_type == "NEW_POOL" and not WATCH_NEW_POOL: return
        if ev_type == "ADD_LIQUIDITY" and not WATCH_ADDLP: return

        if GOOD_ONLY:
            ok, why = await is_good_opportunity(tx)
            if not ok:
                print(f"[FILTER] drop {sig} because {why}")
                return

        pid = (details or {}).get("programId")
        label = program_label(pid)

        base, quote = guess_pair_from_tx(tx)
        if not base or base not in QUOTED_BASES: base = JUP_BASE
        if not quote or quote in QUOTED_BASES:
            keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys", []) or []
            mints = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
            quote = next((k for k in mints if k not in QUOTED_BASES), None)

        buy_jup=buy_ray=sell_jup=sell_ray=""
        if base and quote:
            buy_jup, buy_ray, sell_jup, sell_ray = build_trade_links(base, quote)

        head = "🆕 新池建立" if ev_type == "NEW_POOL" else "➕ 加入流動性"
        lat = f"\n(延遲: {int((time.time() - ws_ts) * 1000)}ms)" if ws_ts else ""
        text = (
            f"{head}  <b>{label}</b>\n"
            f"Sig: <code>{sig}</code>\n{format_sig_link(sig)}\n"
            f"(已驗證{' + 過濾通過' if GOOD_ONLY else ''}){lat}"
        )
        if buy_jup:
            text += (
                f"\n\n<b>一鍵下單</b>\n• 買 Jupiter：{buy_jup}\n• 買 Raydium：{buy_ray}"
                f"\n<b>一鍵賣出</b>\n• 賣 Jupiter：{sell_jup}\n• 賣 Raydium：{sell_ray}"
            )
        tg_send(text)
    except Exception as e:
        print("[POST-VALIDATE] 解析失敗:", sig, e)

# =========================== 佇列處理器（限速） ===========================
PROCESS_QUEUE = asyncio.Queue(maxsize=MAX_QUEUE)

async def process_worker(focus: set):
    interval = 1.0 / max(0.1, PROCESS_QPS)
    last = 0.0
    while True:
        sig, ws_ts = await PROCESS_QUEUE.get()
        now = time.time()
        wait = interval - (now - last)
        if wait > 0: await asyncio.sleep(wait)
        last = time.time()
        try:
            await _post_validate_and_notify(sig, focus, ws_ts)
        finally:
            PROCESS_QUEUE.task_done()

# =========================== WebSocket 訂閱 ===========================
async def ws_consume():
    if not PROGRAM_IDS:
        raise RuntimeError("PROGRAM_IDS 不可為空")
    focus = set(PROGRAM_IDS)
    backoff, backoff_max = 5, 120
    while True:
        try:
            print("[WS] connecting to:", RPC_WS_URL)
            async with websockets.connect(
                RPC_WS_URL, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=2000
            ) as ws:
                backoff = 5
                for idx, pid in enumerate(PROGRAM_IDS, start=1):
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": idx, "method": "logsSubscribe",
                        "params": [{"mentions": [pid]}, {"commitment": WS_COMMITMENT}]
                    }))
                print("[WS] Subscribed to", PROGRAM_IDS)
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("method") != "logsNotification": continue
                    val = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
                    sig, logs = val.get("signature"), (val.get("logs") or [])
                    ws_ts = time.time()
                    if not sig or sig in SEEN_SET: continue
                    if not logs_hint_is_candidate(logs): continue
                    SEEN_SET.add(sig); SEEN_SIGS.append(sig)
                    try:
                        PROCESS_QUEUE.put_nowait((sig, ws_ts))
                    except asyncio.QueueFull:
                        try:
                            _ = PROCESS_QUEUE.get_nowait()
                            PROCESS_QUEUE.task_done()
                        except Exception:
                            pass
                        PROCESS_QUEUE.put_nowait((sig, ws_ts))
        except websockets.exceptions.InvalidStatusCode as e:
            code = getattr(e, "status_code", None)
            wait = max(1.0, backoff + random.uniform(-0.2*backoff, 0.2*backoff))
            print(f"[WS] 連線被拒 (HTTP {code})，{wait:.1f}s 後重連")
            await asyncio.sleep(wait); backoff = min(backoff * 2, backoff_max)
        except Exception as e:
            wait = max(1.0, backoff + random.uniform(-0.2*backoff, 0.2*backoff))
            print(f"[WS] 連線中斷：{e}，{wait:.1f}s 後重連")
            await asyncio.sleep(wait); backoff = min(backoff * 2, backoff_max)

# =========================== Flask（Webhook） ===========================
app = Flask(__name__)

@app.get("/healthz")
def healthz(): return "ok", 200

from flask import Flask, request
import json

app = Flask(__name__)

@app.route("/helius", methods=["POST"])
def helius_webhook():
    try:
        data = request.get_json(force=True)

        # Debug：完整印出 webhook payload，先確認格式
        print("[WEBHOOK RAW]", json.dumps(data, indent=2, ensure_ascii=False))

        handled_count = 0

        # Helius webhook 送進來的是「陣列」
        for tx in data:
            sig = None

            # 先試一般 webhook 結構
            if "signature" in tx:
                sig = tx["signature"]

            # 如果沒有，就抓 transaction 裡面的 signatures[0]
            elif "transaction" in tx and "signatures" in tx["transaction"]:
                sigs = tx["transaction"]["signatures"]
                if sigs and len(sigs[0]) == 88:
                    sig = sigs[0]

            if not sig or len(sig) != 88:
                print(f"[ERROR] Invalid signature: {sig}")
                continue

            print(f"[OK] Got signature: {sig}")

            # 送去你原本的 classify + validate pipeline
            classify_and_process(tx)

            handled_count += 1

        return {"handled": handled_count, "ok": True}

    except Exception as e:
        print("[WEBHOOK ERROR]", str(e))
        return {"handled": 0, "ok": False, "error": str(e)}, 500


def run_flask():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)

# =========================== Start ===========================
loop = asyncio.new_event_loop()

def start_async_loop():
    asyncio.set_event_loop(loop)
    focus = set(PROGRAM_IDS)
    loop.create_task(process_worker(focus))
    if not DISABLE_WS:
        loop.run_until_complete(ws_consume())
    else:
        loop.run_forever()

if __name__ == "__main__":
    t = threading.Thread(target=start_async_loop, daemon=True)
    t.start()
    run_flask()
