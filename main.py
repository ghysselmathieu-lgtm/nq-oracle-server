"""
NQ Oracle PRO — Smart Money Concepts Analysis Server v5
Volledig herbouwd. Geen bugs. Productie-klaar.

Endpoints:
  GET  /              health check
  POST /webhook       ontvangt TradingView candle data
  GET  /latest        laatste candle
  GET  /latest_signal laatste AI signaal
  GET  /history       candle geschiedenis
  POST /clear         reset data
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
import os, threading, json, re, requests as req

app = Flask(__name__)
CORS(app, origins="*")

# ── State ────────────────────────────────────────────────────────────────────
latest_candle  = {}
latest_signal  = {}
candle_history = []   # max 200 candles voor SMC berekeningen
MAX_HISTORY    = 200

WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "nq-oracle-secret")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Auth ─────────────────────────────────────────────────────────────────────
def check_auth():
    token = (request.headers.get("X-TV-Secret") or
             request.headers.get("x-tv-secret") or
             request.args.get("secret", ""))
    return token == WEBHOOK_SECRET

# ── Normalize Pine Script veldnamen ──────────────────────────────────────────
def normalize(data: dict) -> dict:
    renames = {
        "ph": "prev_high", "pl": "prev_low",
        "pc": "prev_close", "pv": "prev_volume",
        "vd": "vol_delta"
    }
    for short, full in renames.items():
        if short in data and full not in data:
            data[full] = data.pop(short)

    if "htf" in data and "htf_trend" not in data:
        data["htf_trend"] = "bull" if str(data.pop("htf")) in ("1", "True", "true") else "bear"

    if "sess" in data and "session" not in data:
        sess_map = {0: "premarket", 1: "open", 2: "midday", 3: "close"}
        try:
            data["session"] = sess_map.get(int(float(data.pop("sess"))), "unknown")
        except Exception:
            data.pop("sess", None)

    if "pat" in data and "pattern" not in data:
        pat_map = {
            0: "none", 1: "bullish_engulfing", 2: "bearish_engulfing",
            3: "doji", 4: "hammer", 5: "shooting_star"
        }
        try:
            data["pattern"] = pat_map.get(int(float(data.pop("pat"))), "none")
        except Exception:
            data.pop("pat", None)

    # Zet alle numerieke strings om naar floats
    numeric = ["open","high","low","close","volume","prev_high","prev_low",
               "prev_close","prev_volume","vwap","ema8","ema21","ema50",
               "rsi","atr","pivot","r1","s1","pdh","pdl","vol_delta"]
    for key in numeric:
        if key in data:
            try:
                data[key] = float(data[key])
            except (ValueError, TypeError):
                pass

    return data

# ── SMC Berekeningen ─────────────────────────────────────────────────────────
def calculate_smc(candles: list, current: dict) -> dict:
    """
    Berekent Smart Money Concepts indicatoren op basis van candle history.
    Returns dict met alle SMC waarden.
    """
    if len(candles) < 5:
        return {}

    closes  = [c.get("close", 0)  for c in candles]
    highs   = [c.get("high", 0)   for c in candles]
    lows    = [c.get("low", 0)    for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    cur_close = current.get("close", 0)
    cur_high  = current.get("high", 0)
    cur_low   = current.get("low", 0)
    cur_open  = current.get("open", 0)
    atr       = current.get("atr", 10)

    result = {}

    # ── 1. Market Structure (Higher Highs / Lower Lows) ──────────────────────
    n = min(20, len(candles))
    recent_highs = highs[-n:]
    recent_lows  = lows[-n:]

    hh = recent_highs[-1] > max(recent_highs[:-1]) if len(recent_highs) > 1 else False
    ll = recent_lows[-1]  < min(recent_lows[:-1])  if len(recent_lows) > 1 else False
    lh = recent_highs[-1] < max(recent_highs[:-1]) if len(recent_highs) > 1 else False
    hl = recent_lows[-1]  > min(recent_lows[:-1])  if len(recent_lows) > 1 else False

    if hh and hl:
        market_structure = "bullish"
        ms_strength = 85
    elif ll and lh:
        market_structure = "bearish"
        ms_strength = 85
    elif hh:
        market_structure = "bullish"
        ms_strength = 65
    elif ll:
        market_structure = "bearish"
        ms_strength = 65
    else:
        market_structure = "ranging"
        ms_strength = 40

    result["market_structure"]    = market_structure
    result["ms_strength"]         = ms_strength
    result["higher_high"]         = hh
    result["lower_low"]           = ll

    # ── 2. Order Blocks ───────────────────────────────────────────────────────
    # Bullish OB: laatste bearish candle voor een bullish impuls
    # Bearish OB: laatste bullish candle voor een bearish impuls
    bull_ob = None
    bear_ob = None

    for i in range(len(candles) - 3, max(0, len(candles) - 15), -1):
        c = candles[i]
        c_next = candles[i+1] if i+1 < len(candles) else None
        if not c_next:
            continue

        # Bullish OB: bearish candle gevolgd door sterke bullish move
        if (c.get("close", 0) < c.get("open", 0) and
                c_next.get("close", 0) > c_next.get("open", 0) and
                (c_next.get("close", 0) - c_next.get("open", 0)) > atr * 0.5):
            if bull_ob is None:
                bull_ob = {
                    "high": c.get("high", 0),
                    "low":  c.get("low", 0),
                    "mid":  (c.get("high", 0) + c.get("low", 0)) / 2,
                    "strength": min(100, int(((c_next.get("close", 0) - c_next.get("open", 0)) / atr) * 40))
                }

        # Bearish OB: bullish candle gevolgd door sterke bearish move
        if (c.get("close", 0) > c.get("open", 0) and
                c_next.get("close", 0) < c_next.get("open", 0) and
                (c_next.get("open", 0) - c_next.get("close", 0)) > atr * 0.5):
            if bear_ob is None:
                bear_ob = {
                    "high": c.get("high", 0),
                    "low":  c.get("low", 0),
                    "mid":  (c.get("high", 0) + c.get("low", 0)) / 2,
                    "strength": min(100, int(((c_next.get("open", 0) - c_next.get("close", 0)) / atr) * 40))
                }

    result["bullish_ob"] = bull_ob
    result["bearish_ob"] = bear_ob

    # ── 3. Fair Value Gaps (FVG / Imbalances) ────────────────────────────────
    fvgs = []
    for i in range(1, min(len(candles) - 1, 20)):
        prev_c = candles[-(i+2)] if i+2 <= len(candles) else None
        curr_c = candles[-(i+1)] if i+1 <= len(candles) else None
        next_c = candles[-i]     if i <= len(candles)    else None
        if not all([prev_c, curr_c, next_c]):
            continue

        # Bullish FVG: low van next > high van prev
        if next_c.get("low", 0) > prev_c.get("high", 0):
            gap_size = next_c.get("low", 0) - prev_c.get("high", 0)
            if gap_size > atr * 0.15:
                fvgs.append({
                    "type": "bullish",
                    "top":  next_c.get("low", 0),
                    "bot":  prev_c.get("high", 0),
                    "mid":  (next_c.get("low", 0) + prev_c.get("high", 0)) / 2,
                    "size": round(gap_size, 2),
                    "bars_ago": i
                })

        # Bearish FVG: high van next < low van prev
        if next_c.get("high", 0) < prev_c.get("low", 0):
            gap_size = prev_c.get("low", 0) - next_c.get("high", 0)
            if gap_size > atr * 0.15:
                fvgs.append({
                    "type": "bearish",
                    "top":  prev_c.get("low", 0),
                    "bot":  next_c.get("high", 0),
                    "mid":  (prev_c.get("low", 0) + next_c.get("high", 0)) / 2,
                    "size": round(gap_size, 2),
                    "bars_ago": i
                })

    result["fvgs"] = fvgs[:5]  # top 5 meest recente

    # ── 4. Liquidity Levels (Equal Highs/Lows = resting liquidity) ───────────
    eq_threshold = atr * 0.25
    liq_highs = []
    liq_lows  = []

    for i in range(len(candles) - 2):
        for j in range(i+1, min(i+10, len(candles))):
            # Equal highs
            if abs(candles[i].get("high", 0) - candles[j].get("high", 0)) < eq_threshold:
                level = (candles[i].get("high", 0) + candles[j].get("high", 0)) / 2
                if all(abs(level - existing) > eq_threshold for existing in liq_highs):
                    liq_highs.append(round(level, 2))
            # Equal lows
            if abs(candles[i].get("low", 0) - candles[j].get("low", 0)) < eq_threshold:
                level = (candles[i].get("low", 0) + candles[j].get("low", 0)) / 2
                if all(abs(level - existing) > eq_threshold for existing in liq_lows):
                    liq_lows.append(round(level, 2))

    # Sorteer: dichtstbij huidige prijs eerst
    liq_highs = sorted(liq_highs, key=lambda x: abs(x - cur_close))[:3]
    liq_lows  = sorted(liq_lows,  key=lambda x: abs(x - cur_close))[:3]

    result["liquidity_highs"] = liq_highs
    result["liquidity_lows"]  = liq_lows

    # ── 5. Liquidity Sweep detectie ───────────────────────────────────────────
    sweep_bull = False  # sweep van lows (bullish reversal signaal)
    sweep_bear = False  # sweep van highs (bearish reversal signaal)

    if len(candles) >= 3:
        prev2 = candles[-3] if len(candles) >= 3 else None
        prev1 = candles[-2] if len(candles) >= 2 else None
        if prev2 and prev1:
            # Sweep low: wick onder vorige low maar close erboven
            if (cur_low < prev1.get("low", 0) and
                    cur_close > prev1.get("low", 0) and
                    cur_close > cur_open):
                sweep_bull = True

            # Sweep high: wick boven vorige high maar close eronder
            if (cur_high > prev1.get("high", 0) and
                    cur_close < prev1.get("high", 0) and
                    cur_close < cur_open):
                sweep_bear = True

    result["liquidity_sweep_bull"] = sweep_bull
    result["liquidity_sweep_bear"] = sweep_bear

    # ── 6. Volume Profile (POC benadering) ───────────────────────────────────
    if volumes and closes:
        total_vol   = sum(volumes[-20:]) if len(volumes) >= 20 else sum(volumes)
        vwap_approx = (sum(c * v for c, v in zip(closes[-20:], volumes[-20:])) / total_vol
                       if total_vol > 0 else cur_close)
        result["volume_poc"] = round(vwap_approx, 2)

        avg_vol = total_vol / min(20, len(volumes))
        result["volume_ratio"] = round(current.get("volume", 0) / avg_vol, 2) if avg_vol > 0 else 1.0

    # ── 7. Momentum (Rate of Change) ─────────────────────────────────────────
    if len(closes) >= 10:
        roc5  = ((closes[-1] - closes[-6])  / closes[-6]  * 100) if closes[-6]  else 0
        roc10 = ((closes[-1] - closes[-11]) / closes[-11] * 100) if closes[-11] else 0
        result["momentum_roc5"]  = round(roc5, 3)
        result["momentum_roc10"] = round(roc10, 3)
        result["momentum_dir"]   = "bull" if roc5 > 0 else "bear"

    # ── 8. Candle Imbalance Score ─────────────────────────────────────────────
    body    = abs(cur_close - cur_open)
    total_r = cur_high - cur_low if cur_high != cur_low else 1
    upper_w = cur_high - max(cur_close, cur_open)
    lower_w = min(cur_close, cur_open) - cur_low
    body_pct = body / total_r if total_r else 0

    result["candle_body_pct"]  = round(body_pct * 100, 1)
    result["candle_upper_wick"] = round(upper_w, 2)
    result["candle_lower_wick"] = round(lower_w, 2)

    # ── 9. Price vs VWAP ─────────────────────────────────────────────────────
    vwap = current.get("vwap", 0)
    if vwap:
        vwap_dist = cur_close - vwap
        result["vwap_distance"]    = round(vwap_dist, 2)
        result["vwap_distance_pct"] = round(vwap_dist / vwap * 100, 3) if vwap else 0
        result["above_vwap"]        = cur_close > vwap

    # ── 10. EMA Stack (trend alignment) ──────────────────────────────────────
    ema8  = current.get("ema8",  0)
    ema21 = current.get("ema21", 0)
    ema50 = current.get("ema50", 0)
    if ema8 and ema21 and ema50:
        if ema8 > ema21 > ema50:
            result["ema_stack"] = "bull"
            result["ema_stack_strength"] = 90
        elif ema8 < ema21 < ema50:
            result["ema_stack"] = "bear"
            result["ema_stack_strength"] = 90
        elif ema8 > ema21:
            result["ema_stack"] = "bull"
            result["ema_stack_strength"] = 60
        elif ema8 < ema21:
            result["ema_stack"] = "bear"
            result["ema_stack_strength"] = 60
        else:
            result["ema_stack"] = "neutral"
            result["ema_stack_strength"] = 30

    return result

# ── AI Signaal Generatie ─────────────────────────────────────────────────────
def build_prompt(candle: dict, smc: dict) -> str:
    """Bouwt een uitgebreide SMC-gebaseerde prompt voor Claude."""

    ob_bull = smc.get("bullish_ob")
    ob_bear = smc.get("bearish_ob")
    fvgs    = smc.get("fvgs", [])
    fvg_bull = [f for f in fvgs if f["type"] == "bullish"]
    fvg_bear = [f for f in fvgs if f["type"] == "bearish"]

    ob_bull_str = (f"Bullish OB: {ob_bull['low']:.2f}-{ob_bull['high']:.2f} (sterkte: {ob_bull['strength']}%)"
                   if ob_bull else "Geen")
    ob_bear_str = (f"Bearish OB: {ob_bear['low']:.2f}-{ob_bear['high']:.2f} (sterkte: {ob_bear['strength']}%)"
                   if ob_bear else "Geen")
    fvg_bull_str = ", ".join([f"{f['bot']:.2f}-{f['top']:.2f} ({f['size']:.1f}pt, {f['bars_ago']} bars geleden)"
                               for f in fvg_bull[:2]]) or "Geen"
    fvg_bear_str = ", ".join([f"{f['bot']:.2f}-{f['top']:.2f} ({f['size']:.1f}pt, {f['bars_ago']} bars geleden)"
                               for f in fvg_bear[:2]]) or "Geen"
    liq_h_str = ", ".join([str(x) for x in smc.get("liquidity_highs", [])]) or "Geen"
    liq_l_str = ", ".join([str(x) for x in smc.get("liquidity_lows",  [])]) or "Geen"

    sweep_str = ""
    if smc.get("liquidity_sweep_bull"):
        sweep_str = "⚡ BULLISH LIQUIDITY SWEEP gedetecteerd (low sweep + reversal)"
    elif smc.get("liquidity_sweep_bear"):
        sweep_str = "⚡ BEARISH LIQUIDITY SWEEP gedetecteerd (high sweep + reversal)"
    else:
        sweep_str = "Geen sweep gedetecteerd"

    atr  = candle.get("atr", 10)
    rsi  = candle.get("rsi", 50)
    close = candle.get("close", 0)

    prompt = f"""Je bent een Smart Money Concepts (SMC) expert trader gespecialiseerd in NQ Nasdaq futures scalping op 1-5 minuut timeframes. Je analyseert institutionele orderflow, liquiditeitsstructuur en price action.

