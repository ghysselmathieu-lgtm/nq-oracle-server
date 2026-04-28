"""
DAX Oracle Master Server v1 — minimal candle store
============================================================
Geen AI, geen subjective signaal-engine.
Dit is alleen een receiver + opslag voor 1-min candles uit TradingView.
De signaal-engine zit in de frontend (dax-oracle-live.html) en gebruikt
EXACT dezelfde v13 logica als dax-oracle-master-v1.html backtest.

→ Backtest signaal == Live signaal (zelfde code path).
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
import os, json
from pathlib import Path

app = Flask(__name__)
CORS(app, origins="*")

# ─── State (in-memory) ───────────────────────────────────────
candle_history = []        # 1-min candles (laatste MAX_HISTORY)
tf_data        = {"1":{},"5":{},"15":{},"30":{},"60":{}}  # MTF snapshot
predictions    = []        # signalen + outcomes voor cross-session tracking
order_history  = []        # uitgevoerde orders

MAX_HISTORY     = 100000   # ~69 dagen 1-min data (geen praktische limiet)
MAX_PREDICTIONS = 5000

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dax-oracle-secret")
DATA_DIR       = Path("/tmp")
CANDLES_FILE   = DATA_DIR / "nq_candles.json"
PRED_FILE      = DATA_DIR / "dax_predictions.json"

# ─── Persistence (overleeft Railway restart) ─────────────────
def save_state():
    try:
        CANDLES_FILE.write_text(json.dumps({
            "candles": candle_history[-MAX_HISTORY:],
            "tf_data": tf_data
        }, default=str))
    except Exception as e:
        print(f"⚠ save_state: {e}")

def save_preds():
    try:
        PRED_FILE.write_text(json.dumps(predictions[-MAX_PREDICTIONS:], default=str))
    except Exception as e:
        print(f"⚠ save_preds: {e}")

def load_state():
    global candle_history, tf_data, predictions
    try:
        if CANDLES_FILE.exists():
            d = json.loads(CANDLES_FILE.read_text())
            candle_history = d.get("candles", [])
            tf_data        = d.get("tf_data", tf_data)
            print(f"✓ Loaded {len(candle_history)} candles")
    except Exception as e:
        print(f"⚠ load candles: {e}")
    try:
        if PRED_FILE.exists():
            predictions = json.loads(PRED_FILE.read_text())
            print(f"✓ Loaded {len(predictions)} predictions")
    except Exception as e:
        print(f"⚠ load preds: {e}")

load_state()

# ─── Auth ────────────────────────────────────────────────────
def check_auth():
    t = (request.headers.get("X-TV-Secret") or
         request.headers.get("x-tv-secret") or
         request.args.get("secret",""))
    return t == WEBHOOK_SECRET

# ─── Pine Script payload normalize ───────────────────────────
def normalize(data):
    """Pine stuurt korte keys. Vertaal naar volledige keys."""
    renames = {
        "o":"open","h":"high","l":"low","c":"close","v":"volume",
        "vw":"vwap","e8":"ema8","e21":"ema21","e50":"ema50",
        "rs":"rsi","at":"atr","pv":"pivot","r1":"r1","s1":"s1",
        "ph":"pdh","pl":"pdl","vd":"vol_delta","ht":"htf",
        "ss":"sess","pt":"pat","ph1":"prev_high","pl1":"prev_low",
        "pc1":"prev_close","pv1":"prev_volume"
    }
    for s,f in renames.items():
        if s in data and f not in data:
            data[f] = data.pop(s)

    # MTF arrays unpacken
    tf_result = {}
    for key,tf in [("m1","1"),("m5","5"),("m15","15"),("m30","30"),("m60","60")]:
        if key in data:
            try:
                raw = str(data.pop(key))
                parts = raw.split(",")
                if len(parts) >= 8:
                    tf_result[tf] = {
                        "open":   float(parts[0]),
                        "high":   float(parts[1]),
                        "low":    float(parts[2]),
                        "close":  float(parts[3]),
                        "volume": float(parts[4]),
                        "rsi":    float(parts[5]),
                        "ema21":  float(parts[6]),
                        "atr":    float(parts[7])
                    }
            except: pass

    # HTF + sessie + patroon decoderen
    if "htf" in data:
        data["htf_trend"] = "bull" if str(data.pop("htf")) in ("1","True","true") else "bear"
    if "sess" in data:
        sm = {0:"premarket",1:"open",2:"midday",3:"close"}
        try: data["session"] = sm.get(int(float(data.pop("sess"))),"unknown")
        except: data.pop("sess",None)
    if "pat" in data:
        pm = {0:"none",1:"bullish_engulfing",2:"bearish_engulfing",3:"doji",4:"hammer",5:"shooting_star"}
        try: data["pattern"] = pm.get(int(float(data.pop("pat"))),"none")
        except: data.pop("pat",None)

    # Cast naar float
    for k in ["open","high","low","close","volume","vwap","ema8","ema21","ema50",
              "rsi","atr","pivot","r1","s1","pdh","pdl","vol_delta",
              "prev_high","prev_low","prev_close","prev_volume"]:
        if k in data:
            try: data[k] = float(data[k])
            except: pass

    data["_tf_data"] = tf_result
    return data

# ─── Routes ──────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    last_ts = candle_history[-1].get("received_at") if candle_history else None
    return jsonify({
        "status":"online",
        "service":"DAX Oracle Master Server v1 (no-AI, deterministic engine in client)",
        "candles": len(candle_history),
        "predictions": len(predictions),
        "open_predictions": sum(1 for p in predictions if p.get("outcome") == "open"),
        "last_candle_at": last_ts,
        "auth_required": True
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    """Ontvangt 1-min candle van Pine Script.
    Gebruikt Pine's 'bt' (bar time UNIX ms) als authoritatieve bucket-key.
    Dit voorkomt het probleem waar Pine soms dezelfde bar 2× verzendt
    (bijv. bij chart refresh) of waar webhook-arrival-time afwijkt van bar-time."""
    global candle_history, tf_data
    if not check_auth():
        return jsonify({"error":"Unauthorized"}), 401

    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error":"No JSON"}), 400

        # ─── Bepaal bar_time VÓÓR normalize (bt is short key uit Pine) ───
        bar_time_ms = None
        if "bt" in data:
            try:
                bar_time_ms = int(data.pop("bt"))
            except: pass

        data = normalize(data)
        tf_raw = data.pop("_tf_data", {})
        for tf,c in tf_raw.items():
            if c: tf_data[tf] = c

        now = datetime.now(timezone.utc)
        data["received_at"] = now.isoformat()

        # ─── Bucket = ECHTE bar-tijd uit Pine (UTC), niet wall-clock ───
        if bar_time_ms is not None:
            bar_dt = datetime.fromtimestamp(bar_time_ms / 1000, tz=timezone.utc)
            bucket = bar_dt.strftime("%Y-%m-%d %H:%M")
            data["bar_time"] = bar_dt.isoformat()
        else:
            # Fallback voor oude Pine zonder bt: gebruik now afgerond op minuut
            bucket = now.strftime("%Y-%m-%d %H:%M")
        data["bucket"] = bucket

        # ─── DEDUP: zoek in HELE history naar deze bucket, niet alleen laatste ───
        # (Pine kan oude bars opnieuw verzenden bij refresh — die moeten ook gevangen worden)
        existing_idx = None
        for i in range(len(candle_history) - 1, max(-1, len(candle_history) - 10), -1):
            if candle_history[i].get("bucket") == bucket:
                existing_idx = i
                break

        if existing_idx is not None:
            # Bestaande bucket → vervang in-place (zelfde positie, geen reorder)
            candle_history[existing_idx] = data
        else:
            # Nieuwe bucket → append + sorteer (om out-of-order Pine-bursts op te vangen)
            candle_history.append(data)
            candle_history.sort(key=lambda x: x.get("bucket", ""))
            if len(candle_history) > MAX_HISTORY:
                candle_history = candle_history[-MAX_HISTORY:]

        if len(candle_history) % 5 == 0:
            save_state()

        print(f"[{bucket}] C={data.get('close')} | bar_t={bar_time_ms} | dedup={'YES' if existing_idx is not None else 'NEW'}")
        return jsonify({"status":"ok","candles":len(candle_history),"bucket":bucket}), 200

    except Exception as e:
        print(f"✗ webhook: {e}")
        return jsonify({"error":str(e)}), 500

@app.route("/import_history", methods=["POST"])
def import_history():
    """Importeer historische 1-min candles (bv. uit TradingView CSV export).
    Body: { "candles": [{t, o, h, l, c, v}, ...] }
    Verwacht t = "YYYY-MM-DD HH:MM" string (of UNIX timestamp seconds).
    Dedupeert op bucket en MERGED met bestaande live candles."""
    global candle_history
    if not check_auth():
        return jsonify({"error":"Unauthorized"}), 401

    try:
        body = request.get_json(force=True, silent=True)
        if not body or "candles" not in body:
            return jsonify({"error":"No candles array"}), 400

        # Bouw bestaande bucket-index
        existing_buckets = set(c.get("bucket") for c in candle_history if c.get("bucket"))

        added = 0
        for c in body["candles"]:
            ts = c.get("t", "")
            # UNIX timestamp → string
            if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
                ts_int = int(ts)
                dt = datetime.fromtimestamp(ts_int, tz=timezone.utc)
                bucket = dt.strftime("%Y-%m-%d %H:%M")
            else:
                # Verwacht "YYYY-MM-DD HH:MM" of "YYYY-MM-DDTHH:MM..."
                bucket = str(ts)[:16].replace("T", " ")

            if bucket in existing_buckets:
                continue

            entry = {
                "open":   float(c.get("o", c.get("open", 0))),
                "high":   float(c.get("h", c.get("high", 0))),
                "low":    float(c.get("l", c.get("low", 0))),
                "close":  float(c.get("c", c.get("close", 0))),
                "volume": float(c.get("v", c.get("volume", 0))),
                "bucket": bucket,
                "received_at": bucket + ":00+00:00",  # Pseudo-iso voor history
                "is_history": True
            }
            candle_history.append(entry)
            existing_buckets.add(bucket)
            added += 1

        # Sorteer chronologisch zodat history vóór live komt
        candle_history.sort(key=lambda x: x.get("bucket", ""))
        if len(candle_history) > MAX_HISTORY:
            candle_history = candle_history[-MAX_HISTORY:]

        save_state()
        return jsonify({
            "status":"ok",
            "added": added,
            "skipped": len(body["candles"]) - added,
            "total": len(candle_history)
        }), 200
    except Exception as e:
        print(f"✗ import_history: {e}")
        return jsonify({"error":str(e)}), 500

@app.route("/latest", methods=["GET"])
def get_latest():
    """Laatste candle + MTF snapshot (voor MTF strip in dashboard)."""
    if not candle_history:
        return jsonify({"status":"no_data"}), 200
    return jsonify({
        "status":"ok",
        "data":    candle_history[-1],
        "tf_data": tf_data
    }), 200

@app.route("/candles", methods=["GET"])
def get_candles():
    """Geeft de laatste N candles terug in OHLCV+t format dat de frontend
    direct kan gebruiken in zijn rolling-HTF backtest engine.
    Gebruikt 'bucket' (UTC minuut) als t — dedup gegarandeerd."""
    n = min(int(request.args.get("n", 500)), MAX_HISTORY)
    out = []
    seen = set()
    for c in candle_history[-n:]:
        # Bucket is bron van waarheid (gezet door webhook + import_history)
        ts = c.get("bucket")
        if not ts:
            # Fallback voor oude entries zonder bucket
            try:
                dt = datetime.fromisoformat(c["received_at"].replace("Z","+00:00"))
                ts = dt.strftime("%Y-%m-%d %H:%M")
            except:
                ts = c.get("received_at","")[:16].replace("T"," ")
        if ts in seen:
            continue  # Extra safety dedup
        seen.add(ts)
        out.append({
            "t": ts,
            "o": float(c.get("open", 0)),
            "h": float(c.get("high", 0)),
            "l": float(c.get("low", 0)),
            "c": float(c.get("close", 0)),
            "v": float(c.get("volume", 0))
        })

    return jsonify({
        "status":"ok",
        "count": len(out),
        "data":  out
    }), 200

@app.route("/predictions", methods=["GET"])
def get_predictions():
    """Voor cross-session prediction continuity (optioneel)."""
    n = int(request.args.get("n", 100))
    return jsonify({
        "status":"ok",
        "predictions": predictions[-n:],
        "total": len(predictions)
    }), 200

@app.route("/predictions", methods=["POST"])
def add_prediction():
    """Frontend stuurt nieuw signaal voor server-side persistentie."""
    if not check_auth():
        return jsonify({"error":"Unauthorized"}), 401
    try:
        p = request.get_json(force=True, silent=True)
        if not p:
            return jsonify({"error":"No JSON"}), 400

        # Auto-id + timestamp
        if "id" not in p:
            p["id"] = (predictions[-1]["id"] + 1) if predictions else 1
        if "created_at" not in p:
            p["created_at"] = datetime.now(timezone.utc).isoformat()
        if "outcome" not in p:
            p["outcome"] = "open"

        predictions.append(p)
        if len(predictions) > MAX_PREDICTIONS:
            predictions.pop(0)
        save_preds()
        return jsonify({"status":"ok","id":p["id"]}), 200
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/predictions/<int:pid>", methods=["PATCH"])
def update_prediction(pid):
    """Update outcome (WIN/LOSS/EXPIRED) van een prediction."""
    if not check_auth():
        return jsonify({"error":"Unauthorized"}), 401
    try:
        update = request.get_json(force=True, silent=True) or {}
        for p in predictions:
            if p.get("id") == pid:
                for k,v in update.items():
                    p[k] = v
                save_preds()
                return jsonify({"status":"ok","id":pid}), 200
        return jsonify({"error":"not_found"}), 404
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/execute", methods=["POST"])
def execute():
    """Registreer een manuele order voor logging."""
    if not check_auth():
        return jsonify({"error":"Unauthorized"}), 401
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error":"No data"}), 400
        entry  = float(data.get("entry", 0))
        stop   = float(data.get("stop",  0))
        target = float(data.get("target",0))
        contracts = int(data.get("contracts", 1))
        action = data.get("action", "long")

        risk_pts   = abs(entry - stop)
        risk_usd   = risk_pts * 2 * contracts   # MNQ: $2/punt
        profit_usd = abs(target - entry) * 2 * contracts
        rr = abs(target - entry) / max(risk_pts, 0.01)

        order = {
            "action":     action,
            "entry":      round(entry, 2),
            "stop":       round(stop, 2),
            "target":     round(target, 2),
            "contracts":  contracts,
            "rr":         round(rr, 2),
            "risk_pts":   round(risk_pts, 2),
            "risk_usd":   round(risk_usd, 2),
            "profit_usd": round(profit_usd, 2),
            "status":     "pending",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        order_history.append(order)
        if len(order_history) > 200:
            order_history.pop(0)
        return jsonify({"status":"ok","order":order}), 200
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/clear", methods=["POST"])
def clear():
    """Reset state. Voorzichtig — predictions blijven."""
    global candle_history, tf_data
    if not check_auth():
        return jsonify({"error":"Unauthorized"}), 401
    keep_preds = request.args.get("keep_preds","1") == "1"
    candle_history = []
    tf_data = {"1":{},"5":{},"15":{},"30":{},"60":{}}
    save_state()
    if not keep_preds:
        predictions.clear()
        save_preds()
    return jsonify({
        "status":"cleared",
        "predictions_kept": len(predictions) if keep_preds else 0
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"DAX Oracle Master Server v1 — port {port}")
    print(f"Loaded: {len(candle_history)} candles, {len(predictions)} predictions")
    app.run(host="0.0.0.0", port=port, debug=False)
