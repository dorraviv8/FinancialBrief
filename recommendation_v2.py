"""Evidence-based, horizon-specific Israeli sector recommendation engine.

The engine deliberately avoids a large black-box model. It compares the current
market state with prior, fully matured TASE price states, keeps evaluation
chronological, shrinks uncertain estimates toward neutral, and separates the
recommendation score from confidence.
"""

from __future__ import annotations

import math
import os
import statistics
import copy
from collections import defaultdict
from datetime import datetime, timezone


MODEL_VERSION = "v2.0-knn-walk-forward"
FEATURE_VERSION = "v2.0-price-regime-risk"
HORIZONS = (10, 20, 30)
PRIMARY_HORIZON = 20
MIN_TRAINING_CASES = 60
BACKTEST_STRIDE = 5

FEATURE_NAMES = (
    "relative_return_20d_pct",
    "relative_return_63d_pct",
    "sector_return_20d_pct",
    "sector_return_63d_pct",
    "sector_volatility_20d_pct",
    "sector_drawdown_60d_pct",
    "market_return_20d_pct",
    "market_volatility_20d_pct",
    "distance_sma_20_pct",
    "distance_sma_50_pct",
)

HORIZON_FEATURE_WEIGHTS = {
    10: {
        "relative_return_20d_pct": 1.6,
        "relative_return_63d_pct": 0.7,
        "sector_return_20d_pct": 1.2,
        "sector_return_63d_pct": 0.5,
        "sector_volatility_20d_pct": 0.8,
        "sector_drawdown_60d_pct": 0.8,
        "market_return_20d_pct": 0.9,
        "market_volatility_20d_pct": 0.7,
        "distance_sma_20_pct": 1.1,
        "distance_sma_50_pct": 0.6,
    },
    20: {
        "relative_return_20d_pct": 1.3,
        "relative_return_63d_pct": 1.1,
        "sector_return_20d_pct": 1.0,
        "sector_return_63d_pct": 0.8,
        "sector_volatility_20d_pct": 0.8,
        "sector_drawdown_60d_pct": 0.9,
        "market_return_20d_pct": 0.8,
        "market_volatility_20d_pct": 0.8,
        "distance_sma_20_pct": 0.8,
        "distance_sma_50_pct": 0.9,
    },
    30: {
        "relative_return_20d_pct": 0.8,
        "relative_return_63d_pct": 1.5,
        "sector_return_20d_pct": 0.7,
        "sector_return_63d_pct": 1.1,
        "sector_volatility_20d_pct": 0.7,
        "sector_drawdown_60d_pct": 1.0,
        "market_return_20d_pct": 0.6,
        "market_volatility_20d_pct": 0.8,
        "distance_sma_20_pct": 0.5,
        "distance_sma_50_pct": 1.2,
    },
}


def _safe_float(value, default=None):
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _pct_change(current: float, previous: float) -> float:
    return ((current / previous) - 1.0) * 100 if previous else 0.0


def _period_return(values: list[float], position: int, sessions: int) -> float | None:
    if position < sessions:
        return None
    return _pct_change(values[position], values[position - sessions])


def _volatility(values: list[float], position: int, sessions: int = 20) -> float | None:
    start = max(1, position - sessions + 1)
    returns = [
        _pct_change(values[index], values[index - 1])
        for index in range(start, position + 1)
    ]
    if len(returns) < 10:
        return None
    return statistics.stdev(returns) * math.sqrt(252)


def _drawdown(values: list[float], position: int, sessions: int = 60) -> float:
    window = values[max(0, position - sessions + 1): position + 1]
    peak = max(window)
    return _pct_change(values[position], peak)


def _distance_from_average(values: list[float], position: int, sessions: int) -> float | None:
    if position + 1 < sessions:
        return None
    average = sum(values[position - sessions + 1: position + 1]) / sessions
    return _pct_change(values[position], average)


def _history_map(instrument: dict) -> dict[str, float]:
    result = {}
    for point in instrument.get("history_closes", []):
        value = _safe_float(point.get("close"))
        if point.get("date") and value and value > 0:
            result[str(point["date"])] = value
    return result