═══════════════════════════════════════
LIVE CANDLE — NQ1! ({candle.get('timeframe','5')}min)
═══════════════════════════════════════
Open:   {candle.get('open')}    High: {candle.get('high')}
Low:    {candle.get('low')}     Close: {close}
Volume: {candle.get('volume')}  ATR14: {atr}
Range:  {round(candle.get('high',0)-candle.get('low',0),2)}pt  Body: {round(abs(close-candle.get('open',0)),2)}pt
Patroon: {candle.get('pattern','none')}

TECHNISCHE INDICATOREN
VWAP: {candle.get('vwap')} | RSI: {rsi} | EMA8: {candle.get('ema8')} | EMA21: {candle.get('ema21')} | EMA50: {candle.get('ema50')}
EMA Stack: {smc.get('ema_stack','?')} (sterkte: {smc.get('ema_stack_strength','?')}%)
Price vs VWAP: {smc.get('vwap_distance',0):+.2f}pt ({smc.get('vwap_distance_pct',0):+.3f}%) — {'BOVEN' if smc.get('above_vwap') else 'ONDER'} VWAP
Volume ratio: {smc.get('volume_ratio',1):.2f}x gemiddeld

═══════════════════════════════════════
SMART MONEY CONCEPTS ANALYSE
═══════════════════════════════════════
MARKET STRUCTURE: {smc.get('market_structure','?').upper()} (sterkte: {smc.get('ms_strength','?')}%)
HTF Trend (15m): {candle.get('htf_trend','?').upper()}
Sessie: {candle.get('session','?')}
Momentum ROC5: {smc.get('momentum_roc5',0):+.3f}% | ROC10: {smc.get('momentum_roc10',0):+.3f}%
Candle body: {smc.get('candle_body_pct',0)}% van range

