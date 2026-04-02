"""
NQ Oracle ELITE — Wall Street Grade SMC Server v7
Complete rebuild. Professional grade. No compromises.

Features:
- Prediction tracking with auto-evaluation (hit target / hit stop / expired)
- Volume Profile POC approximation
- VWAP bands (±1σ, ±2σ)
- Cumulative Delta approximation
- Regime detection (trending / ranging / breakout)
- Win rate, expectancy, Sharpe-like scoring
- Claude Sonnet for maximum intelligence
- Full candle history for chart rendering
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
import os, threading, json, re, math, requests as req

app = Flask(__name__)
CORS(app, origins="*")

# ── Global State ──────────────────────────────────────────────
candle_history   = []   # max 500 candles
latest_candle    = {}
latest_signal    = {}
tf_data          = {"1":{},"5":{},"15":{},"30":{},"60":{}}
predictions      = []   # all predictions with outcomes
pending_order    = {}
order_history    = []
MAX_HISTORY      = 500
MAX_PREDICTIONS  = 200

# Signal stability lock
direction_lock = {"dir":"neutral","count":0,"entry":None,"stop":None,"target":None}
LOCK_THRESHOLD = 3

WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "nq-oracle-secret")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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

    # MTF parsing
    tf_result = {}
    for key,tf in [("m1","1"),("m5","5"),("m15","15"),("m30","30"),("m60","60")]:
        if key in data:
            try:
                raw = str(data.pop(key))
                parts = raw.split(",")
                # New compact format: close,rsi,ema21,atr (4 fields)
                # Old format: open,high,low,close,volume,rsi,ema21,atr (8 fields)
                if len(parts) >= 8:
                    tf_result[tf] = {
                        "open":float(parts[0]),"high":float(parts[1]),
                        "low":float(parts[2]),"close":float(parts[3]),
                        "volume":float(parts[4]),"rsi":float(parts[5]),
                        "ema21":float(parts[6]),"atr":float(parts[7])
                    }
                elif len(parts) >= 4:
                    # Compact format: close,rsi,ema21,atr
                    tf_result[tf] = {
                        "close":float(parts[0]),"open":float(parts[0]),
                        "rsi":float(parts[1]),"ema21":float(parts[2]),
                        "atr":float(parts[3])
                    }
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

    nums = ["open","high","low","close","volume","vwap","ema8","ema21","ema50",
            "rsi","atr","pivot","r1","s1","pdh","pdl","vol_delta",
            "prev_high","prev_low","prev_close","prev_volume"]
    for k in nums:
        if k in data:
            try: data[k] = float(data[k])
            except: pass

    data["_tf_data"] = tf_result
    return data

# ── Advanced SMC Engine ───────────────────────────────────────
def calculate_advanced_smc(candles, current):
    if len(candles) < 10:
        return {}

    closes  = [c.get("close",0)  for c in candles]
    highs   = [c.get("high",0)   for c in candles]
    lows    = [c.get("low",0)    for c in candles]
    volumes = [c.get("volume",0) for c in candles]
    opens   = [c.get("open",0)   for c in candles]

    cur_c = current.get("close",0)
    cur_h = current.get("high",0)
    cur_l = current.get("low",0)
    cur_o = current.get("open",0)
    atr   = current.get("atr",10)
    vwap  = current.get("vwap",cur_c)
    result = {}

    # ── 1. Market Structure ───────────────────────────────────
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

    # ── 2. Regime Detection ───────────────────────────────────
    # ADX-like measure: trend strength via range expansion
    n20 = min(20,len(candles))
    ranges = [candles[-(i+1)].get("high",0)-candles[-(i+1)].get("low",0) for i in range(n20)]
    avg_range = sum(ranges)/len(ranges) if ranges else atr
    cur_range = cur_h-cur_l
    range_ratio = cur_range/avg_range if avg_range>0 else 1

    # Price vs VWAP distance in ATRs
    vwap_dist_atr = abs(cur_c-vwap)/atr if atr>0 else 0

    if ms in ("bullish","bearish") and range_ratio>1.3:
        regime = "trending_strong"
    elif ms in ("bullish","bearish"):
        regime = "trending_normal"
    elif range_ratio < 0.7:
        regime = "compression"
    else:
        regime = "ranging"

    result["regime"]       = regime
    result["range_ratio"]  = round(range_ratio,2)
    result["vwap_dist_atr"]= round(vwap_dist_atr,2)

    # ── 3. VWAP Bands (±1σ, ±2σ) ────────────────────────────
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
        # Where is price relative to bands?
        if cur_c > vwap+2*vwap_std:   result["vwap_zone"] = "extreme_high"
        elif cur_c > vwap+vwap_std:   result["vwap_zone"] = "high"
        elif cur_c > vwap:            result["vwap_zone"] = "above"
        elif cur_c > vwap-vwap_std:   result["vwap_zone"] = "below"
        elif cur_c > vwap-2*vwap_std: result["vwap_zone"] = "low"
        else:                         result["vwap_zone"] = "extreme_low"
    result["vwap"]      = round(vwap,2)
    result["above_vwap"]= cur_c>vwap
    result["vwap_distance"] = round(cur_c-vwap,2)

    # ── 4. Volume Profile POC ────────────────────────────────
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
            poc     = round(price_min + (poc_idx+0.5)*bin_sz,2)
            # VAH/VAL (70% of volume)
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
                          "price_vs_poc": "above" if cur_c>poc else "below"})

    # ── 5. Cumulative Delta (proxy) ───────────────────────────
    n_cd = min(20,len(candles))
    cum_delta = 0
    delta_history = []
    for c in candles[-n_cd:]:
        body = c.get("close",0)-c.get("open",0)
        vol  = c.get("volume",0)
        delta = vol if body>=0 else -vol
        cum_delta += delta
        delta_history.append(cum_delta)

    result["cumulative_delta"]  = round(cum_delta,0)
    result["delta_trend"]       = "bull" if cum_delta>0 else "bear"
    # Delta divergence: price going up but delta going down = bearish divergence
    if len(delta_history)>=5:
        price_up = closes[-1]>closes[-5] if len(closes)>=5 else True
        delta_up = delta_history[-1]>delta_history[-5]
        if price_up and not delta_up:
            result["delta_divergence"] = "bearish"
        elif not price_up and delta_up:
            result["delta_divergence"] = "bullish"
        else:
            result["delta_divergence"] = "none"

    # ── 6. Order Blocks ──────────────────────────────────────
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

    # ── 7. Fair Value Gaps ───────────────────────────────────
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

    # ── 8. Liquidity & Sweeps ────────────────────────────────
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

    # ── 9. EMA Stack & Momentum ──────────────────────────────
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
        result["momentum"]="bull" if roc5>0 else "bear"

    if volumes:
        avg20 = sum(volumes[-20:])/min(20,len(volumes))
        result["vol_ratio"]=round(current.get("volume",0)/avg20,2) if avg20>0 else 1

    body=abs(cur_c-cur_o); tr=cur_h-cur_l if cur_h!=cur_l else 1
    result["body_pct"]=round(body/tr*100,1)
    result["upper_wick"]=round(cur_h-max(cur_c,cur_o),2)
    result["lower_wick"]=round(min(cur_c,cur_o)-cur_l,2)

    return result

# ── Prediction Auto-Evaluation ───────────────────────────────
def evaluate_predictions(current_candle):
    """Check if any open predictions have hit their target or stop."""
    cur_h = current_candle.get("high",0)
    cur_l = current_candle.get("low",0)
    cur_c = current_candle.get("close",0)
    now   = datetime.now(timezone.utc)

    for pred in predictions:
        if pred.get("outcome") != "open":
            continue

        entry  = pred.get("entry",0)
        target = pred.get("target",0)
        stop   = pred.get("stop",0)
        dir    = pred.get("direction","neutral")
        created= pred.get("created_at","")

        # Expire after 60 minutes
        try:
            created_dt = datetime.fromisoformat(created)
            age_mins = (now - created_dt).total_seconds()/60
            if age_mins > 60:
                pred["outcome"]    = "expired"
                pred["outcome_at"] = now.isoformat()
                pred["outcome_price"] = cur_c
                continue
        except: pass

        if dir == "bull":
            # LONG: target must be above entry, stop below entry
            # Safety check: if stored prediction is invalid, mark expired
            if not (target > entry > stop):
                pred["outcome"] = "expired"
                pred["outcome_at"] = now.isoformat()
                pred["outcome_price"] = cur_c
                print(f"⚠ Invalid LONG prediction #{pred.get('id')} expired: E={entry} T={target} S={stop}")
                continue
            if cur_h >= target:
                pred["outcome"]="target_hit"; pred["outcome_price"]=target
                pred["outcome_at"]=now.isoformat(); pred["pnl_pts"]=round(target-entry,2)
            elif cur_l <= stop:
                pred["outcome"]="stop_hit"; pred["outcome_price"]=stop
                pred["outcome_at"]=now.isoformat(); pred["pnl_pts"]=round(stop-entry,2)
        elif dir == "bear":
            # SHORT: target must be below entry, stop above entry
            if not (target < entry < stop):
                pred["outcome"] = "expired"
                pred["outcome_at"] = now.isoformat()
                pred["outcome_price"] = cur_c
                print(f"⚠ Invalid SHORT prediction #{pred.get('id')} expired: E={entry} T={target} S={stop}")
                continue
            if cur_l <= target:
                pred["outcome"]="target_hit"; pred["outcome_price"]=target
                pred["outcome_at"]=now.isoformat(); pred["pnl_pts"]=round(entry-target,2)
            elif cur_h >= stop:
                pred["outcome"]="stop_hit"; pred["outcome_price"]=stop
                pred["outcome_at"]=now.isoformat(); pred["pnl_pts"]=round(entry-stop,2)

# ── Statistics ────────────────────────────────────────────────
def compute_stats():
    closed = [p for p in predictions if p.get("outcome") not in ("open","expired")]
    if not closed:
        return {"total":0,"wins":0,"losses":0,"win_rate":0,"avg_rr":0,"expectancy":0,"total_pts":0}
    wins   = [p for p in closed if p.get("outcome")=="target_hit"]
    losses = [p for p in closed if p.get("outcome")=="stop_hit"]
    total  = len(closed)
    wr     = round(len(wins)/total*100,1) if total else 0
    avg_pnl= sum(p.get("pnl_pts",0) for p in closed)/total if total else 0
    total_pts= sum(p.get("pnl_pts",0) for p in closed)
    # Expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
    avg_win  = sum(p.get("pnl_pts",0) for p in wins)/len(wins)   if wins   else 0
    avg_loss = abs(sum(p.get("pnl_pts",0) for p in losses)/len(losses)) if losses else 0
    exp = round((len(wins)/total)*avg_win - (len(losses)/total)*avg_loss,2) if total else 0
    return {
        "total":total,"wins":len(wins),"losses":len(losses),
        "win_rate":wr,"avg_win_pts":round(avg_win,2),
        "avg_loss_pts":round(avg_loss,2),"expectancy":exp,
        "total_pts":round(total_pts,2),"avg_rr":round(avg_win/avg_loss,2) if avg_loss else 0
    }

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

# ── TF Bias ───────────────────────────────────────────────────
def calc_tf_bias(d):
    if not d or not d.get("close"):
        return {"bias":"flat","strength":40,"rsi":50,"trend":"neutral"}
    c=float(d.get("close",0)); o=float(d.get("open",0))
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

# ── Elite AI Prompt ───────────────────────────────────────────
def build_elite_prompt(candle, smc, tf_biases, stats):
    bulls = sum(1 for v in tf_biases.values() if v.get("bias")=="bull")
    bears = sum(1 for v in tf_biases.values() if v.get("bias")=="bear")
    consensus = f"BULLISH {bulls}/5" if bulls>bears else f"BEARISH {bears}/5" if bears>bulls else f"MIXED {bulls}B/{bears}S"

    ob_b = smc.get("bullish_ob")
    ob_r = smc.get("bearish_ob")
    fvgs = smc.get("fvgs",[])
    fvg_bull = [f for f in fvgs if f["type"]=="bullish"]
    fvg_bear = [f for f in fvgs if f["type"]=="bearish"]

    def ob_s(ob,l): return f"{l}: {ob['low']:.2f}–{ob['high']:.2f} ({ob['strength']}% kracht, {ob.get('bars_ago',0)} bars geleden)" if ob else f"{l}: Geen"
    def fvg_s(fs): return " | ".join([f"{f['bot']:.2f}–{f['top']:.2f} ({f['size']:.1f}pt,{f['bars_ago']}b)" for f in fs[:3]]) or "Geen"

    close = candle.get("close",0)
    atr   = candle.get("atr",10)
    rsi   = candle.get("rsi",50)
    vp_poc= smc.get("vp_poc","n/a")
    regime= smc.get("regime","unknown")
    sweep = "⚡ BULLISH SWEEP" if smc.get("sweep_bull") else "⚡ BEARISH SWEEP" if smc.get("sweep_bear") else "Geen sweep"
    div   = smc.get("delta_divergence","none")
    vwap_zone = smc.get("vwap_zone","unknown")

    tf_lines = "\n".join([
        f"  {tf}min: {v['bias'].upper()} {v['strength']}% | RSI {v['rsi']} | {v['trend']}"
        for tf,v in tf_biases.items()
    ])

    perf = ""
    if stats["total"]>0:
        perf = f"\nMODEL PRESTATIES (historisch): {stats['win_rate']}% win rate | {stats['total']} trades | Expectancy: {stats['expectancy']} pt/trade | Totaal: {stats['total_pts']} pt"

    return f"""Je bent een senior quantitative analyst en Smart Money Concepts specialist bij een tier-1 hedge fund. Je analyseert NQ Nasdaq E-mini futures voor institutionele clients.

