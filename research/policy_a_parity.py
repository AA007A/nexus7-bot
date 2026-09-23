from __future__ import annotations

import builtins
import csv
import hashlib
import io
import json
import math
import os
import statistics
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import patch

REFERENCE_SHA = "676ebe7783ed1899f9322a2f555f4e77fe966da5"
EXPECTED_MANIFEST_SHA = "4019064cfb4c6f359887a07fc33f9dce0dad0eeb048c3682e48d89c7eecbae82"
EXPECTED_BUNDLE_SHA = "c335f5f617bbc6e2fc47d2099509053c05ffbdd20bf8204b313c0529c4fa5b1f"
DATASET_BASE = os.environ.get("DATASET_BASE_URL", "http://bgx-research-dataset.railway.internal:8080").rstrip("/")
EVENT_START_MS = 1790176800000  # 2026-09-23T14:00:00Z
EVENT_END_MS = 1790191200000    # 2026-09-23T18:00:00Z
AUDIT_START_MS = 1790184000000  # 2026-09-23T16:00:00Z? overwritten below from ISO helper

CONTRACTS = {
    "BTCUSDT": "XBTUSDTM", "ETHUSDT": "ETHUSDTM", "SOLUSDT": "SOLUSDTM",
    "XRPUSDT": "XRPUSDTM", "ADAUSDT": "ADAUSDTM", "DOGEUSDT": "DOGEUSDTM",
    "LINKUSDT": "LINKUSDTM", "AVAXUSDT": "AVAXUSDTM", "DOTUSDT": "DOTUSDTM",
    "LTCUSDT": "LTCUSDTM", "NEARUSDT": "NEARUSDTM", "ATOMUSDT": "ATOMUSDTM",
}
TF_MIN = {"1m": 1, "15m": 15, "1h": 60, "4h": 240}


def ms_iso(s: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)

AUDIT_START_MS = ms_iso("2026-09-23T15:20:00Z")
AUDIT_END_MS = ms_iso("2026-09-23T16:20:00Z")

