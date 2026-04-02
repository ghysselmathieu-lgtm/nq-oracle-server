"""
NQ Oracle Webhook Server
Ontvangt TradingView alerts en serveert data naar het dashboard
Deploy op Railway: https://railway.app
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
import json
import os

app = Flask(__name__)
CORS(app)  # Staat dashboard toe om data op te halen

# In-memory opslag (Railway herstart soms containers, maar voor realtime is dit prima)
latest_candle = {}
candle_history = []  # Laatste 50 candles
MAX_HISTORY = 50

# Optioneel: simpel secret token om ongewenste requests te blokkeren
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "nq-oracle-secret")

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "online",
        "service": "NQ Oracle Webhook Server",
        "candles_received": len(candle_history),
        "last_update": latest_candle.get("received_at", "geen data nog")
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Ontvangt TradingView alerts.
    TradingView stuurt JSON in de alert message body.
    """
    global latest_candle

    # Check secret token (optioneel maar aanbevolen)
    token = request.headers.get("X-TV-Secret") or request.args.get("secret")
    if token != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "Geen JSON data ontvangen"}), 400

        # Voeg timestamp toe
        data["received_at"] = datetime.now(timezone.utc).isoformat()

        # Sla op
        latest_candle = data
        candle_history.append(data)

        # Beperk history
        if len(candle_history) > MAX_HISTORY:
            candle_history.pop(0)

        print(f"[{data['received_at']}] Candle ontvangen: O={data.get('open')} H={data.get('high')} L={data.get('low')} C={data.get('close')} V={data.get('volume')}")
        return jsonify({"status": "ok", "received": data}), 200

    except Exception as e:
        print(f"Fout: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/latest", methods=["GET"])
def get_latest():
    """Dashboard pollt dit endpoint voor nieuwe candle data."""
    if not latest_candle:
        return jsonify({"status": "no_data"}), 200
    return jsonify({"status": "ok", "data": latest_candle}), 200

@app.route("/history", methods=["GET"])
def get_history():
    """Geeft laatste N candles terug."""
    n = min(int(request.args.get("n", 10)), MAX_HISTORY)
    return jsonify({
        "status": "ok",
        "count": len(candle_history),
        "data": candle_history[-n:]
    }), 200

@app.route("/clear", methods=["POST"])
def clear():
    """Reset data (handig bij nieuwe tradingdag)."""
    global latest_candle, candle_history
    token = request.headers.get("X-TV-Secret") or request.args.get("secret")
    if token != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    latest_candle = {}
    candle_history = []
    return jsonify({"status": "cleared"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"NQ Oracle Server gestart op poort {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
