"""US trading-day + market-phase via exchange-calendars (ported principle from ZhuLinsen full-guide).

B12: phase boundaries are derived from the exchange calendar's ACTUAL session open/close for the
given date, so they stay correct across EST/EDT (the regular close is 20:00 UTC in summer but 21:00
UTC in winter) and on half-days (early close). Falls back to the EDT-fixed boundaries only if the
calendar is unavailable."""
from __future__ import annotations
import datetime as dt, logging
log = logging.getLogger("cockpit.cal")
try:
    import exchange_calendars as xcals
    _XNYS = xcals.get_calendar("XNYS")
except Exception as e:                          # fail-open
    _XNYS = None
    log.warning("exchange_calendars unavailable: %s", e)

def is_us_trading_day(d: dt.date | None = None) -> bool:
    d = d or dt.date.today()
    if _XNYS is None:
        return d.weekday() < 5                  # fallback: weekday (won't skip holidays)
    return _XNYS.is_session(d.isoformat())

def _to_utc_time(ts) -> dt.time:
    """pandas Timestamp -> naive UTC datetime.time (handles tz-aware and tz-naive)."""
    try:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_convert("UTC")
    except Exception:
        pass
    return ts.time()

def _session_utc(d: dt.date):
    """(open_utc, close_utc) as datetime.time for trading day d via exchange_calendars
    (DST- and half-day-aware), or None if unavailable."""
    if _XNYS is None:
        return None
    try:
        o = _XNYS.session_open(d.isoformat())
        c = _XNYS.session_close(d.isoformat())
        return _to_utc_time(o), _to_utc_time(c)
    except Exception as e:
        log.warning("session bounds unavailable for %s: %s", d, e)
        return None

def market_phase(now_utc: dt.datetime | None = None) -> str:
    """US phase for a brief that fires ~US midday (China 00:00). Boundaries are the date's REAL
    UTC session open/close from the exchange calendar (DST-correct), not a fixed EDT assumption."""
    now = now_utc or dt.datetime.utcnow()
    if not is_us_trading_day(now.date()):
        return "non_trading"
    t = now.time()
    sess = _session_utc(now.date())
    if sess:
        open_t, close_t = sess
    else:
        open_t, close_t = dt.time(13, 30), dt.time(20, 0)    # fallback: EDT regular session
    if t < open_t:
        return "premarket"
    if t >= close_t:
        return "postmarket"
    close_dt = dt.datetime.combine(now.date(), close_t)
    if t >= (close_dt - dt.timedelta(minutes=5)).time():
        return "closing_auction"
    return "intraday"

PHASE_GUARDRAIL = {
    "intraday": "盘中快照：最后一根日线未完成，勿当收盘复盘；给的是盘中观察+收盘前可操作窗口。",
    "premarket": "盘前：勿把今日走势当已发生；看上一完整交易日+隔夜+开盘触发条件。",
    "postmarket": "盘后：完整交易日复盘。",
    "closing_auction": "尾盘竞价：临近收盘，价格基本定型但未最终确认；可据此做收盘前最后决策。",
    "non_trading": "非交易日：美股休市，不发/只发休市说明。",
}