Je taak: geef een objectieve, data-gedreven marktanalyse en een hoog-probabiliteit scalp signaal voor de komende 1-3 candles.

═══════════════ MARKTDATA ═══════════════

INSTRUMENT: NQ1! E-mini Nasdaq Futures
CANDLE: O={candle.get('open')} H={candle.get('high')} L={candle.get('low')} C={close}
Volume: {candle.get('volume')} | ATR14: {atr} | RSI14: {rsi}
Patroon: {candle.get('pattern','none')} | Sessie: {candle.get('session','?')}
Body: {smc.get('body_pct',0)}% van range | Upper wick: {smc.get('upper_wick',0)} | Lower wick: {smc.get('lower_wick',0)}

═══════════════ MULTI-TIMEFRAME ══════════════

{tf_lines}
→ Consensus: {consensus}
HTF Trend (1H EMA): {candle.get('htf_trend','?').upper()}

═══════════════ SMC STRUCTUUR ═══════════════

REGIME: {regime.upper()}
Market Structure: {smc.get('market_structure','?').upper()} ({smc.get('ms_strength','?')}%)
EMA Stack: {smc.get('ema_stack','?')} ({smc.get('ema_stack_str','?')}%)

{ob_s(ob_b,'Bullish OB')}
{ob_s(ob_r,'Bearish OB')}

