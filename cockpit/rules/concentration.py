"""Tier 3: single-name hard cap + sub-theme concentration.

Both are BINDING: they are the ceilings the user has actually committed to.

single_name_hard_cap: a fixed $30,000 absolute ceiling, resolved by
cockpit.domain.policy.hard_cap_usd() and nowhere else. User decision 2026-08-28 -- it is an
absolute limit derived from TOTAL wealth risk tolerance, not a percentage of anything, and it
does not move when the Schwab side (which this system does not manage) is re-estimated.

theme cap: leverage-adjusted sub-theme exposure may not exceed risk.theme_exposure_alert_pct
of NAV (40%). Leveraged ETFs count at their leverage factor (red line v2 clause 6).

ALLOCATION -- scheme (a), user decision 2026-08-28, replacing weakest-RS-first:
    1. apply the fixed $30,000 single-name hard cap
    2. recompute the theme's leverage-adjusted exposure
    3. if still above the ceiling, cut the LEVERAGED members first -- every $1 sold removes
       $leverage_factor of theme exposure, so they are the cheapest way back under it
    4. if the leveraged members reach zero and the theme is still over, cut the ordinary
       members pro-rata by their share of the remaining theme exposure

RS DOES NOT APPEAR HERE. It is observation only: it may not decide a binding sell amount,
an allocation order, or an exit. The rule it replaced ordered the layer by RS, and on the
2026-08-27 book that happened to give the right answer purely because RAM was both the
leveraged product AND the weakest name. Flip RAM's RS to the strongest in its layer and the
old rule left the 2x ETF untouched and sold $26,097 of SKHY instead -- the opposite of what
red line v2 wants when a theme is over its ceiling. A rule that is only correct by
coincidence is not a rule.
"""
from __future__ import annotations
from ..domain.models import RuleProposal, CAP_VALUE, TIER_CONCENTRATION
from ..domain.policy import hard_cap_usd

RULE_HARD_CAP = "concentration.single_name_hard_cap"
RULE_THEME    = "concentration.theme_cap"


def layer_of(ticker, cfg):
    """The ONE place a ticker's layer is resolved. theme_overrides wins, then
    config.subthemes[*].names, then 'unmapped' (a data gap, never a verdict)."""
    rk = (cfg.get("risk") or {})
    ov = (rk.get("theme_overrides") or {}).get(ticker)
    if ov:
        return ov
    for name, v in (cfg.get("subthemes") or {}).items():
        if ticker in (v.get("names") or []):
            return name
    return "unmapped"


def theme_map(tickers, cfg):
    """Complete ticker -> layer map. This is what every risk path must receive --
    the 2026-08-28 defect was that risk.position_caps() got a map built from
    config.subthemes ONLY, so all seven theme_overrides names (SKHY 31% NAV and
    MU 24.5% NAV among them) looked themeless and got no same-theme correlation floor."""
    return {t: layer_of(t, cfg) for t in tickers}


def leverage_of(ticker, cfg):
    return float(((cfg.get("risk") or {}).get("leverage_factors") or {}).get(ticker, 1.0))


def propose(positions, net_liq, cfg):
    """positions: {ticker: {"market_value": float, ...}}. Returns list[RuleProposal].

    Deliberately takes NO rs argument. Removing the parameter rather than leaving it unused
    is the point: an unused parameter is an invitation for RS to creep back into a binding
    amount, and RS is observation only (user decision 2026-08-28)."""
    rk = (cfg.get("risk") or {})
    hard_cap = hard_cap_usd(cfg)          # the ONE definition (cockpit/domain/policy.py)
    thr_pct = float(rk.get("theme_exposure_alert_pct", 40))

    out = []
    capped_mv = {}
    for t, p in positions.items():
        mv = float(p.get("market_value") or 0)
        if mv > hard_cap + 1:
            out.append(RuleProposal(
                rule_id=RULE_HARD_CAP, ticker=t, kind=CAP_VALUE, tier=TIER_CONCENTRATION,
                target_value=hard_cap,
                reason="单票绝对硬顶 $%s（全体财富风险承受上限，固定值）" % format(int(hard_cap), ","),
                evidence={"market_value": round(mv, 2), "cap_usd": hard_cap,
                          "invalidation": "该硬顶为固定绝对值，仅在双周复盘日可修改"}))
            capped_mv[t] = hard_cap
        else:
            capped_mv[t] = mv

    if not net_liq:
        return out

    # exposure AFTER the single-name cap, leverage-adjusted
    lay = theme_map(list(positions), cfg)
    expo = {}
    for t, mv in capped_mv.items():
        expo.setdefault(lay[t], 0.0)
        expo[lay[t]] += mv * leverage_of(t, cfg)

    limit = net_liq * thr_pct / 100.0
    for theme, usd in sorted(expo.items()):
        if theme == "unmapped" or usd <= limit + 1:
            continue
        need = usd - limit                      # leverage-adjusted dollars still to remove
        members = [t for t in capped_mv if lay[t] == theme]
        # step 3 -- leveraged members first. Deterministic order: biggest leverage-adjusted
        # exposure first, ties broken by ticker. NOT by RS, and not by anything that moves
        # with the market.
        leveraged = sorted([t for t in members if leverage_of(t, cfg) > 1.0],
                           key=lambda t: (-capped_mv[t] * leverage_of(t, cfg), t))
        targets = {}
        for t in leveraged:
            if need <= 1:
                break
            lev = leverage_of(t, cfg)
            avail = capped_mv[t] * lev
            take = min(avail, need)
            need -= take
            targets[t] = round((avail - take) / lev, 2)
        # step 4 -- ordinary members, pro-rata on their share of the REMAINING theme exposure
        if need > 1:
            ordinary = sorted(t for t in members if leverage_of(t, cfg) == 1.0
                              and capped_mv[t] > 0)
            pool = sum(capped_mv[t] for t in ordinary)
            if pool > 0:
                scale = max((pool - need) / pool, 0.0)
                for t in ordinary:
                    targets[t] = round(capped_mv[t] * scale, 2)
                need = 0.0
        for t in sorted(targets):
            lev = leverage_of(t, cfg)
            kind_cn = ("杠杆产品优先减仓（每卖 $1 抵减 $%.0f 敞口）" % lev) if lev > 1.0 \
                else "杠杆产品已减至 0 仍超限 → 普通成员按敞口占比同比例削减"
            out.append(RuleProposal(
                rule_id=RULE_THEME, ticker=t, kind=CAP_VALUE, tier=TIER_CONCENTRATION,
                target_value=targets[t],
                reason="%s 敞口 %.1f%% 净值 > %.0f%% 上限；%s" % (
                    theme, usd / net_liq * 100, thr_pct, kind_cn),
                evidence={"theme": theme, "theme_usd": round(usd, 2), "limit_usd": round(limit, 2),
                          "leverage": lev, "allocation": "scheme_a_leveraged_first",
                          "invalidation": "%s 敞口回落至 %.0f%% 净值以下" % (theme, thr_pct)}))
    return out
