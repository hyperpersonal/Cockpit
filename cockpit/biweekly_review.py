"""Bi-weekly review (China Sat, every 2 weeks). Performance vs benchmark + per-holding thesis
review + sub-theme rotation (RS vs SPY) + reflection-memory injection. Fires only on cadence.
ASCII-only source; LLM writes the review in Chinese (7-section 宪法 format)."""
from __future__ import annotations
import os, json, datetime as dt, pathlib, yaml
from . import fmp, ibkr, risk, screener, llm, notify, calendars
from .memory import ReflectionMemory

ROOT = pathlib.Path(__file__).resolve().parent.parent
try:
    CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
except Exception:
    CFG = {}

def _universe() -> list:
    syms = set([CFG.get("benchmark", "SPY"), "QQQ"])
    for v in CFG.get("subthemes", {}).values():
        syms |= set(v.get("etfs", [])) | set(v.get("names", []))
    syms |= {h["ticker"] for h in CFG.get("holdings", [])}
    return sorted(syms)

def _is_review_week() -> bool:
    anchor = dt.date.fromisoformat(CFG["schedule"]["biweekly_anchor_date"])
    return (dt.date.today() - anchor).days % 14 == 0

def build() -> str:
    holdings = [h["ticker"] for h in CFG["holdings"]]
    exclude = set(CFG.get("exclude", []))
    bench = CFG.get("benchmark", "SPY")
    quotes = screener.quote_map(_universe())
    bench_vs200 = 0.0
    if bench in quotes and quotes[bench].get("priceAvg200"):
        bench_vs200 = round((quotes[bench]["price"] / quotes[bench]["priceAvg200"] - 1) * 100, 1)
    setups = {t: screener.name_setup(t, quotes[t], CFG["risk"]["no_chase_bias_threshold_pct"], bench_vs200)
              for t in holdings if t in quotes}
    subs = screener.subtheme_strength(CFG["subthemes"], quotes, bench_vs200)
    candidates = screener.rank_candidates(CFG["subthemes"], quotes, bench_vs200,
                                          set(holdings) | exclude, top=10)
    port = ibkr.get_portfolio()
    mem = ReflectionMemory(str(ROOT / "state" / "reflection_memory.json"))
    lessons = mem.retrieve("biweekly review: which holdings lag the leading main-line; rotation; "
                           "what worked vs not; trim laggards; correlation concentration", n=4)
    bundle = dict(date=dt.date.today().isoformat(), benchmark=bench, bench_vs200=bench_vs200,
                  holdings=quotes, setups=setups, subthemes=subs, new_candidates=candidates,
                  portfolio=port, lessons=lessons,
                  note="perf vs benchmark needs IBKR get_pa_performance(TWR); mark pending if IBKR down.")
    prompt = ("Write a CHINESE biweekly review from the REAL data below, 7 sections: (1) performance "
              "vs benchmark (TWR; pending if IBKR down), (2) main-line/sector rotation from subthemes "
              "(rel_vs_spy, lifecycle, breadth), (3) per-holding thesis: is each still on the leading "
              "main-line? (use setups + subthemes), (4) risk exposure, (5) reflection memory "
              "(right/wrong + lesson from lessons), (6) next-period plan + operation tips (checklist) "
              "incl top new_candidates to rotate toward, (7) 待验证. Never output buy/sell orders. "
              "Do not use prior knowledge for current prices.\n\n"
              + json.dumps(bundle, ensure_ascii=False, default=str)[:90000])
    return llm.run(prompt, model=CFG["models"]["biweekly"], max_tokens=4200)

def main():
    if not _is_review_week() and os.getenv("FORCE_RUN", "false").lower() != "true":
        print("non-review week, skip."); return
    try:
        body = build()
    except Exception as e:
        body = "system degraded: biweekly review error (%s). check data/config." % e
    notify.send("biweekly review %s" % dt.date.today().isoformat(), body)
    print(body)
    # TODO reflection: after a position closes, call mem.add(...)+mem.save() (needs realized return).

if __name__ == "__main__":
    main()