FVG Bullish: {fvg_s(fvg_bull)}
FVG Bearish: {fvg_s(fvg_bear)}

Liquiditeit highs: {smc.get('liquidity_highs',[])}
Liquiditeit lows:  {smc.get('liquidity_lows',[])}
{sweep}

═══════════════ INSTITUTIONELE DATA ══════════════

VWAP: {smc.get('vwap')} | Zone: {vwap_zone.upper()} | Afstand: {smc.get('vwap_distance',0):+.2f}pt
VWAP Bands: -{smc.get('vwap_band1_down','n/a')} / +{smc.get('vwap_band1_up','n/a')} (1σ)
Volume Profile POC: {vp_poc} | VAH: {smc.get('vp_vah','n/a')} | VAL: {smc.get('vp_val','n/a')}
Price vs POC: {smc.get('price_vs_poc','?').upper()}
Cumulative Delta: {smc.get('cumulative_delta',0):+.0f} ({smc.get('delta_trend','?').upper()})
Delta Divergentie: {div.upper()}
Volume Ratio: {smc.get('vol_ratio',1):.2f}x gemiddeld
Momentum ROC5: {smc.get('roc5',0):+.3f}% | ROC10: {smc.get('roc10',0):+.3f}%

Key Levels: Pivot={candle.get('pivot')} R1={candle.get('r1')} S1={candle.get('s1')} PDH={candle.get('pdh')} PDL={candle.get('pdl')}
{perf}

