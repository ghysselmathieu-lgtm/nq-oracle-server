"""
NQ Oracle ELITE v8 — Rebuilt with proper SMC logic
Key changes:
- Only generates signals when FULL SMC confluence is met (fewer, better signals)
- Learns from last N outcomes via performance feedback in prompt  
- Persists predictions to disk (survive restarts)
- Stricter validation: no signal unless setup is clear
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
import os, threading, json, re, math, requests as req
from pathlib import Path

app = Flask(__name__)
CORS(app, origins="*")

# ── State ─────────────────────────────────────────────────────
candle_history   = []
latest_candle    = {}
latest_signal    = {}
tf_data          = {"1":{},"5":{},"15":{},"30":{},"60":{}}
predictions      = []
pending_order    = {}
order_history    = []
MAX_HISTORY      = 500
MAX_PREDICTIONS  = 10000  # Keep up to 10k predictions

WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "nq-oracle-secret")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATA_FILE         = Path("/tmp/nq_predictions.json")

# Signal stability
direction_lock = {"dir":"neutral","count":0,"entry":None,"stop":None,"target":None}
LOCK_THRESHOLD = 3

# ── Persistence ───────────────────────────────────────────────
def save_predictions():
    try:
        DATA_FILE.write_text(json.dumps(predictions[-MAX_PREDICTIONS:], default=str))
    except Exception as e:
        print(f"⚠ Save error: {e}")

def load_predictions():
    global predictions
    try:
        if DATA_FILE.exists():
            data = json.loads(DATA_FILE.read_text())
            predictions = data if isinstance(data, list) else []
            print(f"✓ Loaded {len(predictions)} predictions from disk")
    except Exception as e:
        print(f"⚠ Load error: {e}")

load_predictions()

# ── Auth ──────────────────────────────────────────────────────
def check_auth():
    t = (request.headers.get("X-TV-Secret") or
         request.headers.get("x-tv-secret") or
         request.args.get("secret",""))
    return t == WEBHOOK_SECRET

# ── Normalize ─────────────────────────────────────────────────
def normalize(data):
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

    tf_result = {}
    for key,tf in [("m1","1"),("m5","5"),("m15","15"),("m30","30"),("m60","60")]:
        if key in data:
            try:
                raw = str(data.pop(key))
                parts = raw.split(",")
                if len(parts)>=8:
                    tf_result[tf] = {"open":float(parts[0]),"high":float(parts[1]),
                        "low":float(parts[2]),"close":float(parts[3]),"volume":float(parts[4]),
                        "rsi":float(parts[5]),"ema21":float(parts[6]),"atr":float(parts[7])}
                elif len(parts)>=4:
                    tf_result[tf] = {"close":float(parts[0]),"open":float(parts[0]),
                        "rsi":float(parts[1]),"ema21":float(parts[2]),"atr":float(parts[3])}
            except: pass

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

    for k in ["open","high","low","close","volume","vwap","ema8","ema21","ema50",
              "rsi","atr","pivot","r1","s1","pdh","pdl","vol_delta",
              "prev_high","prev_low","prev_close","prev_volume"]:
        if k in data:
            try: data[k] = float(data[k])
            except: pass

    data["_tf_data"] = tf_result
    return data

# ── Advanced SMC Engine ───────────────────────────────────────
def calculate_smc(candles, current):
    if len(candles) < 10:
        return {}

    closes  = [c.get("close",0)  for c in candles]
    highs   = [c.get("high",0)   for c in candles]
    lows    = [c.get("low",0)    for c in candles]
    volumes = [c.get("volume",0) for c in candles]

    cur_c = current.get("close",0)
    cur_h = current.get("high",0)
    cur_l = current.get("low",0)
    cur_o = current.get("open",0)
    atr   = current.get("atr",10)
    vwap  = current.get("vwap",cur_c)
    result = {}

    # Market Structure
    n = min(30,len(candles))
    rh = highs[-n:]; rl = lows[-n:]
    hh = rh[-1]>max(rh[:-1]) if len(rh)>1 else False
    ll = rl[-1]<min(rl[:-1])  if len(rl)>1 else False
    lh = rh[-1]<max(rh[:-1]) if len(rh)>1 else False
    hl = rl[-1]>min(rl[:-1])  if len(rl)>1 else False

    if hh and hl:   ms,ms_s="bullish",90
    elif ll and lh: ms,ms_s="bearish",90
    elif hh:        ms,ms_s="bullish",65
    elif ll:        ms,ms_s="bearish",65
    else:           ms,ms_s="ranging",35

    result.update({"market_structure":ms,"ms_strength":ms_s,"hh":hh,"ll":ll,"lh":lh,"hl":hl})

    # Regime
    n20 = min(20,len(candles))
    ranges = [candles[-(i+1)].get("high",0)-candles[-(i+1)].get("low",0) for i in range(n20)]
    avg_range = sum(ranges)/len(ranges) if ranges else atr
    cur_range = cur_h-cur_l
    range_ratio = cur_range/avg_range if avg_range>0 else 1
    vwap_dist_atr = abs(cur_c-vwap)/atr if atr>0 else 0

    if ms in ("bullish","bearish") and range_ratio>1.3: regime="trending_strong"
    elif ms in ("bullish","bearish"): regime="trending_normal"
    elif range_ratio < 0.7: regime="compression"
    else: regime="ranging"

    result["regime"] = regime
    result["range_ratio"] = round(range_ratio,2)
    result["vwap_dist_atr"] = round(vwap_dist_atr,2)

    # VWAP Bands
    n_vwap = min(50,len(candles))
    vwap_prices = [c.get("vwap",0) for c in candles[-n_vwap:] if c.get("vwap",0)>0]
    if len(vwap_prices)>5:
        vwap_mean = sum(vwap_prices)/len(vwap_prices)
        vwap_var  = sum((x-vwap_mean)**2 for x in vwap_prices)/len(vwap_prices)
        vwap_std  = math.sqrt(vwap_var)
        result["vwap_band1_up"]   = round(vwap+vwap_std,2)
        result["vwap_band1_down"] = round(vwap-vwap_std,2)
        result["vwap_band2_up"]   = round(vwap+2*vwap_std,2)
        result["vwap_band2_down"] = round(vwap-2*vwap_std,2)
        result["vwap_std"]        = round(vwap_std,2)
        if cur_c > vwap+2*vwap_std:   result["vwap_zone"]="extreme_high"
        elif cur_c > vwap+vwap_std:   result["vwap_zone"]="high"
        elif cur_c > vwap:            result["vwap_zone"]="above"
        elif cur_c > vwap-vwap_std:   result["vwap_zone"]="below"
        elif cur_c > vwap-2*vwap_std: result["vwap_zone"]="low"
        else:                         result["vwap_zone"]="extreme_low"

    result["vwap"] = round(vwap,2)
    result["above_vwap"] = cur_c>vwap
    result["vwap_distance"] = round(cur_c-vwap,2)

    # Volume Profile POC
    n_vp = min(100,len(candles))
    if n_vp >= 10:
        all_closes = closes[-n_vp:]
        all_vols   = volumes[-n_vp:]
        price_min  = min(lows[-n_vp:])
        price_max  = max(highs[-n_vp:])
        if price_max > price_min:
            n_bins = 20
            bin_sz = (price_max-price_min)/n_bins
            bins   = [0.0]*n_bins
            for price,vol in zip(all_closes,all_vols):
                idx = min(int((price-price_min)/bin_sz), n_bins-1)
                bins[idx] += vol
            poc_idx = bins.index(max(bins))
            poc = round(price_min+(poc_idx+0.5)*bin_sz,2)
            total_vol = sum(bins)
            target_vol = total_vol*0.70
            sorted_bins = sorted(enumerate(bins),key=lambda x:-x[1])
            val_bins = set()
            cum = 0
            for idx,v in sorted_bins:
                if cum>=target_vol: break
                val_bins.add(idx); cum+=v
            val_idxs = sorted(val_bins)
            vah = round(price_min+(max(val_idxs)+1)*bin_sz,2) if val_idxs else price_max
            val_lvl = round(price_min+min(val_idxs)*bin_sz,2) if val_idxs else price_min
            result.update({"vp_poc":poc,"vp_vah":vah,"vp_val":val_lvl,
                          "price_vs_poc":"above" if cur_c>poc else "below"})

    # Cumulative Delta
    n_cd = min(20,len(candles))
    cum_delta = 0
    delta_history = []
    for c in candles[-n_cd:]:
        body = c.get("close",0)-c.get("open",0)
        vol  = c.get("volume",0)
        delta = vol if body>=0 else -vol
        cum_delta += delta
        delta_history.append(cum_delta)

    result["cumulative_delta"] = round(cum_delta,0)
    result["delta_trend"]      = "bull" if cum_delta>0 else "bear"
    if len(delta_history)>=5:
        price_up = closes[-1]>closes[-5] if len(closes)>=5 else True
        delta_up = delta_history[-1]>delta_history[-5]
        if price_up and not delta_up:   result["delta_divergence"]="bearish"
        elif not price_up and delta_up: result["delta_divergence"]="bullish"
        else:                           result["delta_divergence"]="none"

    # Order Blocks
    bull_ob = bear_ob = None
    for i in range(len(candles)-3,max(0,len(candles)-20),-1):
        c=candles[i]; cn=candles[i+1] if i+1<len(candles) else None
        if not cn: continue
        if (c.get("close",0)<c.get("open",0) and cn.get("close",0)>cn.get("open",0) and
                (cn.get("close",0)-cn.get("open",0))>atr*0.4 and bull_ob is None):
            bull_ob={"high":c.get("high",0),"low":c.get("low",0),
                     "mid":(c.get("high",0)+c.get("low",0))/2,
                     "strength":min(100,int(((cn.get("close",0)-cn.get("open",0))/atr)*40)),
                     "bars_ago":len(candles)-1-i}
        if (c.get("close",0)>c.get("open",0) and cn.get("close",0)<cn.get("open",0) and
                (cn.get("open",0)-cn.get("close",0))>atr*0.4 and bear_ob is None):
            bear_ob={"high":c.get("high",0),"low":c.get("low",0),
                     "mid":(c.get("high",0)+c.get("low",0))/2,
                     "strength":min(100,int(((cn.get("open",0)-cn.get("close",0))/atr)*40)),
                     "bars_ago":len(candles)-1-i}
    result["bullish_ob"]=bull_ob; result["bearish_ob"]=bear_ob

    # FVGs
    fvgs=[]
    for i in range(1,min(len(candles)-1,30)):
        pc=candles[-(i+2)] if i+2<=len(candles) else None
        nc=candles[-i]     if i<=len(candles)   else None
        if not pc or not nc: continue
        if nc.get("low",0)>pc.get("high",0):
            gs=nc.get("low",0)-pc.get("high",0)
            if gs>atr*0.1:
                fvgs.append({"type":"bullish","top":nc.get("low",0),"bot":pc.get("high",0),
                              "mid":(nc.get("low",0)+pc.get("high",0))/2,"size":round(gs,2),"bars_ago":i})
        if nc.get("high",0)<pc.get("low",0):
            gs=pc.get("low",0)-nc.get("high",0)
            if gs>atr*0.1:
                fvgs.append({"type":"bearish","top":pc.get("low",0),"bot":nc.get("high",0),
                              "mid":(pc.get("low",0)+nc.get("high",0))/2,"size":round(gs,2),"bars_ago":i})
    result["fvgs"]=fvgs[:6]

    # Liquidity
    eq=atr*0.3; lh2=[]; ll2=[]
    for i in range(min(len(candles)-2,50)):
        for j in range(i+1,min(i+8,len(candles))):
            if abs(candles[-(i+1)].get("high",0)-candles[-(j+1)].get("high",0))<eq:
                lv=(candles[-(i+1)].get("high",0)+candles[-(j+1)].get("high",0))/2
                if all(abs(lv-x)>eq for x in lh2): lh2.append(round(lv,2))
            if abs(candles[-(i+1)].get("low",0)-candles[-(j+1)].get("low",0))<eq:
                lv=(candles[-(i+1)].get("low",0)+candles[-(j+1)].get("low",0))/2
                if all(abs(lv-x)>eq for x in ll2): ll2.append(round(lv,2))
    result["liquidity_highs"]=sorted(lh2,key=lambda x:abs(x-cur_c))[:4]
    result["liquidity_lows"] =sorted(ll2,key=lambda x:abs(x-cur_c))[:4]

    if len(candles)>=2:
        p1=candles[-2]
        result["sweep_bull"]=(cur_l<p1.get("low",0) and cur_c>p1.get("low",0) and cur_c>cur_o)
        result["sweep_bear"]=(cur_h>p1.get("high",0) and cur_c<p1.get("high",0) and cur_c<cur_o)
    else:
        result["sweep_bull"]=False; result["sweep_bear"]=False

    # EMA Stack
    e8=current.get("ema8",0); e21=current.get("ema21",0); e50=current.get("ema50",0)
    if e8 and e21 and e50:
        if e8>e21>e50:   result["ema_stack"],result["ema_stack_str"]="bull",90
        elif e8<e21<e50: result["ema_stack"],result["ema_stack_str"]="bear",90
        elif e8>e21:     result["ema_stack"],result["ema_stack_str"]="bull",55
        elif e8<e21:     result["ema_stack"],result["ema_stack_str"]="bear",55
        else:            result["ema_stack"],result["ema_stack_str"]="neutral",30

    if len(closes)>=10:
        roc5  = round((closes[-1]-closes[-6])/closes[-6]*100,3)  if closes[-6]  else 0
        roc10 = round((closes[-1]-closes[-11])/closes[-11]*100,3) if closes[-11] else 0
        result["roc5"]=roc5; result["roc10"]=roc10

    if volumes:
        avg20 = sum(volumes[-20:])/min(20,len(volumes))
        result["vol_ratio"]=round(current.get("volume",0)/avg20,2) if avg20>0 else 1

    body=abs(cur_c-cur_o); tr=cur_h-cur_l if cur_h!=cur_l else 1
    result["body_pct"]=round(body/tr*100,1)
    result["upper_wick"]=round(cur_h-max(cur_c,cur_o),2)
    result["lower_wick"]=round(min(cur_c,cur_o)-cur_l,2)

    return result

# ── Performance Analysis ──────────────────────────────────────
def compute_stats():
    closed = [p for p in predictions if p.get("outcome") not in ("open","expired","")]
    if not closed:
        return {"total":0,"wins":0,"losses":0,"win_rate":0,"avg_rr":0,"expectancy":0,"total_pts":0,"avg_win_pts":0,"avg_loss_pts":0}
    wins   = [p for p in closed if p.get("outcome")=="target_hit"]
    losses = [p for p in closed if p.get("outcome")=="stop_hit"]
    total  = len(closed)
    wr     = round(len(wins)/total*100,1) if total else 0
    avg_win  = sum(abs(p.get("pnl_pts",0)) for p in wins)/len(wins)   if wins   else 0
    avg_loss = sum(abs(p.get("pnl_pts",0)) for p in losses)/len(losses) if losses else 0
    exp = round((len(wins)/total)*avg_win - (len(losses)/total)*avg_loss,2) if total else 0
    total_pts = sum(p.get("pnl_pts",0) for p in closed if p.get("pnl_pts") is not None)

    # Breakdown by setup type
    setup_stats = {}
    for p in closed:
        st = p.get("setup_type","unknown")
        if st not in setup_stats: setup_stats[st]={"wins":0,"losses":0,"pts":0}
        if p.get("outcome")=="target_hit": setup_stats[st]["wins"]+=1; setup_stats[st]["pts"]+=abs(p.get("pnl_pts",0))
        elif p.get("outcome")=="stop_hit": setup_stats[st]["losses"]+=1; setup_stats[st]["pts"]-=abs(p.get("pnl_pts",0))

    # Breakdown by session
    sess_stats = {}
    for p in closed:
        candle = p.get("candle",{}) or {}
        sess = candle.get("session","unknown") if isinstance(candle,dict) else "unknown"
        if sess not in sess_stats: sess_stats[sess]={"wins":0,"losses":0}
        if p.get("outcome")=="target_hit": sess_stats[sess]["wins"]+=1
        elif p.get("outcome")=="stop_hit": sess_stats[sess]["losses"]+=1

    return {
        "total":total,"wins":len(wins),"losses":len(losses),
        "win_rate":wr,"avg_win_pts":round(avg_win,2),
        "avg_loss_pts":round(avg_loss,2),"expectancy":exp,
        "total_pts":round(total_pts,2),
        "avg_rr":round(avg_win/avg_loss,2) if avg_loss else 0,
        "setup_stats":setup_stats, "sess_stats":sess_stats
    }

# ── Prediction Evaluation ─────────────────────────────────────
def evaluate_predictions(current_candle):
    cur_h = current_candle.get("high",0)
    cur_l = current_candle.get("low",0)
    cur_c = current_candle.get("close",0)
    now   = datetime.now(timezone.utc)
    changed = False

    for pred in predictions:
        if pred.get("outcome") != "open":
            continue
        entry  = float(pred.get("entry",0) or 0)
        target = float(pred.get("target",0) or 0)
        stop   = float(pred.get("stop",0) or 0)
        dir_   = pred.get("direction","neutral")

        try:
            created_dt = datetime.fromisoformat(pred.get("created_at",""))
            age_mins = (now - created_dt).total_seconds()/60
            if age_mins > 90:
                pred["outcome"]="expired"; pred["outcome_at"]=now.isoformat()
                pred["outcome_price"]=cur_c; changed=True; continue
        except: pass

        if dir_ == "bull":
            if not (target > entry > stop):
                pred["outcome"]="expired"; pred["outcome_at"]=now.isoformat()
                pred["outcome_price"]=cur_c; changed=True; continue
            if cur_h >= target:
                pred["outcome"]="target_hit"; pred["outcome_price"]=target
                pred["outcome_at"]=now.isoformat()
                pred["pnl_pts"]=round(target-entry,2); changed=True
            elif cur_l <= stop:
                pred["outcome"]="stop_hit"; pred["outcome_price"]=stop
                pred["outcome_at"]=now.isoformat()
                pred["pnl_pts"]=round(stop-entry,2); changed=True
        elif dir_ == "bear":
            if not (target < entry < stop):
                pred["outcome"]="expired"; pred["outcome_at"]=now.isoformat()
                pred["outcome_price"]=cur_c; changed=True; continue
            if cur_l <= target:
                pred["outcome"]="target_hit"; pred["outcome_price"]=target
                pred["outcome_at"]=now.isoformat()
                pred["pnl_pts"]=round(entry-target,2); changed=True
            elif cur_h >= stop:
                pred["outcome"]="stop_hit"; pred["outcome_price"]=stop
                pred["outcome_at"]=now.isoformat()
                pred["pnl_pts"]=round(entry-stop,2); changed=True

    if changed:
        save_predictions()

# ── TF Bias ───────────────────────────────────────────────────
def calc_tf_bias(d):
    if not d or not d.get("close"):
        return {"bias":"flat","strength":40,"rsi":50,"trend":"neutral"}
    c=float(d.get("close",0)); o=float(d.get("open",c))
    rsi=float(d.get("rsi",50)); e21=float(d.get("ema21",c))
    sc=0
    if c>o: sc+=1
    if c>e21: sc+=1
    if rsi>55: sc+=1
    if rsi<45: sc-=1
    if c<e21: sc-=1
    if c<o: sc-=1
    if sc>=2: return {"bias":"bull","strength":min(90,50+sc*15),"rsi":round(rsi,1),"trend":"bullish"}
    if sc<=-2: return {"bias":"bear","strength":min(90,50+abs(sc)*15),"rsi":round(rsi,1),"trend":"bearish"}
    return {"bias":"flat","strength":40,"rsi":round(rsi,1),"trend":"ranging"}

# ── Signal Lock ───────────────────────────────────────────────
def apply_lock(new_dir, new_entry, new_stop, new_target, atr):
    global direction_lock
    tick = max(0.25, round(atr/8,2))
    def snap(v): return round(round(v/tick)*tick,2)

    if new_dir==direction_lock["dir"]:
        direction_lock["count"]=min(direction_lock["count"]+1,10)
    else:
        direction_lock["count"]-=1
        if direction_lock["count"]<=0:
            direction_lock={"dir":new_dir,"count":1,"entry":None,"stop":None,"target":None}

    if direction_lock["entry"] is None:
        direction_lock["entry"]  = snap(new_entry)
        direction_lock["stop"]   = snap(new_stop)
        direction_lock["target"] = snap(new_target)
    elif abs(new_entry-direction_lock["entry"]) > atr*1.5:
        direction_lock["entry"]  = snap(new_entry)
        direction_lock["stop"]   = snap(new_stop)
        direction_lock["target"] = snap(new_target)

    return {
        "direction": direction_lock["dir"],
        "count":     direction_lock["count"],
        "entry":     direction_lock["entry"],
        "stop":      direction_lock["stop"],
        "target":    direction_lock["target"],
        "locked":    direction_lock["count"]>=LOCK_THRESHOLD
    }

# ── Elite SMC Prompt ──────────────────────────────────────────
def build_prompt(candle, smc, tf_biases, stats):
    bulls = sum(1 for v in tf_biases.values() if v.get("bias")=="bull")
    bears = sum(1 for v in tf_biases.values() if v.get("bias")=="bear")
    consensus = f"BULLISH {bulls}/5 TFs" if bulls>bears else f"BEARISH {bears}/5 TFs" if bears>bulls else f"MIXED {bulls}B/{bears}S"

    ob_b = smc.get("bullish_ob"); ob_r = smc.get("bearish_ob")
    fvgs = smc.get("fvgs",[])
    fvg_b = [f for f in fvgs if f["type"]=="bullish"]
    fvg_r = [f for f in fvgs if f["type"]=="bearish"]

    def ob_s(ob,l): return f"{l}: {ob['low']:.2f}–{ob['high']:.2f} ({ob['strength']}% kracht, {ob.get('bars_ago',0)}bars)" if ob else f"{l}: Geen"
    def fvg_s(fs): return " | ".join([f"{f['bot']:.2f}–{f['top']:.2f} ({f['size']:.1f}pt,{f['bars_ago']}b)" for f in fs[:3]]) or "Geen"

    tf_lines = "\n".join([f"  {tf}min: {v['bias'].upper()} {v['strength']}% | RSI {v['rsi']} | {v['trend']}" for tf,v in tf_biases.items()])

    close = candle.get("close",0)
    atr   = candle.get("atr",10)

    # Performance feedback for learning
    closed = [p for p in predictions if p.get("outcome") not in ("open","expired","")]
    perf_lines = []
    if stats["total"]>0:
        perf_lines.append(f"\nJOUW PRESTATIES: {stats['win_rate']}% WR | {stats['total']} trades | Expectancy: {stats['expectancy']}pt | Totaal: {stats['total_pts']}pt")
        if stats["win_rate"] < 50:
            perf_lines.append("⚠ WIN RATE ONDER 50% — Wees veel selectiever. Geef GEEN signaal tenzij volledige confluency.")

    if closed:
        recent = closed[-12:]
        perf_lines.append("\nRECENTE UITKOMSTEN — leer hieruit:")
        for p in recent:
            oc = "✓ WIN" if p["outcome"]=="target_hit" else "✗ STOP"
            candle_info = p.get("candle",{}) or {}
            sess = candle_info.get("session","?") if isinstance(candle_info,dict) else "?"
            perf_lines.append(f"  #{p['id']} {p.get('direction','?').upper()} E={p.get('entry','?')} → {oc} {p.get('pnl_pts',0):+.1f}pt | {p.get('setup_type','?')} | sess={sess} | regime={p.get('regime','?')}")

        losses = [p for p in closed[-30:] if p.get("outcome")=="stop_hit"]
        if len(losses)>=3:
            loss_setups = {}
            for p in losses:
                st = p.get("setup_type","?")
                loss_setups[st] = loss_setups.get(st,0)+1
            worst = sorted(loss_setups.items(),key=lambda x:-x[1])[:3]
            perf_lines.append(f"\n⚠ VERLIESPATRONEN (laatste 30 trades):")
            for st,cnt in worst:
                perf_lines.append(f"  {st}: {cnt} stops — wees EXTRA voorzichtig met deze setup")

        # Session analysis
        if stats.get("sess_stats"):
            perf_lines.append("\nPRESTATIES PER SESSIE:")
            for sess,data in stats["sess_stats"].items():
                total_s = data["wins"]+data["losses"]
                if total_s>0:
                    wr_s = round(data["wins"]/total_s*100)
                    perf_lines.append(f"  {sess}: {wr_s}% WR ({data['wins']}W/{data['losses']}L)")

    perf = "\n".join(perf_lines)
    sweep = "⚡ BULLISH LIQUIDITY SWEEP" if smc.get("sweep_bull") else "⚡ BEARISH LIQUIDITY SWEEP" if smc.get("sweep_bear") else "Geen sweep"

    return f"""Je bent een institutionele SMC trader bij een top hedge fund. Je taak: identificeer ALLEEN hoog-probabiliteit Smart Money setups.

