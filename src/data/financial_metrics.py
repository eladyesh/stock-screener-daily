"""Comparable quarterly metrics; missing values never become zero observations."""

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd


def completed_daily_bars(frame, now=None):
    """Exclude partial US sessions and future rows; conservative on early closes."""
    if frame.empty:
        return frame
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    complete_today = (now.hour, now.minute) >= (16, 15)
    dates = pd.DatetimeIndex(frame.index).date
    mask = (dates <= now.date()) if complete_today else (dates < now.date())
    return frame.loc[mask].copy()


def number(value):
    """Return a finite number or None, preserving a genuine zero."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def growth(current, previous):
    """Percentage growth requires a positive comparison base."""
    current, previous = number(current), number(previous)
    if current is None or previous is None or previous <= 0:
        return None
    return 100 * (current / previous - 1)


def series(frame, *names):
    """Find the first supported statement row with dated finite observations."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.Series(dtype=float)
    for name in names:
        if name in frame.index:
            row = pd.to_numeric(frame.loc[name], errors="coerce")
            row.index = pd.to_datetime(row.index)
            return row.sort_index().replace([float("inf"), -float("inf")], float("nan"))
    return pd.Series(dtype=float)


def matching_year(row, end):
    """Match the same fiscal quarter, including 52/53-week calendars."""
    if end is None or row.empty:
        return None
    matches = [
        (abs((end - d).days - 365), d)
        for d in row.index
        if 330 <= (end - d).days <= 400
    ]
    return min(matches)[1] if matches else None


def ttm(row, end):
    """Sum exactly four consecutive quarters ending at the requested date."""
    if row.empty or end not in row.index:
        return None
    values = row.loc[:end].tail(4)
    if len(values) != 4 or values.isna().any():
        return None
    gaps = values.index.to_series().diff().dropna().dt.days
    if not gaps.between(65, 115).all():
        return None
    return number(values.sum())


def statement_metrics(income, balance, cashflow):
    """Normalize matched periods and retain the original series for screening."""
    rev = series(income, "Total Revenue", "Operating Revenue")
    if rev.empty:
        return {}
    # Ignore empty future/provider placeholder columns, but never bridge missing quarters.
    valid = rev.dropna()
    if valid.empty:
        return {}
    end = valid.index[-1]
    prev_year = matching_year(rev, end)
    eps = series(income, "Diluted EPS", "Basic EPS")
    op = series(income, "Operating Income")
    gross = series(income, "Gross Profit")
    net = series(income, "Net Income", "Net Income Common Stockholders")
    inv = series(balance, "Inventory")
    shares = series(income, "Diluted Average Shares", "Basic Average Shares")
    debt = series(balance, "Total Debt")
    cash = series(
        balance,
        "Cash Cash Equivalents And Short Term Investments",
        "Cash And Cash Equivalents",
    )
    assets = series(balance, "Total Assets")
    ocf = series(
        cashflow,
        "Operating Cash Flow",
        "Cash Flow From Continuing Operating Activities",
    )
    fcf = series(cashflow, "Free Cash Flow")
    if fcf.empty:
        capex = series(cashflow, "Capital Expenditure")
        fcf = ocf - capex.abs()  # Providers use either sign for cash spent on capex.

    def at(row, date=end):
        return number(row.get(date)) if date is not None else None

    def margin(row, date=end):
        numerator, denominator = at(row, date), at(rev, date)
        return (
            numerator / denominator * 100
            if numerator is not None and denominator is not None and denominator > 0
            else None
        )

    previous = rev.index[rev.index < end]
    prior = previous[-1] if len(previous) else None
    if prior is not None and not 65 <= (end - prior).days <= 115:
        prior = None
    data = {
        "schema_version": 2,
        "data_source": "yahoo_finance",
        "fetch_date": datetime.now(timezone.utc).isoformat(),
        "report_period": end.date().isoformat(),
        "revenue_yoy_change": growth(at(rev), at(rev, prev_year)),
        "revenue_qoq_change": growth(at(rev), at(rev, prior)),
        "eps_yoy_change": growth(at(eps), at(eps, prev_year)),
        "eps_qoq_change": growth(at(eps), at(eps, prior)),
        "latest_eps": at(eps),
        "gross_margin": margin(gross),
        "operating_margin": margin(op),
        "net_margin": margin(net),
        "inventory_yoy_change": growth(at(inv), at(inv, matching_year(inv, end))),
        "inventory_qoq_change": growth(at(inv), at(inv, prior)),
        "shares_yoy_change": growth(at(shares), at(shares, matching_year(shares, end))),
        "free_cash_flow_ttm": ttm(fcf, end),
        "operating_cash_flow_ttm": ttm(ocf, end),
        "revenue_ttm": ttm(rev, end),
        "net_income_ttm": ttm(net, end),
        "ebitda_ttm": ttm(series(income, "EBITDA", "Normalized EBITDA"), end),
        "total_debt": at(debt),
        "cash": at(cash),
        "total_assets": at(assets),
        "inventory_breakdown_available": False,
    }
    for name, row in (
        ("quarterly_revenue", rev),
        ("quarterly_eps", eps),
        ("quarterly_inventory", inv),
    ):
        data[name] = {
            d.date().isoformat(): number(v) for d, v in row.items() if d <= end
        }
    for key, row in (("margin_change", gross), ("operating_margin_yoy_change", op)):
        date = prior if key == "margin_change" else prev_year
        current, previous = margin(row), margin(row, date)
        data[key] = (
            current - previous if current is not None and previous is not None else None
        )
    data["inventory_to_sales_ratio"] = (
        at(inv) / at(rev) if at(inv) is not None and at(rev) and at(rev) > 0 else None
    )
    if (
        data["free_cash_flow_ttm"] is not None
        and data["revenue_ttm"]
        and data["revenue_ttm"] > 0
    ):
        data["fcf_margin"] = data["free_cash_flow_ttm"] / data["revenue_ttm"] * 100
    if at(debt) is not None and at(cash) is not None:
        data["net_debt"] = at(debt) - at(cash)
        if data["ebitda_ttm"] and data["ebitda_ttm"] > 0:
            data["net_debt_to_ebitda"] = data["net_debt"] / data["ebitda_ttm"]
    if at(debt) is not None and at(assets) and at(assets) > 0:
        data["debt_to_assets"] = at(debt) / at(assets) * 100
    return data