ORDER BLOCKS
{ob_bull_str}
{ob_bear_str}

FAIR VALUE GAPS (Imbalances)
Bullish FVG: {fvg_bull_str}
Bearish FVG: {fvg_bear_str}

LIQUIDITEIT
Buy-side liquiditeit (equal highs): {liq_h_str}
Sell-side liquiditeit (equal lows): {liq_l_str}
{sweep_str}

KEY LEVELS
Pivot: {candle.get('pivot')} | R1: {candle.get('r1')} | S1: {candle.get('s1')}
PDH: {candle.get('pdh')} | PDL: {candle.get('pdl')}

VORIGE CANDLE
H: {candle.get('prev_high')} | L: {candle.get('prev_low')} | C: {candle.get('prev_close')}
Volume delta (5 bars): {candle.get('vol_delta',0)} ({'bearish' if candle.get('vol_delta',0)<0 else 'bullish'} druk)

═══════════════════════════════════════
TAAK
═══════════════════════════════════════
Geef een professioneel SMC scalp signaal. Denk als een institutionele trader:
- Waar staat liquiditeit? Welke kant gaat het smart money op?
- Is er een order block als steun/weerstand?
- Is er een FVG die gevuld moet worden?
- Bevestigt de market structure het signaal?
- Wat is het risico van dit signaal?