SMART MONEY REGELS die je ALTIJD volgt:
1. Handel NOOIT tegen de hogere timeframe bias
2. Wacht op een Break of Structure (BoS) VOOR je een setup zoekt
3. Trade ALLEEN OB retests of FVG fills NA een BoS
4. Liquidity sweeps zijn de BESTE entry signalen
5. Bij ranging markt of compressie: GEEN signaal (direction=neutral)
6. Minimum R:R = 1:2.0. Onder 1:2 = GEEN trade
7. Bij twijfel: GEEN signaal is beter dan een slecht signaal
8. Hoge RSI (>70) in uptrend = wacht op pullback, geen new long
9. Lage RSI (<30) in downtrend = wacht op pullback, geen new short

═══ MARKTDATA ═══
CANDLE: O={candle.get('open')} H={candle.get('high')} L={candle.get('low')} C={close}
Volume={candle.get('volume')} | ATR={atr} | RSI={candle.get('rsi')} | Patroon={candle.get('pattern','none')}
Body={smc.get('body_pct',0)}% | UW={smc.get('upper_wick',0)} | LW={smc.get('lower_wick',0)}
Sessie={candle.get('session','?')} | HTF={candle.get('htf_trend','?').upper()}

═══ MTF BIAS ═══
{tf_lines}
→ {consensus}