def _price_features(
    sector_values: list[float],
    benchmark_values: list[float],
    position: int,
) -> dict:
    sector_20 = _period_return(sector_values, position, 20)
    sector_63 = _period_return(sector_values, position, 63)
    market_20 = _period_return(benchmark_values, position, 20)
    market_63 = _period_return(benchmark_values, position, 63)
    return {
        "relative_return_20d_pct": (
            sector_20 - market_20
            if sector_20 is not None and market_20 is not None
            else None
        ),
        "relative_return_63d_pct": (
            sector_63 - market_63
            if sector_63 is not None and market_63 is not None
            else None
        ),
        "sector_return_20d_pct": sector_20,
        "sector_return_63d_pct": sector_63,
        "sector_volatility_20d_pct": _volatility(sector_values, position),
        "sector_drawdown_60d_pct": _drawdown(sector_values, position),
        "market_return_20d_pct": market_20,
        "market_return_63d_pct": market_63,
        "market_volatility_20d_pct": _volatility(benchmark_values, position),
        "distance_sma_20_pct": _distance_from_average(sector_values, position, 20),
        "distance_sma_50_pct": _distance_from_average(sector_values, position, 50),
    }


def _current_confirmation_features(sector: dict) -> dict:
    breadth = sector.get("breadth", {})
    advancers = _safe_float(breadth.get("advancers"), 0.0)
    decliners = _safe_float(breadth.get("decliners"), 0.0)
    breadth_ratio = (
        advancers / (advancers + decliners)
        if advancers + decliners
        else 0.5
    )
    stocks = sector.get("top_stocks_by_market_cap", [])
    confirmations = []
    liquidities = []
    for stock in stocks:
        stock_return = _safe_float(
            stock.get("technical_analysis", {}).get("return_20d_pct")
        )
        if stock_return is not None:
            confirmations.append(stock_return > 0)
        liquidity = _safe_float(stock.get("avg_daily_turnover_6m_ils"))
        if liquidity is not None and liquidity > 0:
            liquidities.append(liquidity)
    return {
        "breadth_ratio": breadth_ratio,
        "top_stock_confirmation_ratio": (
            sum(confirmations) / len(confirmations) if confirmations else 0.5
        ),
        "top_5_weight_pct": _safe_float(sector.get("top_5_weight_pct")),
        "median_top_stock_liquidity_ils": (
            statistics.median(liquidities) if liquidities else None
        ),
    }


def build_historical_samples(data: dict) -> list[dict]:
    """Build matured, weekly-spaced historical cases without future leakage."""
    benchmark_map = _history_map(data.get("indices", {}).get("ת״א-125", {}))
    samples = []
    for sector_name, sector in data.get("sectors", {}).items():
        sector_map = _history_map(sector)
        dates = sorted(set(benchmark_map) & set(sector_map))
        if len(dates) < 95:
            continue
        sector_values = [sector_map[date] for date in dates]
        benchmark_values = [benchmark_map[date] for date in dates]
        for position in range(63, len(dates) - min(HORIZONS), BACKTEST_STRIDE):
            features = _price_features(sector_values, benchmark_values, position)
            if sum(features.get(name) is not None for name in FEATURE_NAMES) < 8:
                continue
            outcomes = {}
            for horizon in HORIZONS:
                target_position = position + horizon
                if target_position >= len(dates):
                    continue
                sector_return = _pct_change(
                    sector_values[target_position], sector_values[position]
                )
                benchmark_return = _pct_change(
                    benchmark_values[target_position], benchmark_values[position]
                )
                path = sector_values[position: target_position + 1]
                max_drawdown = min(
                    _pct_change(value, max(path[:index + 1]))
                    for index, value in enumerate(path)
                )
                outcomes[horizon] = {
                    "outcome_date": dates[target_position],
                    "sector_return_pct": sector_return,
                    "benchmark_return_pct": benchmark_return,
                    "excess_return_pct": sector_return - benchmark_return,
                    "positive": int(sector_return > 0),
                    "outperformed": int(sector_return > benchmark_return),
                    "max_drawdown_pct": max_drawdown,
                }
            if outcomes:
                samples.append({
                    "sample_date": dates[position],
                    "sector_name": sector_name,
                    "features": features,
                    "outcomes": outcomes,
                })
    return samples