═══════════════ ANALYSE INSTRUCTIES ══════════════

Analyseer alle data als een professionele institutionele trader:
1. Wat is de dominante institutionele bias op basis van MTF confluency?
2. Welk SMC concept biedt het beste entry punt (OB retest, FVG fill, liquidity sweep, BoS)?
3. Is er een delta divergentie of volume anomalie die de richting bevestigt of tegenspreekt?
4. Waar staat institutionele liquiditeit en wat is de logische magneet?
5. Geef een concreet, actionable signaal met precieze niveaus.

KRITISCH:
- Voor BULL signaal: target MOET hoger zijn dan entry, stop MOET lager zijn dan entry
- Voor BEAR signaal: target MOET lager zijn dan entry, stop MOET hoger zijn dan entry
- Controleer dit ALTIJD voor je antwoordt
- Geef UITSLUITEND het JSON object terug. Geen tekst, geen markdown.

{{"direction":"bull","probability":74,"entry":{close},"target":{round(close+atr*2,2)},"stop":{round(close-atr*0.8,2)},"rr":2.5,"confidence":"high","setup_type":"ob_retest","confluence_scores":[78,72,85,68,82,79],"smc_bias":"bullish","invalidation":{round(close-atr*1.2,2)},"regime":"{regime}","key_driver":"vwap_reclaim_ob_confluence","analysis":"[120-150 woorden professionele Nederlandse analyse. Bespreek: 1) institutionele bias op basis van MTF data, 2) specifiek SMC concept dat het signaal drijft met exacte niveaus, 3) volume/delta bevestiging of waarschuwing, 4) exacte invalidatie conditie en risicobeheer]"}}"""

# ── Async Analysis ────────────────────────────────────────────
def analyse_async(data, smc, tf_biases):
    global latest_signal, predictions

    if not ANTHROPIC_API_KEY:
        print("⚠ Geen API key")
        return

    stats  = compute_stats()
    prompt = build_elite_prompt(data, smc, tf_biases, stats)

    try:
        atr   = data.get("atr",10)
        close = data.get("close",0)
        sess  = data.get("session","?")
        ms    = smc.get("market_structure","?")
        print(f"🔬 Elite analyse: C={close} | sess={sess} | regime={smc.get('regime','?')} | struct={ms} | {sum(1 for v in tf_biases.values() if v.get('bias')=='bull')}B/{sum(1 for v in tf_biases.values() if v.get('bias')=='bear')}S...")

        resp = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json",
                     "x-api-key":ANTHROPIC_API_KEY,
                     "anthropic-version":"2023-06-01"},
            json={"model":"claude-sonnet-4-20250514",  # Sonnet for maximum quality
                  "max_tokens":1200,
                  "messages":[{"role":"user","content":prompt}]},
            timeout=35
        )

        if resp.status_code!=200:
            print(f"✗ API {resp.status_code}: {resp.text[:300]}")
            return

        raw = "".join(b.get("text","") for b in resp.json().get("content",[]))
        raw = raw.strip()
        if "```" in raw:
            raw = re.sub(r'```[a-z]*\n?','',raw).strip()

        match = re.search(r'\{[\s\S]*\}',raw)
        if not match:
            print(f"✗ Geen JSON: {raw[:200]}")
            return

        signal = json.loads(match.group())

        # Signal lock
        locked = apply_lock(
            signal.get("direction","neutral"),
            signal.get("entry",close),
            signal.get("stop",close-atr),
            signal.get("target",close+atr*2),
            atr
        )
        signal["entry"]       = locked["entry"]
        signal["stop"]        = locked["stop"]
        signal["target"]      = locked["target"]
        signal["direction"]   = locked["direction"]
        signal["lock_count"]  = locked["count"]
        signal["signal_locked"]= locked["locked"]

        # ── Validate AFTER lock — lock cannot override physics ───
        final_dir    = signal.get("direction","neutral")
        final_entry  = float(signal.get("entry",  close) or close)
        final_target = float(signal.get("target", close) or close)
        final_stop   = float(signal.get("stop",   close) or close)

        if final_dir == "bull":
            if final_target <= final_entry:
                final_target = round(final_entry + atr * 2.0, 2)
                print(f"⚠ POST-LOCK: LONG target {signal.get('target')} corrected → {final_target}")
            if final_stop >= final_entry:
                final_stop = round(final_entry - atr * 0.8, 2)
                print(f"⚠ POST-LOCK: LONG stop {signal.get('stop')} corrected → {final_stop}")
        elif final_dir == "bear":
            if final_target >= final_entry:
                final_target = round(final_entry - atr * 2.0, 2)
                print(f"⚠ POST-LOCK: SHORT target {signal.get('target')} corrected → {final_target}")
            if final_stop <= final_entry:
                final_stop = round(final_entry + atr * 0.8, 2)
                print(f"⚠ POST-LOCK: SHORT stop {signal.get('stop')} corrected → {final_stop}")

        risk   = abs(final_entry - final_stop)
        reward = abs(final_target - final_entry)
        signal["entry"]  = final_entry
        signal["target"] = final_target
        signal["stop"]   = final_stop
        signal["rr"]     = round(reward / risk, 2) if risk > 0 else 0

        # Also fix the direction_lock stored values
        direction_lock["entry"]  = final_entry
        direction_lock["stop"]   = final_stop
        direction_lock["target"] = final_target

        # Attach context
        signal["smc_data"]    = smc
        signal["tf_biases"]   = tf_biases
        signal["stats"]       = stats
        signal["candle"]      = {k:data.get(k) for k in ["open","high","low","close","volume","session","pattern","htf_trend","rsi","atr","vwap","ema8","ema21","ema50"]}
        signal["analysed_at"] = datetime.now(timezone.utc).isoformat()

        # Register as new prediction if direction changed or price moved significantly
        if signal.get("direction") not in ("neutral",) and signal.get("probability",0)>=60:
            last_pred = predictions[-1] if predictions else None
            is_new = (not last_pred or
                      last_pred.get("direction")!=signal["direction"] or
                      abs(signal.get("entry",0)-last_pred.get("entry",0))>atr*1.5)
            if is_new:
                # Only store prediction if entry/target/stop are logically valid
                p_dir = signal["direction"]
                p_entry = float(signal["entry"] or 0)
                p_target = float(signal["target"] or 0)
                p_stop = float(signal["stop"] or 0)
                p_valid = (
                    (p_dir == "bull" and p_target > p_entry > p_stop) or
                    (p_dir == "bear" and p_target < p_entry < p_stop)
                )
                if not p_valid:
                    print(f"⚠ Prediction skipped: invalid {p_dir} E={p_entry} T={p_target} S={p_stop}")
                    is_new = False

            if is_new:
                prediction = {
                    "id":          len(predictions)+1,
                    "direction":   signal["direction"],
                    "entry":       signal["entry"],
                    "target":      signal["target"],
                    "stop":        signal["stop"],
                    "probability": signal.get("probability",0),
                    "setup_type":  signal.get("setup_type","unknown"),
                    "regime":      signal.get("regime","unknown"),
                    "rr":          signal.get("rr",0),
                    "outcome":     "open",
                    "outcome_price":None,
                    "pnl_pts":    None,
                    "created_at":  signal["analysed_at"],
                    "candle_close":close
                }
                predictions.append(prediction)
                if len(predictions)>MAX_PREDICTIONS:
                    predictions.pop(0)

        latest_signal = signal
        prob = signal.get('probability','?')
        dir  = signal.get('direction','?')
        lock = locked['count']
        print(f"✓ Signal: {dir} {prob}% | {signal.get('setup_type','?')} | lock={lock}/{LOCK_THRESHOLD} | stats: {stats['win_rate']}% WR")

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
        "status":"online","service":"NQ Oracle ELITE v7",
        "candles":len(candle_history),
        "predictions":len(predictions),
        "win_rate":f"{stats['win_rate']}%",
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

        # Evaluate open predictions
        evaluate_predictions(data)

        print(f"[{data['received_at']}] C={data.get('close')} | {data.get('session')} | {data.get('pattern')} | candles={len(candle_history)}")

        tf_biases = {tf:calc_tf_bias(tf_data.get(tf,{})) for tf in ["1","5","15","30","60"]}
        smc = calculate_advanced_smc(candle_history[:-1],data) if len(candle_history)>=5 else {}

        t = threading.Thread(target=analyse_async,args=(data.copy(),smc,tf_biases),daemon=True)
        t.start()

        return jsonify({"status":"ok","candles":len(candle_history)}),200
    except Exception as e:
        print(f"✗ Webhook: {e}")
        return jsonify({"error":str(e)}),500

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
    n = min(int(request.args.get("n",100)),MAX_HISTORY)
    return jsonify({"status":"ok","count":len(candle_history),"data":candle_history[-n:]}),200

@app.route("/predictions", methods=["GET"])
def get_predictions():
    stats = compute_stats()
    return jsonify({"status":"ok","predictions":predictions[-50:],"stats":stats}),200

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
        if len(order_history)>100: order_history.pop(0)
        print(f"📋 Order: {action.upper()} {contracts}x @ {entry} SL:{stop} TP:{target} R:R 1:{rr:.1f} Risico:${risk_usd:.0f}")
        return jsonify({"status":"ok","order":order}),200
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/pending_order", methods=["GET"])
def get_pending():
    if not pending_order: return jsonify({"status":"no_order"}),200
    return jsonify({"status":"ok","order":pending_order}),200

@app.route("/clear", methods=["POST"])
def clear():
    global latest_candle,candle_history,latest_signal,tf_data,predictions,pending_order,direction_lock
    if not check_auth(): return jsonify({"error":"Unauthorized"}),401
    latest_candle={}; candle_history=[]; latest_signal={}
    tf_data={"1":{},"5":{},"15":{},"30":{},"60":{}}
    predictions=[]; pending_order={}
    direction_lock={"dir":"neutral","count":0,"entry":None,"stop":None,"target":None}
    return jsonify({"status":"cleared"}),200

if __name__=="__main__":
    port=int(os.environ.get("PORT",8080))
    print(f"NQ Oracle ELITE v7 — poort {port}")
    print(f"Model: claude-sonnet-4-20250514")
    print(f"API: {'✓' if ANTHROPIC_API_KEY else '✗'}")
    app.run(host="0.0.0.0",port=port,debug=False)
