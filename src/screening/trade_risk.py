"""Price-derived risk estimates. Historical resistance is not a price forecast."""

import math
from typing import Dict, Optional, Tuple

import pandas as pd

from .signal_policy import MAX_STOP_DISTANCE, MIN_REWARD_RISK, MIN_STOP_DISTANCE


def _positive(value: object) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _support_stop(
    price_data: pd.DataFrame, current_price: float, phase_info: Dict, phase: int
) -> Tuple[Optional[float], str]:
    """Keep an observed support stop, including one outside the risk budget."""
    if not _positive(current_price) or "Low" not in price_data or price_data.empty:
        return None, "unavailable"
    lows = pd.to_numeric(
        price_data["Low"].tail(10 if phase == 2 else 30), errors="coerce"
    )
    if not lows.map(_positive).all():
        return None, "invalid_low_data"
    candidates = [(float(lows.min()) * (0.995 if phase == 2 else 0.99), "recent_low")]
    sma = phase_info.get("sma_50")
    if phase == 2 and _positive(sma):
        candidates.append((float(sma) * 0.99, "sma_50"))
    candidates = [
        (stop, basis) for stop, basis in candidates if 0 < stop < current_price
    ]
    if not candidates:
        return None, "no_support_below_entry"
    stop, basis = max(candidates)
    # Preserve the minimum-distance buffer, but never tighten a wide support
    # stop just to make its risk fit the budget.
    minimum_distance_stop = current_price * (1 - MIN_STOP_DISTANCE)
    if stop > minimum_distance_stop:
        return minimum_distance_stop, f"{basis}_with_minimum_distance_buffer"
    return stop, basis


def calculate_stop_loss(
    price_data: pd.DataFrame, current_price: float, phase_info: Dict, phase: int
) -> Optional[float]:
    """Return the support stop; callers must separately enforce their risk budget."""
    return _support_stop(price_data, current_price, phase_info, phase)[0]


def _resistance_target(price_data: pd.DataFrame, current_price: float) -> Dict:
    """Nearest higher pivot confirmed by two bars on each side, before entry.

    Only the previous 252 bars are searched. The entry bar cannot confirm a
    pivot. Missing/non-monotonic dates or an unconfirmed high yield no target.
    """
    unavailable = {
        "reward_target": None,
        "target_date": None,
        "target_basis": "unavailable",
    }
    if (
        "High" not in price_data
        or not isinstance(price_data.index, pd.DatetimeIndex)
        or price_data.index.hasnans
        or not price_data.index.is_unique
        or not price_data.index.is_monotonic_increasing
    ):
        return unavailable
    history = pd.to_numeric(price_data["High"].iloc[:-1].tail(252), errors="coerce")
    history = history.replace([float("inf"), float("-inf")], float("nan"))
    confirmed = history > current_price
    for offset in (-2, -1, 1, 2):
        neighbor = history.shift(offset)
        confirmed &= (neighbor > 0) & (history > neighbor)
    candidates = history[confirmed.fillna(False)]
    if candidates.empty:
        return unavailable
    date = candidates.idxmin()
    high = float(candidates.loc[date])
    return {
        "reward_target": high,
        "target_date": date.date().isoformat(),
        "target_basis": "nearest_confirmed_prior_swing_high",
    }


def assess_trade_risk(
    price_data: pd.DataFrame, current_price: float, phase_info: Dict, phase: int
) -> Dict:
    """Describe missing evidence separately from an explicitly failing risk gate.

    An unavailable target grants no R/R points, but is not itself a buy veto.
    Invalid/wide stops and observed R/R below the minimum veto a buy regardless
    of its other scores. All decisions use unrounded prices.
    """
    stop, basis = _support_stop(price_data, current_price, phase_info, phase)
    result = {
        "stop_loss": stop,
        "stop_basis": basis,
        "risk_amount": None,
        "risk_pct": None,
        "reward_amount": None,
        "risk_reward_ratio": None,
        "reward_target": None,
        "target_date": None,
        "target_basis": "unavailable",
        "risk_status": "invalid_stop",
        "risk_eligible": False,
    }
    if stop is None:
        return result
    risk = current_price - stop
    result.update(risk_amount=risk, risk_pct=risk / current_price)
    result.update(_resistance_target(price_data, current_price))
    if result["reward_target"] is not None:
        reward = result["reward_target"] - current_price
        result.update(reward_amount=reward, risk_reward_ratio=reward / risk)
    if result["risk_pct"] > MAX_STOP_DISTANCE + 1e-12:
        result["risk_status"] = "stop_too_wide"
    elif result["risk_reward_ratio"] is None:
        result.update(risk_status="target_unavailable", risk_eligible=True)
    elif result["risk_reward_ratio"] < MIN_REWARD_RISK:
        result["risk_status"] = "insufficient_reward_risk"
    else:
        result.update(risk_status="eligible", risk_eligible=True)
    return result


def format_trade_risk(signal: Dict) -> str:
    """Render missing risk evidence without treating it as zero or a forecast."""
    details = signal.get("details", {})
    lines = []
    stop = signal.get("stop_loss")
    if stop is not None:
        lines.append(f"Stop Loss: ${stop:.2f} ({details.get('stop_basis', 'support')})")
    else:
        lines.append("Stop Loss: unavailable")
    rr = signal.get("risk_reward_ratio")
    target = details.get("reward_target", signal.get("reward_target"))
    if rr is None or target is None:
        lines.append("Risk/Reward: unavailable — no supported target; 0 R/R points")
    else:
        risk_amount = details.get("risk_amount")
        reward_amount = details.get("reward_amount")
        amounts = (
            f" (Risk ${risk_amount:.2f}, Reward ${reward_amount:.2f})"
            if risk_amount is not None and reward_amount is not None
            else ""
        )
        lines.append(f"Risk/Reward: {rr:.2f}:1{amounts}")
        lines.append(
            f"Observed resistance: ${target:.2f} on {details.get('target_date', 'unknown date')} "
            "(technical reference, not a forecast)"
        )
    if details.get("risk_pct") is not None:
        lines.append(f"Stop distance: {details['risk_pct']:.1%}")
    return "\n".join(lines)