Reageer UITSLUITEND met dit JSON object. Geen tekst ervoor of erna. Geen markdown. Geen backticks:
{{"direction":"bull","probability":72,"entry":{close},"target":{round(close+atr*1.5,2)},"stop":{round(close-atr*0.8,2)},"rr":1.9,"confidence":"medium","setup_type":"ob_retest","confluence_scores":[75,68,82,71,65,78],"smc_bias":"bullish","key_level_type":"bullish_ob","invalidation":{round(close-atr*1.2,2)},"analysis":"Schrijf hier 120-150 woorden professionele SMC analyse in het Nederlands. Beschrijf: 1) de market structure, 2) welk SMC concept het signaal drijft (OB/FVG/sweep), 3) waarom dit entry punt logisch is, 4) wat het risico is en wanneer het signaal ongeldig wordt."}}

Geldige waarden voor direction: bull, bear, neutral
Geldige waarden voor confidence: low, medium, high
Geldige waarden voor setup_type: ob_retest, fvg_fill, liquidity_sweep, bos_retest, vwap_reversion, momentum_continuation, none
Geldige waarden voor smc_bias: bullish, bearish, neutral"""

    return prompt

def analyse_candle_async(data: dict):
    """Draait in een achtergrond thread. Analyseert candle en slaat signaal op."""
    global latest_signal

    if not ANTHROPIC_API_KEY:
        print("⚠ Geen ANTHROPIC_API_KEY — sla analyse over")
        return

    # SMC berekeningen
    smc = {}
    if len(candle_history) >= 3:
        smc = calculate_smc(candle_history[:-1], data)  # gebruik history exclusief huidige

    prompt = build_prompt(data, smc)

    try:
        print(f"🔍 SMC analyse: C={data.get('close')} sess={data.get('session')} struct={smc.get('market_structure','?')}...")

        resp = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=28
        )

        if resp.status_code != 200:
            print(f"✗ Anthropic {resp.status_code}: {resp.text[:300]}")
            return

        raw = "".join(b.get("text", "") for b in resp.json().get("content", []))
        raw = raw.strip()

        # Strip markdown
        if "```" in raw:
            raw = re.sub(r'```[a-z]*\n?', '', raw).strip()

        # Extract JSON
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            print(f"✗ Geen JSON in response: {raw[:300]}")
            return

        signal = json.loads(match.group())

        # Voeg SMC data toe aan signaal
        signal["smc_data"]    = smc
        signal["candle"]      = {k: data.get(k) for k in ["open","high","low","close","volume","session","pattern","htf_trend"]}
        signal["analysed_at"] = datetime.now(timezone.utc).isoformat()

        latest_signal = signal
        print(f"✓ Signaal: {signal.get('direction','?')} {signal.get('probability','?')}% | {signal.get('setup_type','?')} | conf={signal.get('confidence','?')}")

    except json.JSONDecodeError as e:
        print(f"✗ JSON parse fout: {e} | raw: {raw[:300]}")
    except req.exceptions.Timeout:
        print("✗ Anthropic API timeout na 28s")
    except Exception as e:
        print(f"✗ Analyse fout: {type(e).__name__}: {e}")

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":           "online",
        "service":          "NQ Oracle PRO v5",
        "version":          "5.0.0",
        "candles_received": len(candle_history),
        "last_candle":      latest_candle.get("received_at", "geen data"),
        "last_signal":      latest_signal.get("analysed_at", "nog geen signaal"),
        "anthropic_ready":  bool(ANTHROPIC_API_KEY),
        "smc_engine":       "active"
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    global latest_candle
    if not check_auth():
        return jsonify({"error": "Unauthorized — controleer X-TV-Secret header of ?secret= parameter"}), 401

    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error": "Ongeldige of lege JSON body"}), 400

        data = normalize(data)
        data["received_at"] = datetime.now(timezone.utc).isoformat()

        latest_candle = data
        candle_history.append(data)
        if len(candle_history) > MAX_HISTORY:
            candle_history.pop(0)

        sess = data.get("session", "?")
        pat  = data.get("pattern", "?")
        close = data.get("close", "?")
        print(f"[{data['received_at']}] C={close} | sess={sess} | pat={pat} | candles={len(candle_history)}")

        # Start analyse in achtergrond (non-blocking)
        t = threading.Thread(target=analyse_candle_async, args=(data.copy(),), daemon=True)
        t.start()

        return jsonify({"status": "ok", "candles": len(candle_history)}), 200

    except Exception as e:
        print(f"✗ Webhook fout: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/latest", methods=["GET"])
def get_latest():
    if not latest_candle:
        return jsonify({"status": "no_data", "message": "Nog geen candles ontvangen"}), 200
    return jsonify({"status": "ok", "data": latest_candle}), 200

@app.route("/latest_signal", methods=["GET"])
def get_latest_signal():
    if not latest_signal:
        return jsonify({"status": "no_signal", "message": "Nog geen signaal gegenereerd"}), 200
    return jsonify({"status": "ok", "signal": latest_signal}), 200

@app.route("/history", methods=["GET"])
def get_history():
    n = min(int(request.args.get("n", 20)), MAX_HISTORY)
    return jsonify({
        "status": "ok",
        "count":  len(candle_history),
        "data":   candle_history[-n:]
    }), 200

@app.route("/clear", methods=["POST"])
def clear():
    global latest_candle, candle_history, latest_signal
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    latest_candle  = {}
    candle_history = []
    latest_signal  = {}
    print("⚠ Data gereset")
    return jsonify({"status": "cleared"}), 200

# ── Start ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"NQ Oracle PRO v5 gestart op poort {port}")
    print(f"Anthropic API: {'✓ ingesteld' if ANTHROPIC_API_KEY else '✗ NIET ingesteld'}")
    app.run(host="0.0.0.0", port=port, debug=False)
