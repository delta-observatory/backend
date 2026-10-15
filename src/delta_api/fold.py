"""Phase fold and binned means for the fold view (rule fold-window/1).

This is a display calculation on the stored prepared series. It is not a stored result.
The frontend makes the same calculation during a drag.
tests/vectors/fold.json holds the cases that both implementations must pass.
"""
import math


def offset_hours(t: float, t0: float, period: float) -> float:
    """Time from the nearest event at t0 + n × period, in hours, in the range [-period/2, period/2)."""
    phase = math.fmod((t - t0) / period, 1.0)
    if phase >= 0.5:
        phase -= 1
    if phase < -0.5:
        phase += 1
    return phase * period * 24


def fold(time: list, flux: list, t0: float, period: float, window_hours: float,
         time_range: tuple[float, float] | None) -> tuple[list[float], list[float]]:
    """Points within ±window_hours of an event. Non-finite values are skipped."""
    xs, ys = [], []
    for t, f in zip(time, flux):
        if t is None or f is None or not (math.isfinite(t) and math.isfinite(f)):
            continue
        if time_range and (t < time_range[0] or t > time_range[1]):
            continue
        h = offset_hours(t, t0, period)
        if abs(h) <= window_hours:
            xs.append(h)
            ys.append(f)
    return xs, ys


def bins(xs: list[float], ys: list[float], window_hours: float, bin_hours: float, min_count: int = 4) -> list[dict]:
    """Mean and standard error of the mean in bins of width bin_hours. A bin needs min_count points."""
    n_bins = math.ceil(2 * window_hours / bin_hours)
    total, squares, count = [0.0] * n_bins, [0.0] * n_bins, [0] * n_bins
    for x, y in zip(xs, ys):
        k = min(n_bins - 1, max(0, math.floor((x + window_hours) / bin_hours)))
        total[k] += y
        squares[k] += y * y
        count[k] += 1
    out = []
    for k in range(n_bins):
        c = count[k]
        if c < min_count:
            continue
        mean = total[k] / c
        variance = max(0.0, (squares[k] / c - mean * mean) * (c / (c - 1)))
        out.append({'x_hours': -window_hours + (k + 0.5) * bin_hours, 'mean': mean,
                    'standard_error': math.sqrt(variance / c), 'n': c})
    return out
