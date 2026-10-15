"""Bounded legacy growth score, with no points awarded for missing evidence."""

from src.data.financial_metrics import number


def scale(value, low, high):
    """Map a finite observation to [0, 100]; absent observations remain absent."""
    value = number(value)
    if value is None:
        return None
    return max(0.0, min(100.0, (value - low) / (high - low) * 100))


def score_fundamentals(data):
    """Return up to 40 points using YoY comparisons, not seasonal QoQ penalties."""
    data = data or {}
    specs = [
        ("revenue_yoy_change", 15, 0, 30),
        ("eps_yoy_change", 15, -20, 60),
        ("operating_margin_yoy_change", 5, -3, 3),
    ]
    points, covered, reasons = 0.0, 0.0, []
    for key, weight, low, high in specs:
        value = number(data.get(key))
        if value is None:
            reasons.append(f"{key}: unavailable (no points)")
            continue
        points += weight * scale(value, low, high) / 100
        covered += weight
        reasons.append(f"{key}: {value:+.1f}")
    inv, rev = number(data.get("inventory_yoy_change")), number(
        data.get("revenue_yoy_change")
    )
    if inv is not None and rev is not None:
        points += 5 * (1 - scale(inv - rev, 0, 30) / 100)
        covered += 5
        reasons.append(f"Inventory growth minus revenue growth: {inv - rev:+.1f}pp YoY")
    return {
        "score": round(min(40, points), 3),
        "coverage": covered / 40,
        "reasons": reasons,
    }