def build_current_features(data: dict) -> dict[str, dict]:
    benchmark_map = _history_map(data.get("indices", {}).get("ת״א-125", {}))
    macro_features = {
        "boi_interest_pct": _safe_float(
            data.get("boi_interest", {}).get("current_interest_pct")
        ),
        "usd_ils_change_pct": _safe_float(
            data.get("exchange_rates", {})
            .get("rates", {})
            .get("USD", {})
            .get("change_pct")
        ),
    }
    result = {}
    for sector_name, sector in data.get("sectors", {}).items():
        sector_map = _history_map(sector)
        dates = sorted(set(benchmark_map) & set(sector_map))
        if len(dates) < 64:
            result[sector_name] = {
                **_current_confirmation_features(sector),
                **macro_features,
                "data_date": None,
            }
            continue
        sector_values = [sector_map[date] for date in dates]
        benchmark_values = [benchmark_map[date] for date in dates]
        result[sector_name] = {
            **_price_features(
                sector_values, benchmark_values, len(dates) - 1
            ),
            **_current_confirmation_features(sector),
            **macro_features,
            "data_date": dates[-1],
            "history_sessions": len(dates),
        }
    return result


def _feature_scales(samples: list[dict]) -> dict[str, tuple[float, float]]:
    scales = {}
    for name in FEATURE_NAMES:
        values = [
            _safe_float(sample["features"].get(name))
            for sample in samples
        ]
        values = [value for value in values if value is not None]
        center = statistics.median(values) if values else 0.0
        deviations = [abs(value - center) for value in values]
        scale = statistics.median(deviations) * 1.4826 if deviations else 1.0
        if scale < 0.05:
            scale = statistics.stdev(values) if len(values) > 1 else 1.0
        scales[name] = (center, max(scale, 0.05))
    return scales


def _distance(
    left: dict,
    right: dict,
    scales: dict[str, tuple[float, float]],
    horizon: int,
) -> float:
    weighted = 0.0
    total_weight = 0.0
    for name, feature_weight in HORIZON_FEATURE_WEIGHTS[horizon].items():
        left_value = _safe_float(left.get(name))
        right_value = _safe_float(right.get(name))
        if left_value is None or right_value is None:
            continue
        scale = scales[name][1]
        difference = (left_value - right_value) / scale
        weighted += feature_weight * difference * difference
        total_weight += feature_weight
    return math.sqrt(weighted / total_weight) if total_weight else 999.0


def _weighted_quantile(
    values_and_weights: list[tuple[float, float]], quantile: float
) -> float | None:
    if not values_and_weights:
        return None
    ordered = sorted(values_and_weights)
    target = sum(weight for _value, weight in ordered) * quantile
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= target:
            return value
    return ordered[-1][0]


def _predict_from_cases(
    features: dict,
    samples: list[dict],
    horizon: int,
) -> dict:
    eligible = [sample for sample in samples if horizon in sample["outcomes"]]
    if not eligible:
        return {
            "probability_positive_pct": 50.0,
            "probability_outperform_pct": 50.0,
            "expected_excess_return_pct": 0.0,
            "expected_excess_low_pct": None,
            "expected_excess_high_pct": None,
            "training_cases": 0,
            "neighbor_cases": 0,
        }
    scales = _feature_scales(eligible)
    neighbor_limit = min(80, max(30, int(math.sqrt(len(eligible)) * 4)))
    nearest = sorted(
        (
            (_distance(features, sample["features"], scales, horizon), sample)
            for sample in eligible
        ),
        key=lambda item: item[0],
    )[:neighbor_limit]
    weighted = [
        (1.0 / (0.35 + distance), sample["outcomes"][horizon])
        for distance, sample in nearest
    ]
    total_weight = sum(weight for weight, _outcome in weighted)
    base_positive = sum(
        sample["outcomes"][horizon]["positive"] for sample in eligible
    ) / len(eligible)
    base_outperform = sum(
        sample["outcomes"][horizon]["outperformed"] for sample in eligible
    ) / len(eligible)
    prior_weight = 8.0
    probability_positive = (
        sum(weight * outcome["positive"] for weight, outcome in weighted)
        + prior_weight * base_positive
    ) / (total_weight + prior_weight)
    probability_outperform = (
        sum(weight * outcome["outperformed"] for weight, outcome in weighted)
        + prior_weight * base_outperform
    ) / (total_weight + prior_weight)
    excess_values = [
        (outcome["excess_return_pct"], weight)
        for weight, outcome in weighted
    ]
    expected_excess = (
        sum(value * weight for value, weight in excess_values) / total_weight
        if total_weight
        else 0.0
    )
    return {
        "probability_positive_pct": round(probability_positive * 100, 1),
        "probability_outperform_pct": round(probability_outperform * 100, 1),
        "expected_excess_return_pct": round(expected_excess, 2),
        "expected_excess_low_pct": round(
            _weighted_quantile(excess_values, 0.25), 2
        ),
        "expected_excess_high_pct": round(
            _weighted_quantile(excess_values, 0.75), 2
        ),
        "training_cases": len(eligible),
        "neighbor_cases": len(nearest),
    }