═══ SMC STRUCTUUR ═══
Market Structure: {smc.get('market_structure','?').upper()} ({smc.get('ms_strength','?')}%)
Regime: {smc.get('regime','?').upper()} | Range ratio: {smc.get('range_ratio',1):.2f}x
EMA Stack: {smc.get('ema_stack','?')} ({smc.get('ema_stack_str','?')}%)
{ob_s(ob_b,'Bullish OB')}
{ob_s(ob_r,'Bearish OB')}
FVG Bullish: {fvg_s(fvg_b)}
FVG Bearish: {fvg_s(fvg_r)}
{sweep}

═══ INSTITUTIONEEL ═══
VWAP={smc.get('vwap')} | Zone={smc.get('vwap_zone','?').upper()} | Afstand={smc.get('vwap_distance',0):+.2f}pt
VOL POC={smc.get('vp_poc','?')} | VAH={smc.get('vp_vah','?')} | VAL={smc.get('vp_val','?')}
Cum Delta={smc.get('cumulative_delta',0):+.0f} ({smc.get('delta_trend','?')}) | Div={smc.get('delta_divergence','none')}
Vol Ratio={smc.get('vol_ratio',1):.2f}x | ROC5={smc.get('roc5',0):+.3f}%
Levels: Pivot={candle.get('pivot')} R1={candle.get('r1')} S1={candle.get('s1')} PDH={candle.get('pdh')} PDL={candle.get('pdl')}
Liquiditeit H={smc.get('liquidity_highs',[])} L={smc.get('liquidity_lows',[])}
{perf}

