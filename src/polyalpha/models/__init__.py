"""Probability models package.

Components for the model ensemble:
- MarketPriorModel (Component A): market price as prior
- MicrostructureModel (Component B): order-book dynamics
- FundamentalModel (Component C): external data with Bayesian updating
- LogisticRegressionModel: trained ML model on book features
- GradientBoostedModel: trained ML model on book features
- TrainedEnsembleModel: learned-weight ensemble of all components
"""

from .fundamental import FundamentalModel
from .market_prior import MarketPriorModel
from .microstructure import MicrostructureModel
from .trained import (
    GradientBoostedModel,
    LogisticRegressionModel,
    TrainedEnsembleModel,
)

__all__ = [
    "MarketPriorModel",
    "MicrostructureModel",
    "FundamentalModel",
    "LogisticRegressionModel",
    "GradientBoostedModel",
    "TrainedEnsembleModel",
]
