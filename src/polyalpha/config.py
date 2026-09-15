"""Construct a research engine from explicit configuration and reviewed clusters."""

import os
import tomllib
from decimal import Decimal as D
from pathlib import Path

from .backtest import Engine
from .forecasting import Baseline
from .risk import Limits


def load_env(path: str | Path = ".env") -> None:
    """Load environment variables from a .env file.

    Does not overwrite existing environment variables.
    """
    p = Path(path)
    if not p.exists():
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def load_toml(path: str | Path) -> dict:
    """Load a TOML configuration file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with open(p, "rb") as f:
        return tomllib.load(f)


def load_config(path: str | Path) -> dict:
    """Load config from TOML or YAML based on extension."""
    p = Path(path)
    if p.suffix in (".toml",):
        return load_toml(p)
    elif p.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ImportError("pyyaml required for YAML config")
        with open(p) as f:
            return yaml.safe_load(f)
    else:
        raise ValueError(f"unsupported config format: {p.suffix}")


_REQUIRED_FIELDS = ["collection", "signal", "risk", "execution", "model"]


def validate_config(config: dict) -> list[str]:
    """Validate a configuration dict and return a list of error strings.

    An empty list means the config is valid. Checks for:
    - missing required top-level fields
    - negative min liquidity
    - negative max spread or spread > 1
    - position fraction > 1 or negative
    - drawdown threshold <= 0
    """
    errors: list[str] = []

    for field in _REQUIRED_FIELDS:
        if field not in config:
            errors.append(f"missing required field: {field}")

    risk = config.get("risk", {})
    if "initial_cash" in risk:
        try:
            v = float(risk["initial_cash"])
            if v <= 0:
                errors.append(f"initial_cash must be positive, got {v}")
        except (TypeError, ValueError):
            errors.append(f"initial_cash must be numeric, got {risk['initial_cash']!r}")
    if "max_drawdown_fraction" in risk:
        try:
            v = float(risk["max_drawdown_fraction"])
            if v <= 0:
                errors.append(f"max_drawdown_fraction must be > 0, got {v}")
        except (TypeError, ValueError):
            errors.append(f"max_drawdown_fraction must be numeric, got {risk['max_drawdown_fraction']!r}")
    if "max_position_fraction" in risk:
        try:
            v = float(risk["max_position_fraction"])
            if v < 0:
                errors.append(f"max_position_fraction must be >= 0, got {v}")
            if v > 1:
                errors.append(f"max_position_fraction must be <= 1, got {v}")
        except (TypeError, ValueError):
            errors.append(f"max_position_fraction must be numeric, got {risk['max_position_fraction']!r}")

    market_filter = config.get("market_filter", {})
    if "min_liquidity" in market_filter:
        try:
            v = float(market_filter["min_liquidity"])
            if v < 0:
                errors.append(f"min_liquidity must be >= 0, got {v}")
        except (TypeError, ValueError):
            errors.append(f"min_liquidity must be numeric, got {market_filter['min_liquidity']!r}")
    if "max_spread" in market_filter:
        try:
            v = float(market_filter["max_spread"])
            if v < 0:
                errors.append(f"max_spread must be >= 0, got {v}")
            if v > 1:
                errors.append(f"max_spread must be <= 1, got {v}")
        except (TypeError, ValueError):
            errors.append(f"max_spread must be numeric, got {market_filter['max_spread']!r}")

    signal = config.get("signal", {})
    if "min_net_edge" in signal:
        try:
            v = float(signal["min_net_edge"])
            if v < 0:
                errors.append(f"min_net_edge must be >= 0, got {v}")
        except (TypeError, ValueError):
            errors.append(f"min_net_edge must be numeric, got {signal['min_net_edge']!r}")
    if "position_fraction" in signal:
        try:
            v = float(signal["position_fraction"])
            if v < 0:
                errors.append(f"position_fraction must be >= 0, got {v}")
            if v > 1:
                errors.append(f"position_fraction must be <= 1, got {v}")
        except (TypeError, ValueError):
            errors.append(f"position_fraction must be numeric, got {signal['position_fraction']!r}")

    collection = config.get("collection", {})
    if "interval_seconds" in collection:
        try:
            v = float(collection["interval_seconds"])
            if v < 0:
                errors.append(f"interval_seconds must be >= 0, got {v}")
        except (TypeError, ValueError):
            errors.append(f"interval_seconds must be numeric, got {collection['interval_seconds']!r}")
    if "database" in collection and not collection["database"]:
        errors.append("collection.database must not be empty")

    model = config.get("model", {})
    if "uncertainty" in model:
        try:
            v = float(model["uncertainty"])
            if v < 0 or v > 1:
                errors.append(f"model.uncertainty must be in [0,1], got {v}")
        except (TypeError, ValueError):
            errors.append(f"model.uncertainty must be numeric, got {model['uncertainty']!r}")

    return errors


def make_engine(config, clusters, quality=None):
    risk = dict(config.get("risk", {}))
    initial = D(str(risk.pop("initial_cash", "10000")))
    limits = Limits(**{k: D(str(v)) for k, v in risk.items()})
    execution = config.get("execution", {})
    signal = config.get("signal", {})
    model = Baseline(D(str(config.get("model", {}).get("uncertainty", ".05"))))
    engine = Engine(
        clusters,
        model=model,
        initial_cash=initial,
        latency_seconds=float(execution.get("latency_seconds", 1)),
        exit_policy=execution.get("exit_policy", "hold"),
        require_resolution_review=True,
        min_edge=D(str(signal.get("min_net_edge", ".025"))),
        quality=quality,
        stale_metadata_age=int(execution.get("stale_metadata_age", 300)),
        resolution_penalty=D(str(signal.get("resolution_penalty", ".01"))),
        risk_limits=limits,
    )
    return engine