═══ BESLISSING ═══
Is er een volwaardige SMC setup aanwezig? Denk stap voor stap:
1. Wat is de HTF bias? Is de LTF daarmee in lijn?
2. Is er een BoS geweest? Waar is de OB of FVG die retested wordt?
3. Bevestigt de volume/delta het signaal?
4. Wat is de exacte entry, stop (logisch achter OB/FVG) en target (volgende liquiditeitspool)?
5. Is de R:R minimaal 1:2? Zo niet → neutral

Als GEEN duidelijke setup: geef direction=neutral met probability<55.

KRITISCH voor LONG: target MOET > entry > stop
KRITISCH voor SHORT: target MOET < entry < stop
Minimum R:R: 1:2.0

Reageer UITSLUITEND met dit JSON object:
{{"direction":"bull","probability":74,"entry":{close},"target":{round(close+atr*2.5,2)},"stop":{round(close-atr*0.8,2)},"rr":3.1,"confidence":"high","setup_type":"ob_retest","confluence_scores":[78,72,85,68,82,79],"smc_bias":"bullish","invalidation":{round(close-atr*1.2,2)},"regime":"{smc.get('regime','ranging')}","key_driver":"htf_bull_ob_retest_sweep","analysis":"120-150 woorden professionele analyse. 1) HTF bias en confluency 2) Specifiek SMC concept 3) Volume/delta bevestiging 4) Exacte invalidatie conditie"}}"""

# ── Async Analysis ────────────────────────────────────────────
def analyse_async(data, smc, tf_biases):
    global latest_signal, predictions
    if not ANTHROPIC_API_KEY:
        print("⚠ Geen API key"); return

    stats  = compute_stats()
    prompt = build_prompt(data, smc, tf_biases, stats)
    close  = data.get("close",0)
    atr    = data.get("atr",10)

    try:
        bulls = sum(1 for v in tf_biases.values() if v.get("bias")=="bull")
        bears = sum(1 for v in tf_biases.values() if v.get("bias")=="bear")
        print(f"🔬 Analyse: C={close} | {smc.get('regime','?')} | {smc.get('market_structure','?')} | {bulls}B/{bears}S...")

        resp = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"},
            json={"model":"claude-sonnet-4-20250514","max_tokens":1000,
                  "messages":[{"role":"user","content":prompt}]},
            timeout=35
        )
        if resp.status_code!=200:
            print(f"✗ API {resp.status_code}: {resp.text[:300]}"); return

        raw = "".join(b.get("text","") for b in resp.json().get("content",[]))
        raw = raw.strip()
        if "```" in raw:
            raw = re.sub(r'```[a-z]*\n?','',raw).strip()

        match = re.search(r'\{[\s\S]*\}',raw)
        if not match:
            print(f"✗ Geen JSON: {raw[:200]}"); return

        signal = json.loads(match.group())

        # Lock
        locked = apply_lock(
            signal.get("direction","neutral"),
            signal.get("entry",close),
            signal.get("stop",close-atr),
            signal.get("target",close+atr*2.5),
            atr
        )
        signal["direction"]    = locked["direction"]
        signal["lock_count"]   = locked["count"]
        signal["signal_locked"]= locked["locked"]

        # POST-LOCK VALIDATION — strict
        final_dir    = signal["direction"]
        final_entry  = float(locked["entry"] or close)
        final_target = float(locked["target"] or close+atr*2.5)
        final_stop   = float(locked["stop"]   or close-atr)

        if final_dir == "bull":
            if final_target <= final_entry:
                final_target = round(final_entry + atr*2.5, 2)
                print(f"⚠ LONG target corrected → {final_target}")
            if final_stop >= final_entry:
                final_stop = round(final_entry - atr*0.8, 2)
                print(f"⚠ LONG stop corrected → {final_stop}")
        elif final_dir == "bear":
            if final_target >= final_entry:
                final_target = round(final_entry - atr*2.5, 2)
                print(f"⚠ SHORT target corrected → {final_target}")
            if final_stop <= final_entry:
                final_stop = round(final_entry + atr*0.8, 2)
                print(f"⚠ SHORT stop corrected → {final_stop}")

        risk   = abs(final_entry-final_stop)
        reward = abs(final_target-final_entry)
        rr     = round(reward/risk,2) if risk>0 else 0

        # Enforce minimum R:R
        if rr < 1.8 and final_dir != "neutral":
            print(f"⚠ R:R {rr} te laag — signaal gedegradeerd naar neutral")
            final_dir = "neutral"
            signal["direction"] = "neutral"

        signal["entry"]  = final_entry
        signal["target"] = final_target
        signal["stop"]   = final_stop
        signal["rr"]     = rr
        direction_lock["entry"]  = final_entry
        direction_lock["stop"]   = final_stop
        direction_lock["target"] = final_target

        signal["smc_data"]    = smc
        signal["tf_biases"]   = tf_biases
        signal["stats"]       = stats
        signal["candle"]      = {k:data.get(k) for k in ["open","high","low","close","volume","session","pattern","htf_trend","rsi","atr","vwap","ema8","ema21","ema50"]}
        signal["analysed_at"] = datetime.now(timezone.utc).isoformat()

        # Register prediction (only strong, valid signals)
        if (final_dir in ("bull","bear") and
                signal.get("probability",0)>=65 and
                signal.get("confidence","") in ("high","medium") and
                rr >= 1.8):
            last = predictions[-1] if predictions else None
            is_new = (not last or
                      last.get("direction")!=final_dir or
                      abs(final_entry-(last.get("entry") or 0))>atr*1.5)

            if is_new:
                # Validate before storing
                valid = ((final_dir=="bull" and final_target>final_entry>final_stop) or
                         (final_dir=="bear" and final_target<final_entry<final_stop))
                if valid:
                    pred = {
                        "id":          len(predictions)+1,
                        "direction":   final_dir,
                        "entry":       final_entry,
                        "target":      final_target,
                        "stop":        final_stop,
                        "probability": signal.get("probability",0),
                        "setup_type":  signal.get("setup_type","unknown"),
                        "regime":      signal.get("regime","unknown"),
                        "rr":          rr,
                        "outcome":     "open",
                        "outcome_price":None,
                        "outcome_at":  None,
                        "pnl_pts":     None,
                        "created_at":  signal["analysed_at"],
                        "candle_close":close,
                        "candle":      signal["candle"]
                    }
                    predictions.append(pred)
                    if len(predictions)>MAX_PREDICTIONS: predictions.pop(0)
                    save_predictions()

        latest_signal = signal
        print(f"✓ {final_dir} {signal.get('probability','?')}% | {signal.get('setup_type','?')} | R:R 1:{rr} | lock={locked['count']}/{LOCK_THRESHOLD}")

    except json.JSONDecodeError as e:
        print(f"✗ JSON: {e}")
    except req.exceptions.Timeout:
        print("✗ Timeout (35s)")
    except Exception as e:
        print(f"✗ {type(e).__name__}: {e}")

# ── Routes ────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    stats = compute_stats()
    return jsonify({
        "status":"online","service":"NQ Oracle ELITE v8",
        "candles":len(candle_history),"predictions":len(predictions),
        "open_predictions":sum(1 for p in predictions if p.get("outcome")=="open"),
        "win_rate":f"{stats['win_rate']}%","expectancy":f"{stats['expectancy']}pt",
        "last_signal":latest_signal.get("analysed_at","none"),
        "anthropic_model":"claude-sonnet-4-20250514",
        "anthropic_ready":bool(ANTHROPIC_API_KEY)
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    global latest_candle, tf_data
    if not check_auth(): return jsonify({"error":"Unauthorized"}),401
    try:
        data = request.get_json(force=True,silent=True)
        if not data: return jsonify({"error":"No JSON"}),400
        data  = normalize(data)
        tf_raw= data.pop("_tf_data",{})
        for tf,c in tf_raw.items():
            if c: tf_data[tf]=c
        data["received_at"] = datetime.now(timezone.utc).isoformat()
        latest_candle = data
        candle_history.append(data)
        if len(candle_history)>MAX_HISTORY: candle_history.pop(0)
        evaluate_predictions(data)
        print(f"[{data['received_at']}] C={data.get('close')} | {data.get('session')} | {data.get('pattern')}")
        tf_biases = {tf:calc_tf_bias(tf_data.get(tf,{})) for tf in ["1","5","15","30","60"]}
        smc = calculate_smc(candle_history[:-1],data) if len(candle_history)>=5 else {}
        t = threading.Thread(target=analyse_async,args=(data.copy(),smc,tf_biases),daemon=True)
        t.start()
        return jsonify({"status":"ok","candles":len(candle_history)}),200
    except Exception as e:
        print(f"✗ Webhook: {e}"); return jsonify({"error":str(e)}),500

@app.route("/latest", methods=["GET"])
def get_latest():
    if not latest_candle: return jsonify({"status":"no_data"}),200
    return jsonify({"status":"ok","data":latest_candle,"tf_data":tf_data}),200

@app.route("/latest_signal", methods=["GET"])
def get_signal():
    if not latest_signal: return jsonify({"status":"no_signal"}),200
    return jsonify({"status":"ok","signal":latest_signal}),200

@app.route("/candles", methods=["GET"])
def get_candles():
    n = min(int(request.args.get("n",200)),MAX_HISTORY)
    return jsonify({"status":"ok","count":len(candle_history),"data":candle_history[-n:]}),200

@app.route("/predictions", methods=["GET"])
def get_predictions():
    stats = compute_stats()
    n = int(request.args.get("n",100))
    return jsonify({"status":"ok","predictions":predictions[-n:],"stats":stats,"total_stored":len(predictions)}),200

@app.route("/predictions/all", methods=["GET"])
def get_all_predictions():
    stats = compute_stats()
    return jsonify({"status":"ok","predictions":predictions,"stats":stats,"total":len(predictions)}),200

@app.route("/execute", methods=["POST"])
def execute():
    global pending_order
    if not check_auth(): return jsonify({"error":"Unauthorized"}),401
    try:
        data = request.get_json(force=True,silent=True)
        if not data: return jsonify({"error":"No data"}),400
        entry=float(data.get("entry",0)); stop=float(data.get("stop",0))
        target=float(data.get("target",0)); contracts=int(data.get("contracts",2))
        action=data.get("action","long")
        risk_pts=abs(entry-stop); risk_usd=risk_pts*20*contracts
        profit_usd=abs(target-entry)*20*contracts
        rr=abs(target-entry)/abs(stop-entry) if abs(stop-entry)>0 else 0
        order={"action":action,"entry":round(entry,2),"stop":round(stop,2),
               "target":round(target,2),"contracts":contracts,"rr":round(rr,2),
               "risk_pts":round(risk_pts,2),"risk_usd":round(risk_usd,2),
               "profit_usd":round(profit_usd,2),"status":"pending",
               "created_at":datetime.now(timezone.utc).isoformat()}
        pending_order=order; order_history.append(dict(order))
        if len(order_history)>200: order_history.pop(0)
        return jsonify({"status":"ok","order":order}),200
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/clear", methods=["POST"])
def clear():
    global latest_candle,candle_history,latest_signal,tf_data,pending_order,direction_lock
    if not check_auth(): return jsonify({"error":"Unauthorized"}),401
    latest_candle={}; candle_history=[]; latest_signal={}
    tf_data={"1":{},"5":{},"15":{},"30":{},"60":{}}
    pending_order={}
    direction_lock={"dir":"neutral","count":0,"entry":None,"stop":None,"target":None}
    # Note: predictions preserved intentionally
    return jsonify({"status":"cleared","predictions_preserved":len(predictions)}),200

if __name__=="__main__":
    port=int(os.environ.get("PORT",8080))
    print(f"NQ Oracle ELITE v8 — poort {port}")
    print(f"Model: claude-sonnet-4-20250514")
    print(f"Predictions loaded: {len(predictions)}")
    app.run(host="0.0.0.0",port=port,debug=False)
