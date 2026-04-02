"""
NQ Oracle Webhook Server v2
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
import os

app = Flask(__name__)
CORS(app)

latest_candle = {}
candle_history = []
MAX_HISTORY = 50
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "nq-oracle-secret")

def normalize(data):
    """Vertaal verkorte Pine Script veldnamen naar volledige namen."""
    rename = {
        "ph": "prev_high", "pl": "prev_low",
        "pc": "prev_close", "pv": "prev_volume",
        "vd": "vol_delta"
    }
    for short, full in rename.items():
        if short in data and full not in data:
            data[full] = data.pop(short)

    if "htf" in data and "htf_trend" not in data:
        data["htf_trend"] = "bull" if int(data.pop("htf")) == 1 else "bear"

    if "sess" in data and "session" not in data:
        sess_map = {0:"premarket", 1:"open", 2:"midday", 3:"close"}
        data["session"] = sess_map.get(int(data.pop("sess")), "unknown")

    if "pat" in data and "pattern" not in data:
        pat_map = {0:"none", 1:"bullish_engulfing", 2:"bearish_engulfing", 3:"doji", 4:"hammer"}
        data["pattern"] = pat_map.get(int(data.pop("pat")), "none")

    return data

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "online",
        "service": "NQ Oracle Webhook Server v2",
        "candles_received": len(candle_history),
        "last_update": latest_candle.get("received_at", "geen data nog")
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    global latest_candle
    token = request.headers.get("X-TV-Secret") or request.args.get("secret")
    if token != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "Geen JSON data"}), 400
        data = normalize(data)
        data["received_at"] = datetime.now(timezone.utc).isoformat()
        latest_candle = data
        candle_history.append(data)
        if len(candle_history) > MAX_HISTORY:
            candle_history.pop(0)
        print(f"[{data['received_at']}] O={data.get('open')} H={data.get('high')} L={data.get('low')} C={data.get('close')} sess={data.get('session')} pat={data.get('pattern')}")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/latest", methods=["GET"])
def get_latest():
    if not latest_candle:
        return jsonify({"status": "no_data"}), 200
    return jsonify({"status": "ok", "data": latest_candle}), 200

@app.route("/history", methods=["GET"])
def get_history():
    n = min(int(request.args.get("n", 10)), MAX_HISTORY)
    return jsonify({"status": "ok", "count": len(candle_history), "data": candle_history[-n:]}), 200

@app.route("/clear", methods=["POST"])
def clear():
    global latest_candle, candle_history
    token = request.headers.get("X-TV-Secret") or request.args.get("secret")
    if token != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    latest_candle = {}
    candle_history = []
    return jsonify({"status": "cleared"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