def _risk_quality(features: dict) -> int:
    volatility = _safe_float(features.get("sector_volatility_20d_pct"), 30.0)
    drawdown = _safe_float(features.get("sector_drawdown_60d_pct"), -10.0)
    breadth = _safe_float(features.get("breadth_ratio"), 0.5)
    confirmation = _safe_float(
        features.get("top_stock_confirmation_ratio"), 0.5
    )
    volatility_score = 100 - _clamp((volatility - 12) / 38 * 100)
    drawdown_score = _clamp(100 + drawdown * 5)
    breadth_score = _clamp(breadth * 100)
    confirmation_score = _clamp(confirmation * 100)
    return round(
        0.40 * volatility_score
        + 0.25 * drawdown_score
        + 0.20 * breadth_score
        + 0.15 * confirmation_score
    )


def _data_completeness(features: dict) -> float:
    price_available = sum(
        features.get(name) is not None for name in FEATURE_NAMES
    ) / len(FEATURE_NAMES)
    confirmation_available = sum(
        features.get(name) is not None
        for name in (
            "breadth_ratio",
            "top_stock_confirmation_ratio",
            "median_top_stock_liquidity_ils",
        )
    ) / 3
    return 0.8 * price_available + 0.2 * confirmation_available


def _score_prediction(
    prediction: dict,
    risk_quality: int,
    completeness: float,
    evaluation: dict,
    catalyst_adjustment: int,
    history_sessions: int,
) -> dict:
    opportunity = (
        0.45 * prediction["probability_positive_pct"]
        + 0.35 * prediction["probability_outperform_pct"]
        + 0.20 * risk_quality
    )
    sample_confidence = _clamp(
        prediction["training_cases"] / 800, 0.10, 1.0
    )
    brier_skill = max(
        0.0, _safe_float(evaluation.get("outperform_brier_skill_pct"), 0.0) / 100
    )
    evaluation_confidence = _clamp(0.35 + brier_skill, 0.30, 0.70)
    confidence = _clamp(
        0.40 * sample_confidence
        + 0.35 * completeness
        + 0.25 * evaluation_confidence,
        0.20,
        0.90,
    )
    if history_sessions < 500:
        confidence = min(confidence, 0.65)
    score_before_news = 50 + confidence * (opportunity - 50)
    final_score = round(_clamp(score_before_news + catalyst_adjustment))
    return {
        **prediction,
        "opportunity_score": round(opportunity, 1),
        "risk_quality": risk_quality,
        "confidence_pct": round(confidence * 100),
        "score_before_news": round(score_before_news),
        "news_catalyst_adjustment": catalyst_adjustment,
        "final_score": final_score,
    }


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average_rank = (index + end) / 2
        for position in range(index, end + 1):
            ranks[order[position]] = average_rank
        index = end + 1
    return ranks


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    left_variance = sum((x - left_mean) ** 2 for x in left)
    right_variance = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_variance * right_variance)
    return numerator / denominator if denominator else None


