"""IBKR positions via Flex Web Service (Activity Flex Query). Standard Flex XML schema.
Two-step flow: SendRequest (token+queryId) -> ReferenceCode -> GetStatement (poll) -> parse.
FAIL-OPEN: any error -> return None so the brief still runs and labels a data gap (never fabricates).

IMPORTANT (dedupe): if the Flex Open Positions section has BOTH Summary and Lot level of detail,
each symbol appears in MULTIPLE <OpenPosition> rows. We must NOT sum them (that doubles the
position). Per symbol we keep ONE row: the SUMMARY row (or, if unlabeled, the row with the
largest |position|, which is the aggregate)."""
from __future__ import annotations
import os, time, logging, xml.etree.ElementTree as ET
import requests

log = logging.getLogger("cockpit.ibkr")
SEND = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
VER = "3"

def _f(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0

def _fetch_xml() -> str | None:
    token = os.getenv("IBKR_FLEX_TOKEN"); query = os.getenv("IBKR_FLEX_QUERY_ID")
    if not (token and query):
        log.warning("IBKR not configured -> portfolio block will be a labeled data gap")
        return None
    try:
        r = requests.get(SEND, params={"t": token, "q": query, "v": VER}, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        if (root.findtext("Status") or "").strip() != "Success":
            log.warning("IBKR SendRequest not Success: %s", r.text[:200]); return None
        ref = root.findtext("ReferenceCode")
        base = root.findtext("Url") or "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
        for _ in range(8):
            g = requests.get(base, params={"t": token, "q": ref, "v": VER}, timeout=30)
            g.raise_for_status()
            if ("FlexQueryResponse" in g.text and "<OpenPosition" in g.text) or "EquitySummary" in g.text:
                return g.text
            if "Statement generation in progress" in g.text or "<Status>Warn" in g.text:
                time.sleep(5); continue
            return g.text
        return None
    except Exception as e:
        log.warning("IBKR fetch failed: %s", e); return None

def _parse(xml: str) -> dict | None:
    try:
        root = ET.fromstring(xml)
        best = {}                                          # sym -> (rank, attrib) ; dedupe Summary vs Lot
        for o in root.iter("OpenPosition"):
            a = o.attrib
            sym = a.get("symbol")
            if not sym or a.get("assetCategory", "STK") not in ("STK", ""):
                continue
            qty = _f(a.get("position"))
            if qty == 0:
                continue
            lod = (a.get("levelOfDetail") or "").upper()
            rank = (1 if lod == "SUMMARY" else 0, abs(qty))   # prefer SUMMARY, else largest |qty| (aggregate)
            if sym not in best or rank > best[sym][0]:
                best[sym] = (rank, a)
        positions = {}
        for sym, (rank, a) in best.items():
            qty = _f(a.get("position"))
            mv = _f(a.get("positionValue")) or _f(a.get("markPrice")) * qty
            avg = _f(a.get("costBasisPrice")) or (_f(a.get("costBasisMoney")) / qty if qty else 0.0)
            positions[sym] = {"shares": qty, "avg_price": avg, "mv": mv}
        net_liq = cash = 0.0
        rows = list(root.iter("EquitySummaryByReportDateInBase")) or list(root.iter("EquitySummaryInBase"))
        if rows:
            last = rows[-1].attrib
            net_liq = _f(last.get("total")); cash = _f(last.get("cash"))
            as_of = last.get("reportDate")
        if not net_liq:
            net_liq = sum(p["mv"] for p in positions.values()) + cash
        if not positions and not net_liq:
            return None
        return {"net_liq": net_liq, "cash": cash, "positions": positions, "as_of": locals().get("as_of")}
    except Exception as e:
        log.warning("IBKR parse failed: %s", e); return None

def get_portfolio() -> dict | None:
    """Return {"net_liq": float, "cash": float,
              "positions": {TICKER: {"shares","avg_price","mv"}}} or None (fail-open)."""
    xml = _fetch_xml()
    return _parse(xml) if xml else None
