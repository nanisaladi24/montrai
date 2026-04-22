import numpy as np
import pickle
from pathlib import Path
from typing import Tuple, Optional
from hmmlearn import hmm
from config.settings import (
    HMM_N_REGIMES_RANGE, HMM_LOOKBACK_DAYS, HMM_STABILITY_WINDOW, REGIME_NAMES
)
from core.market_data import fetch_historical
from core.feature_engineering import build_hmm_features
from monitoring.logger import get_logger

logger = get_logger("hmm_engine")
MODEL_PATH = "regime_hmm.pkl"


class RegimeDetector:
    def __init__(self):
        self.model: Optional[hmm.GaussianHMM] = None
        self.n_regimes: int = 3
        self._regime_history: list[int] = []
        self._state_to_regime: dict = {}

    def _train_model(self, features: np.ndarray, n: int) -> Tuple[hmm.GaussianHMM, float]:
        model = hmm.GaussianHMM(
            n_components=n,
            covariance_type="full",
            n_iter=200,
            random_state=42,
        )
        model.fit(features)
        # BIC = -2 * log-likelihood + k * log(n)
        ll = model.score(features)
        k = n * n + 2 * n * features.shape[1]  # rough param count
        bic = -2 * ll * len(features) + k * np.log(len(features))
        return model, bic

    def _train_model_on_features(self, features: np.ndarray):
        """Directly train on a pre-computed feature array (used by backtester)."""
        best_model, best_bic, best_n = None, np.inf, 3
        for n in range(HMM_N_REGIMES_RANGE[0], min(HMM_N_REGIMES_RANGE[1] + 1, len(features) // 10 + 1)):
            try:
                model, bic = self._train_model(features, n)
                if bic < best_bic:
                    best_bic, best_model, best_n = bic, model, n
            except Exception:
                pass
        if best_model:
            self.model = best_model
            self.n_regimes = best_n
            self._sort_regimes_by_volatility(features)
            self._state_to_regime = getattr(self, "_state_to_regime", {})

    def train(self, benchmark_symbol: str = "SPY") -> bool:
        logger.info(f"Training HMM on {benchmark_symbol}...")
        df = fetch_historical(benchmark_symbol, days=HMM_LOOKBACK_DAYS)
        if df.empty:
            logger.error("No data for HMM training.")
            return False

        features = build_hmm_features(df)
        if len(features) < 50:
            logger.error("Insufficient feature rows for HMM training.")
            return False

        best_model, best_bic, best_n = None, np.inf, 3
        for n in range(HMM_N_REGIMES_RANGE[0], HMM_N_REGIMES_RANGE[1] + 1):
            try:
                model, bic = self._train_model(features, n)
                logger.info(f"  n={n} BIC={bic:.1f}")
                if bic < best_bic:
                    best_bic, best_model, best_n = bic, model, n
            except Exception as e:
                logger.warning(f"  n={n} failed: {e}")

        self.model = best_model
        self.n_regimes = best_n
        self._sort_regimes_by_volatility(features)
        self.save()
        logger.info(f"HMM trained: {best_n} regimes selected (BIC={best_bic:.1f})")
        return True

    def _sort_regimes_by_volatility(self, features: np.ndarray):
        """Re-order hidden states so regime 0 = lowest vol (crash label mapping)."""
        if self.model is None:
            return
        states = self.model.predict(features)
        # vol is index 1 in features (realised_vol)
        mean_vols = {s: features[states == s, 1].mean() for s in range(self.n_regimes) if (states == s).any()}
        # sort ascending; assign new transmat / means accordingly (state relabeling)
        # Simple approach: store ordering for label remapping
        sorted_states = sorted(mean_vols, key=mean_vols.get)
        self._state_to_regime = {s: i for i, s in enumerate(sorted_states)}

    def predict_regime(self, features: np.ndarray) -> int:
        """Predict current regime with stability filter."""
        if self.model is None:
            return 2  # neutral default
        # Empty feature matrix (data fetch failure or cold start) — hold
        # the last stable reading rather than crashing the main loop.
        if features is None or features.size == 0 or features.shape[0] == 0:
            logger.warning("Empty feature matrix passed to predict_regime; returning neutral.")
            if self._regime_history:
                return self._regime_history[-1]
            return 2
        # Guard against feature-count mismatch after a data-source change
        expected = self.model.means_.shape[1]
        if features.shape[1] != expected:
            logger.warning(
                f"Feature count mismatch: model expects {expected}, got {features.shape[1]}. "
                "Retraining HMM."
            )
            self.train("SPY")
            if self.model is None or features.shape[1] != self.model.means_.shape[1]:
                return 2
        raw_state = int(self.model.predict(features)[-1])
        mapped = self._state_to_regime.get(raw_state, raw_state)

        # Cap to 0–4 range (crash / bear / neutral / bull / euphoria)
        mapped = min(mapped, 4)

        self._regime_history.append(mapped)
        self._regime_history = self._regime_history[-HMM_STABILITY_WINDOW:]

        # Only flip if last N bars all agree
        if len(self._regime_history) >= HMM_STABILITY_WINDOW:
            if len(set(self._regime_history)) == 1:
                return self._regime_history[-1]
        # Return the previous stable reading (or latest if no history)
        if len(self._regime_history) > 1:
            return self._regime_history[-2]
        return mapped

    def regime_name(self, regime_id: int) -> str:
        return REGIME_NAMES.get(regime_id, "unknown")

    def save(self):
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls) -> "RegimeDetector":
        if Path(MODEL_PATH).exists():
            try:
                with open(MODEL_PATH, "rb") as f:
                    obj = pickle.load(f)
                logger.info("Loaded saved HMM model.")
                return obj
            except Exception as e:
                logger.warning(f"Could not load HMM model: {e}. Will retrain.")
        return cls()