# Production structured HTF triage observations captured from the pinned live deployment.
# These are semantic state targets, not invented trading decisions.
T15_SHORT = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT","LTCUSDT","ATOMUSDT"]
BASE_1530 = {
    "BTCUSDT":("SHORT","NEUTRAL","LONG"), "ETHUSDT":("SHORT","NEUTRAL","LONG"),
    "SOLUSDT":("SHORT","NEUTRAL","LONG"), "XRPUSDT":("SHORT","NEUTRAL","LONG"),
    "ADAUSDT":("SHORT","NEUTRAL","LONG"), "DOGEUSDT":("SHORT","NEUTRAL","LONG"),
    "LINKUSDT":("SHORT","SHORT","NEUTRAL"), "AVAXUSDT":("SHORT","NEUTRAL","LONG"),
    "DOTUSDT":("SHORT","SHORT","NEUTRAL"), "LTCUSDT":("SHORT","NEUTRAL","LONG"),
    "NEARUSDT":("NEUTRAL","LONG","LONG"), "ATOMUSDT":("SHORT","NEUTRAL","LONG"),
}
BASE_1600 = {
    "BTCUSDT":("SHORT","NEUTRAL","NEUTRAL"), "ETHUSDT":("SHORT","NEUTRAL","NEUTRAL"),
    "SOLUSDT":("SHORT","NEUTRAL","NEUTRAL"), "XRPUSDT":("SHORT","NEUTRAL","NEUTRAL"),
    "ADAUSDT":("SHORT","NEUTRAL","NEUTRAL"), "DOGEUSDT":("SHORT","NEUTRAL","NEUTRAL"),
    "LINKUSDT":("SHORT","SHORT","NEUTRAL"), "AVAXUSDT":("SHORT","SHORT","NEUTRAL"),
    "DOTUSDT":("SHORT","SHORT","NEUTRAL"), "LTCUSDT":("SHORT","NEUTRAL","NEUTRAL"),
    "NEARUSDT":("NEUTRAL","NEUTRAL","LONG"), "ATOMUSDT":("SHORT","NEUTRAL","NEUTRAL"),
}
STATE_TARGETS = {
    "2026-09-23T15:30:05Z": BASE_1530,
    "2026-09-23T15:45:10Z": BASE_1530,
    "2026-09-23T16:00:20Z": BASE_1600,
    "2026-09-23T16:15:22Z": BASE_1600,
}
# Canonical strategy signals actually emitted by production.
SIGNAL_TARGETS = [
    ("2026-09-23T15:20:01Z", "NEARUSDT", "LONG", 80, 82, 77, 80, "MOMENTUM"),
    ("2026-09-23T15:30:04Z", "NEARUSDT", "LONG", 65, 82, 77, 47, "PULLBACK"),
    ("2026-09-23T15:45:09Z", "NEARUSDT", "LONG", 64, 82, 77, 45, "PULLBACK"),
]
# Exact late production diagnostic snapshot around 16:14:37-16:15:00Z.
LATE_NUMERIC = {
    "BTCUSDT":(40,53,71,"TRENDING_DOWN","NEUTRAL","NEUTRAL"),
    "ETHUSDT":(32,61,62,"TRENDING_DOWN","SHORT","NEUTRAL"),
    "SOLUSDT":(45,50,61,"TRENDING_DOWN","NEUTRAL","NEUTRAL"),
    "XRPUSDT":(35,45,60,"TRENDING_DOWN","NEUTRAL","NEUTRAL"),
    "ADAUSDT":(40,53,83,"TRENDING_DOWN","NEUTRAL","NEUTRAL"),
    "DOGEUSDT":(39,48,67,"TRENDING_DOWN","NEUTRAL","NEUTRAL"),
    "LINKUSDT":(42,64,73,"TRENDING_DOWN","SHORT","NEUTRAL"),
    "AVAXUSDT":(43,63,83,"TRENDING_DOWN","SHORT","NEUTRAL"),
    "DOTUSDT":(44,61,76,"TRENDING_DOWN","SHORT","NEUTRAL"),
    "LTCUSDT":(45,57,71,"TRENDING_DOWN","NEUTRAL","NEUTRAL"),
    "ATOMUSDT":(61,64,81,"TRENDING_DOWN","NEUTRAL","NEUTRAL"),
}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept":"*/*", "User-Agent":"bgx-policy-a-parity/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}: {url}")
        return r.read()


def safe_extract(blob: bytes, dst: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        root = dst.resolve()
        for m in tf.getmembers():
            p = (dst / m.name).resolve()
            if root not in p.parents and p != root:
                raise RuntimeError("unsafe tar member")
        tf.extractall(dst)


def load_csv(root: Path, contract: str, tf: str) -> list[dict]:
    p = root / "normalized" / f"{contract}_{tf}.csv"
    rows = []
    with p.open(newline="") as f:
        for r in csv.DictReader(f):
            # Production market-data integrity rewrites Futures activity/volume
            # to KuCoin transaction amount index 6. Collector stores that as turnover.
            rows.append({
                "ts": int(r["timestamp"]), "o": float(r["open"]), "h": float(r["high"]),
                "l": float(r["low"]), "c": float(r["close"]), "v": float(r["turnover"]),
            })
    rows.sort(key=lambda x: x["ts"])
    return rows


def forming_from_1m(one: list[dict], tf_min: int, t_ms: int) -> dict | None:
    width = tf_min * 60_000
    start = (t_ms // width) * width
    vals = [r for r in one if start <= r["ts"] and r["ts"] + 60_000 <= t_ms]
    if not vals:
        return None
    vals.sort(key=lambda x:x["ts"])
    return {
        "ts": start, "o": vals[0]["o"], "h": max(x["h"] for x in vals),
        "l": min(x["l"] for x in vals), "c": vals[-1]["c"],
        "v": sum(x["v"] for x in vals), "_research_forming": True,
        "_source_1m_count": len(vals),
    }


def series_at(data: dict, contract: str, tf: str, t_ms: int) -> list[dict]:
    width = TF_MIN[tf] * 60_000
    direct = data[(contract, tf)]
    confirmed = [r for r in direct if r["ts"] + width <= t_ms]
    form = forming_from_1m(data[(contract,"1m")], TF_MIN[tf], t_ms)
    return confirmed + ([form] if form is not None else [])


def closed_at(data: dict, contract: str, tf: str, t_ms: int) -> list[dict]:
    width = TF_MIN[tf] * 60_000
    return [r for r in data[(contract, tf)] if r["ts"] + width <= t_ms]


def ga(rows):
    return ([x["c"] for x in rows],[x["h"] for x in rows],[x["l"] for x in rows],
            [x["o"] for x in rows],[x["v"] for x in rows])


def diagnostics(strategy, shadow, data, internal: str, t_ms: int) -> dict:
    contract = CONTRACTS[internal]
    c15r = closed_at(data,contract,"15m",t_ms); c1r=closed_at(data,contract,"1h",t_ms); c4r=closed_at(data,contract,"4h",t_ms)
    if min(len(c15r),len(c1r),len(c4r)) < 10:
        return {"error":"insufficient"}
    c15,h15,l15,o15,v15=ga(c15r); c1,h1,l1,o1,v1=ga(c1r); c4,h4,l4,o4,v4=ga(c4r)
    s15state=shadow._ema_state(c15); s1state=shadow._ema_state(c1); s4state=shadow._ema_state(c4)
    a4=strategy.atr(h4,l4,c4); atr4=float(a4[-1]); regime=strategy.detect_regime(c4,h4,l4,atr4)
    desired = "SHORT" if s15state == "SHORT" else "LONG" if s15state == "LONG" else None
    primary = None
    if s15state != "SHORT": primary="WEAK_15M"
    elif s1state != "SHORT": primary="WEAK_1H"
    elif s4state != "SHORT": primary="WEAK_4H"
    else: primary="DOWNSTREAM"
    out={"state15":s15state,"state1h":s1state,"state4h":s4state,"regime":regime,"primary_short_reject":primary}
    # Score snapshots are diagnostic only; canonical code computes these after 4H+1H alignment.
    if desired:
        try:
            def score(c,h,l,o,v):
                aa=strategy.atr(h,l,c); av=float(aa[-1]); avg=float(sum(aa[-20:])/len(aa[-20:])) if len(aa)>=20 else av
                return strategy.score_tf(c,h,l,o,v,desired,av,avg)
            z4,z1,z15=score(c4,h4,l4,o4,v4),score(c1,h1,l1,o1,v1),score(c15,h15,l15,o15,v15)
            out.update(score4=z4.get("total"),score1=z1.get("total"),score15=z15.get("total"))
        except Exception as exc:
            out["score_error"]=type(exc).__name__
    return out


def call_policy_a(Analyzer, data, internal: str, t_ms: int):
    c=CONTRACTS[internal]
    k15=series_at(data,c,"15m",t_ms); k1=series_at(data,c,"1h",t_ms); k4=series_at(data,c,"4h",t_ms)
    # The outer production market-data-integrity wrapper obtains 'now' via time.time().
    # Freeze it to simulated T so candle authority is identical and no future candle can close early.
    with patch("time.time", return_value=t_ms/1000.0):
        return Analyzer().analyze_mtf(internal,k15,k1,k4,min_score=60,fee_mult=2.0,vol_mult=1.0)


def main():
    result={
        "reference_sha":REFERENCE_SHA,"policy_run":"A_ONLY","lookahead_bias":"NONE",
        "policies_c_d_e_run":False,"production_database_accessed":False,"exchange_action":False,
    }
    try:
        sc=getattr(builtins,"_nexus_sitecustomize_status","missing")
        result["sitecustomize_status"]=sc
        if sc != "ok": raise RuntimeError(f"production runtime bootstrap unavailable: {sc}")

        bundle=get(DATASET_BASE+"/bundle.tar.gz"); bsha=sha(bundle)
        result["input_bundle_sha256"]=bsha
        if bsha != EXPECTED_BUNDLE_SHA: raise RuntimeError(f"bundle hash mismatch {bsha}")
        with tempfile.TemporaryDirectory(prefix="bgx-policy-a-") as td:
            root=Path(td); safe_extract(bundle,root)
            mbytes=(root/"output"/"manifest.json").read_bytes(); msha=sha(mbytes)
            result["input_manifest_sha256"]=msha
            if msha != EXPECTED_MANIFEST_SHA: raise RuntimeError(f"manifest hash mismatch {msha}")
            validation=json.loads((root/"output"/"validation_report.json").read_text())
            if not validation.get("dataset_valid") or validation.get("slices_acquired") != 60 or validation.get("cross_timeframe_mismatches") != 0:
                raise RuntimeError("pinned dataset validation gate failed")
            result["input_hash_verified"]=True; result["dataset_valid"]=True

            data={}
            for internal,contract in CONTRACTS.items():
                for tf in TF_MIN:
                    data[(contract,tf)] = load_csv(root,contract,tf)

            from bot.strategy import Analyzer
            from bot import strategy
            from bot import htf_transition_shadow as shadow

            records=[]; exact=semantic=mismatch=not_rep=0
            direction_ok=direction_total=0; regime_ok=regime_total=0; decision_ok=decision_total=0; reject_ok=reject_total=0
            score_abs=[]

            # Structured HTF state + decision targets at production-equivalent snapshots.
            for ts,targets in STATE_TARGETS.items():
                t=ms_iso(ts)
                for sym,exp in targets.items():
                    d=diagnostics(strategy,shadow,data,sym,t)
                    got=(d.get("state15"),d.get("state1h"),d.get("state4h"))
                    state_match=(got==exp)
                    direction_total += 3; direction_ok += sum(a==b for a,b in zip(got,exp))
                    sig=call_policy_a(Analyzer,data,sym,t)
                    expected_decision = "LONG" if sym=="NEARUSDT" and ts in ("2026-09-23T15:30:05Z","2026-09-23T15:45:10Z") else "HOLD"
                    got_decision = sig.direction if sig else "HOLD"
                    decision_total += 1; decision_ok += int(got_decision==expected_decision)
                    # Primary short-reject classification expected from observed HTF state.
                    if exp[0] != "SHORT": exp_reject="WEAK_15M"
                    elif exp[1] != "SHORT": exp_reject="WEAK_1H"
                    elif exp[2] != "SHORT": exp_reject="WEAK_4H"
                    else: exp_reject="DOWNSTREAM"
                    reject_total += 1; reject_ok += int(d.get("primary_short_reject")==exp_reject)
                    # Late production forensic result established TRENDING_DOWN for 11/12; NEAR excluded.
                    if ts in ("2026-09-23T16:00:20Z","2026-09-23T16:15:22Z") and sym!="NEARUSDT":
                        regime_total+=1; regime_ok+=int(d.get("regime")=="TRENDING_DOWN")
                    cls="EXACT_MATCH" if state_match and got_decision==expected_decision else "MISMATCH"
                    if cls=="EXACT_MATCH": exact+=1
                    else: mismatch+=1
                    records.append({"timestamp":ts,"symbol":sym,"production_states":exp,"replay_states":got,
                                    "production_decision":expected_decision,"replay_decision":got_decision,
                                    "production_reject":exp_reject,"replay_reject":d.get("primary_short_reject"),
                                    "replay_regime":d.get("regime"),"classification":cls})

            # Numeric canonical signal anchors for NEAR: real production SINAL logs.
            for ts,sym,edir,escore,e4,e1,e15,eentry in SIGNAL_TARGETS:
                t=ms_iso(ts); sig=call_policy_a(Analyzer,data,sym,t); d=diagnostics(strategy,shadow,data,sym,t)
                if sig is None:
                    mismatch+=1
                    records.append({"timestamp":ts,"symbol":sym,"target":"canonical_signal","classification":"MISMATCH","reason":"replay_no_signal"})
                    continue
                got=(sig.direction,int(sig.score),getattr(sig,"entry_type",None)); exp=(edir,escore,eentry)
                # components are available only if diagnostic direction agrees with canonical direction; compute exact canonical components separately.
                contract=CONTRACTS[sym]
                c15,h15,l15,o15,v15=ga(closed_at(data,contract,"15m",t)); c1,h1,l1,o1,v1=ga(closed_at(data,contract,"1h",t)); c4,h4,l4,o4,v4=ga(closed_at(data,contract,"4h",t))
                def sc(c,h,l,o,v):
                    aa=strategy.atr(h,l,c); av=float(aa[-1]); avg=float(sum(aa[-20:])/len(aa[-20:])) if len(aa)>=20 else av
                    return strategy.score_tf(c,h,l,o,v,edir,av,avg)["total"]
                g4,g1,g15=sc(c4,h4,l4,o4,v4),sc(c1,h1,l1,o1,v1),sc(c15,h15,l15,o15,v15)
                score_abs.extend([abs(g4-e4),abs(g1-e1),abs(g15-e15)])
                ok=got==exp and (g4,g1,g15)==(e4,e1,e15)
                if ok: exact+=1
                else: mismatch+=1
                records.append({"timestamp":ts,"symbol":sym,"target":"canonical_signal",
                                "production":{"direction":edir,"score":escore,"score4":e4,"score1":e1,"score15":e15,"entry_type":eentry},
                                "replay":{"direction":sig.direction,"score":int(sig.score),"score4":g4,"score1":g1,"score15":g15,"entry_type":getattr(sig,"entry_type",None)},
                                "classification":"EXACT_MATCH" if ok else "MISMATCH"})

            # Exact late score/regime diagnostics are not canonical Policy-A score-stage values because production emitted them from LEGACY_FORMING_CANDLE_DIAG.
            # We preserve them as NOT_REPLAYABLE rather than falsely counting them as parity PASS.
            not_rep += len(LATE_NUMERIC)
            for sym,val in LATE_NUMERIC.items():
                records.append({"timestamp":"2026-09-23T16:14:58Z","symbol":sym,"target":"LEGACY_FORMING_CANDLE_DIAG",
                                "production":{"score4":val[0],"score1":val[1],"score15":val[2],"regime":val[3]},
                                "classification":"NOT_REPLAYABLE_FROM_AVAILABLE_INPUT",
                                "reason":"forming diagnostic uses intra-candle runtime state not fully represented by 1m close-only authoritative bundle"})

            # Whole audited selloff: minute-clock Policy A. Any SHORT is fatal parity mismatch.
            shorts=[]; long_signals=[]
            t=AUDIT_START_MS+1000
            while t < AUDIT_END_MS:
                for sym in CONTRACTS:
                    sig=call_policy_a(Analyzer,data,sym,t)
                    if sig:
                        row={"timestamp_ms":t,"symbol":sym,"direction":sig.direction,"score":int(sig.score),"entry_type":getattr(sig,"entry_type",None)}
                        (shorts if sig.direction=="SHORT" else long_signals).append(row)
                t += 60_000

            # Deduplicate repeated minute observations by (symbol,direction,score,entry_type,15m bucket).
            def dedup(rows):
                seen=set(); out=[]
                for r in rows:
                    k=(r["symbol"],r["direction"],r["score"],r["entry_type"],r["timestamp_ms"]//900000)
                    if k not in seen: seen.add(k); out.append(r)
                return out
            shorts=dedup(shorts); long_signals=dedup(long_signals)

            result.update({
                "total_comparable_evaluations":len(STATE_TARGETS)*12+len(SIGNAL_TARGETS),
                "exact_matches":exact,"semantic_matches":semantic,"mismatches":mismatch,"not_replayable":not_rep,
                "direction_match_rate":round(direction_ok/direction_total,6) if direction_total else None,
                "regime_match_rate":round(regime_ok/regime_total,6) if regime_total else None,
                "final_decision_match_rate":round(decision_ok/decision_total,6) if decision_total else None,
                "reject_reason_match_rate":round(reject_ok/reject_total,6) if reject_total else None,
                "score_mean_absolute_error":round(statistics.mean(score_abs),6) if score_abs else None,
                "current_policy_shorts_generated":len(shorts),"current_policy_short_records":shorts,
                "production_shorts_observed":0,"replay_long_signal_records":long_signals,
                "primary_parity_differences":[r for r in records if r.get("classification")=="MISMATCH"],
                "comparison_records":records,
            })
            material = mismatch==0 and len(shorts)==0 and direction_ok==direction_total and decision_ok==decision_total and reject_ok==reject_total and regime_ok==regime_total
            result["replay_parity"]="PASS" if material else "FAIL"
            result["counterfactual_allowed"] = bool(material)
            result["lookahead_bias"]="NONE"
    except Exception as exc:
        result.setdefault("input_hash_verified",False)
        result.setdefault("dataset_valid",False)
        result["replay_parity"]="FAIL"; result["counterfactual_allowed"]=False
        result["fatal_error"]=f"{type(exc).__name__}: {exc}"

    print("BGX_POLICY_A_PARITY_RESULT="+json.dumps(result,sort_keys=True,separators=(",",":")),flush=True)
    out=Path("/tmp/bgx-policy-a-result.json")
    out.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    if result.get("replay_parity") != "PASS":
        raise SystemExit(2)

if __name__ == "__main__":
    main()
