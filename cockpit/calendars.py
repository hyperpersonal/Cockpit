"""US trading-day + market-phase via exchange-calendars (ported principle from ZhuLinsen full-guide)."""
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

def market_phase(now_utc: dt.datetime | None = None) -> str:
    """Rough US phase for a brief that fires ~US midday (China 00:00)."""
    now = now_utc or dt.datetime.utcnow()
    if not is_us_trading_day(now.date()):
        return "non_trading"
    # US regular session 13:30-20:00 UTC (EDT). Our cron 16:00 UTC = intraday.
    t = now.time()
    if t < dt.time(13, 30):  return "premarket"
    if t >= dt.time(20, 0):  return "postmarket"
    if t >= dt.time(19, 55): return "closing_auction"
    return "intraday"

PHASE_GUARDRAIL = {
    "intraday": "盘中快照：最后一根日线未完成，勿当收盘复盘；给的是盘中观察+收盘前可操作窗口。",
    "premarket": "盘前：勿把今日走势当已发生；看上一完整交易日+隔夜+开盘触发条件。",
    "postmarket": "盘后：完整交易日复盘。",
    "non_trading": "非交易日：美股休市，不发/只发休市说明。",
}
