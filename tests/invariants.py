"""Pure invariant checkers. No I/O, no network, no imports from cockpit.

Each function returns a LIST OF VIOLATION STRINGS (empty list == invariant holds).
They are written this way so the same checker can be pointed at (a) the historical
2026-08-28 email, where it MUST find violations -- otherwise the checker is asleep --
and (b) the system's own output, where it must find none.

Lesson behind the shape (2026-08-28, selfcheck gate 10): a checker that can be
fooled is not a checker. Every invariant here is exercised in both directions.
"""
from __future__ import annotations

TOL = 1.0   # dollars; the renderer rounds, so 1-dollar disagreement is rounding, not a defect


def one_amount_per_ticker(sections: dict) -> list:
    """sections: {section_name: {ticker: usd}}. A ticker must carry ONE amount everywhere."""
    seen = {}
    for sec, amounts in sections.items():
        for t, usd in (amounts or {}).items():
            seen.setdefault(t, []).append((sec, float(usd)))
    bad = []
    for t, pairs in sorted(seen.items()):
        vals = [v for _s, v in pairs]
        if max(vals) - min(vals) > TOL:
            bad.append("%s carries %d different amounts: %s" % (
                t, len(set(vals)), ", ".join("%s=%.0f" % (s, v) for s, v in pairs)))
    return bad


def one_sell_total(totals: dict) -> list:
    """totals: {section_name: usd}. One brief, one final sell total."""
    if not totals:
        return []
    vals = list(totals.values())
    if max(vals) - min(vals) > TOL:
        return ["%d different sell totals in one brief: %s" % (
            len(set(vals)), ", ".join("%s=%.0f" % (k, v) for k, v in sorted(totals.items())))]
    return []


def as_of_not_overwritten_by_run_date(as_of: str, recorded_under: str) -> list:
    """Flex data stamped as_of=X must be recorded under X, never under the run date."""
    a = (as_of or "").replace("-", "")
    r = (recorded_under or "").replace("-", "")
    if a and r and a != r:
        return ["portfolio data as_of=%s was recorded under date=%s" % (as_of, recorded_under)]
    return []


def exits_present_in_attribution(exited: list, attributed: list) -> list:
    """Every name that left the book during the window must appear in the attribution."""
    missing = [t for t in (exited or []) if t not in set(attributed or [])]
    return ["exited names missing from attribution: %s" % ", ".join(sorted(missing))] if missing else []


def overrides_reach_risk_paths(theme_overrides: dict, theme_map_used_by_risk: dict) -> list:
    """Every ticker whose layer comes ONLY from risk.theme_overrides must still be visible
    to the correlation / peer-expansion map, or its same-theme crowding is invisible."""
    missing = [t for t in sorted(theme_overrides or {})
               if theme_overrides[t] and t not in (theme_map_used_by_risk or {})]
    return ["theme_overrides not reaching the risk/correlation map: %s" % ", ".join(missing)] if missing else []


def no_out_of_scope_advice(text: str, forbidden: list) -> list:
    """The IBKR brief must not carry Schwab/QQQ management advice (explicitly out of scope)."""
    hits = [w for w in (forbidden or []) if w and w in (text or "")]
    return ["out-of-scope content in the IBKR brief: %s" % ", ".join(hits)] if hits else []


def no_residual_noise(proposals: list, net_liq: float, min_pct_nav: float, min_usd: float) -> list:
    """A suggestion whose whole position is below the noise floor is not a decision."""
    bad = []
    for p in proposals or []:
        mv = float(p.get("market_value") or 0)
        amt = float(p.get("amount_usd") or 0)
        if net_liq and mv / net_liq * 100 < min_pct_nav and amt < min_usd:
            bad.append("%s: position $%.0f (%.3f%% NAV), suggestion $%.0f -- below noise floor"
                       % (p.get("ticker"), mv, mv / net_liq * 100, amt))
    return bad


def nav_identity(stock, cash, interest_accruals, dividend_accruals, nav, tol=0.02) -> list:
    """NAV is NOT stock + cash. Accruals are part of it."""
    calc = stock + cash + interest_accruals + dividend_accruals
    if abs(calc - nav) > tol:
        return ["NAV identity broken: stock+cash+accruals=%.2f but NAV=%.2f (gap %.2f)"
                % (calc, nav, calc - nav)]
    return []


def theme_ceiling_respected(exposures, net_liq, thr_pct) -> list:
    """After every Decision is applied, no theme may exceed its ceiling."""
    limit = net_liq * thr_pct / 100.0
    return ["%s exposure %.2f exceeds the %.0f%% ceiling (%.2f)" % (k, v, thr_pct, limit)
            for k, v in sorted((exposures or {}).items())
            if k != "unmapped" and v > limit + 1.0]


def no_unnecessary_theme_cut(exposures, net_liq, thr_pct, theme_bound) -> list:
    """Minimality. Scheme (a) removes exactly the excess, so any theme the theme rule actually
    bound must land ON its ceiling -- landing below it means something was sold that did not
    need to be. This is the check that would have caught the 2026-08-28 over-sell: the theme
    ended at 0 exposure against a 60,000 ceiling."""
    limit = net_liq * thr_pct / 100.0
    bad = []
    for theme in sorted(theme_bound or []):
        v = (exposures or {}).get(theme, 0.0)
        if v < limit - 1.0:
            bad.append("%s was cut to %.2f but its ceiling is %.2f -- %.2f of exposure was "
                       "removed unnecessarily" % (theme, v, limit, limit - v))
    return bad