def walk_forward_evaluation(samples: list[dict]) -> dict[int, dict]:
    """Evaluate only against later dates, never training on the future."""
    by_date = defaultdict(list)
    for sample in samples:
        by_date[sample["sample_date"]].append(sample)
    dates = sorted(by_date)
    evaluations = {}
    for horizon in HORIZONS:
        predictions = []
        training = []
        for date in dates:
            current_rows = [
                row for row in by_date[date] if horizon in row["outcomes"]
            ]
            if len(training) >= MIN_TRAINING_CASES:
                for row in current_rows:
                    estimate = _predict_from_cases(
                        row["features"], training, horizon
                    )
                    outcome = row["outcomes"][horizon]
                    predictions.append({
                        "date": date,
                        "sector": row["sector_name"],
                        "p_positive": estimate["probability_positive_pct"] / 100,
                        "p_outperform": estimate["probability_outperform_pct"] / 100,
                        "score": (
                            0.55 * estimate["probability_positive_pct"]
                            + 0.45 * estimate["probability_outperform_pct"]
                        ),
                        **outcome,
                    })
            training.extend(current_rows)

        if not predictions:
            evaluations[horizon] = {"sample_count": 0}
            continue
        positive_brier = sum(
            (row["p_positive"] - row["positive"]) ** 2 for row in predictions
        ) / len(predictions)
        outperform_brier = sum(
            (row["p_outperform"] - row["outperformed"]) ** 2
            for row in predictions
        ) / len(predictions)
        base_rate = sum(row["outperformed"] for row in predictions) / len(predictions)
        baseline_brier = sum(
            (base_rate - row["outperformed"]) ** 2 for row in predictions
        ) / len(predictions)
        grouped_predictions = defaultdict(list)
        for row in predictions:
            grouped_predictions[row["date"]].append(row)
        rank_correlations = []
        top_excess = []
        top_hits = []
        for rows in grouped_predictions.values():
            correlation = _correlation(
                _rank([row["score"] for row in rows]),
                _rank([row["excess_return_pct"] for row in rows]),
            )
            if correlation is not None:
                rank_correlations.append(correlation)
            leaders = sorted(rows, key=lambda row: row["score"], reverse=True)[:3]
            top_excess.extend(row["excess_return_pct"] for row in leaders)
            top_hits.extend(row["outperformed"] for row in leaders)
        evaluations[horizon] = {
            "sample_count": len(predictions),
            "positive_brier": round(positive_brier, 4),
            "outperform_brier": round(outperform_brier, 4),
            "outperform_brier_skill_pct": round(
                (1 - outperform_brier / baseline_brier) * 100
                if baseline_brier
                else 0.0,
                1,
            ),
            "directional_accuracy_pct": round(
                sum(
                    (row["p_outperform"] >= 0.5) == bool(row["outperformed"])
                    for row in predictions
                ) / len(predictions) * 100,
                1,
            ),
            "rank_information_coefficient": round(
                sum(rank_correlations) / len(rank_correlations), 3
            ) if rank_correlations else None,
            "top_three_hit_rate_pct": round(
                sum(top_hits) / len(top_hits) * 100, 1
            ) if top_hits else None,
            "top_three_avg_excess_return_pct": round(
                sum(top_excess) / len(top_excess), 2
            ) if top_excess else None,
        }
    return evaluations


def build_recommendation_bundle(
    data: dict,
    catalysts: dict[str, dict] | None = None,
) -> dict:
    catalysts = catalysts or {}
    samples = build_historical_samples(data)
    evaluation = walk_forward_evaluation(samples)
    current_features = build_current_features(data)
    sector_results = {}
    for sector_name, features in current_features.items():
        risk_quality = _risk_quality(features)
        completeness = _data_completeness(features)
        catalyst = catalysts.get(sector_name, {})
        catalyst_adjustment = int(
            _clamp(catalyst.get("adjustment", 0), -5, 5)
        )
        horizons = {}
        for horizon in HORIZONS:
            prediction = _predict_from_cases(features, samples, horizon)
            horizons[horizon] = _score_prediction(
                prediction,
                risk_quality,
                completeness,
                evaluation.get(horizon, {}),
                catalyst_adjustment,
                int(features.get("history_sessions") or 0),
            )
        sector_results[sector_name] = {
            "features": features,
            "risk_quality": risk_quality,
            "data_completeness_pct": round(completeness * 100),
            "catalyst": catalyst,
            "horizons": horizons,
            "primary_score": horizons[PRIMARY_HORIZON]["final_score"],
        }
    return {
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "mode": os.getenv("RECOMMENDATION_V2_MODE", "shadow").lower(),
        "primary_horizon": PRIMARY_HORIZON,
        "historical_samples": samples,
        "evaluation": evaluation,
        "sectors": sector_results,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def apply_catalysts(bundle: dict, catalysts: dict[str, dict]) -> dict:
    """Apply audited catalyst points without rerunning historical evaluation."""
    updated = copy.deepcopy(bundle)
    for sector_name, recommendation in updated.get("sectors", {}).items():
        catalyst = catalysts.get(sector_name, {})
        adjustment = int(_clamp(catalyst.get("adjustment", 0), -5, 5))
        recommendation["catalyst"] = catalyst
        for forecast in recommendation.get("horizons", {}).values():
            forecast["news_catalyst_adjustment"] = adjustment
            forecast["final_score"] = round(_clamp(
                forecast["score_before_news"] + adjustment
            ))
        recommendation["primary_score"] = recommendation["horizons"][
            PRIMARY_HORIZON
        ]["final_score"]
    return updated


def confidence_label(confidence_pct: float) -> str:
    if confidence_pct >= 75:
        return "גבוהה"
    if confidence_pct >= 55:
        return "בינונית"
    return "נמוכה"
