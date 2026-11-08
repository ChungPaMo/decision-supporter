"""LightGBM 학습·예측. 하이퍼파라미터는 26-10-26 실험기록 §8 에서 선택한 값."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np

PARAMS = {
    "num_leaves": 7,
    "min_data_in_leaf": 50,       # = min_child_samples
    "learning_rate": 0.05,
    "verbosity": -1,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 1,             # 같은 입력·seed → 같은 출력
}
NUM_ROUNDS = 200
LIBRARY_VERSION = lgb.__version__


def fit_predict(X: np.ndarray, y: np.ndarray, X_pred: np.ndarray, *, objective: str,
                alpha: float | None = None, seed: int) -> np.ndarray:
    """정규화된 목표로 학습하고 예측한다. 음수는 0으로 자른다.

    기록의 "시드 3회"는 무작위 샘플링이 없는 설정이라 결과가 같았다(재현 시 시드 간 최대 차이 8.9e-16).
    그래서 seed 하나만 쓴다.
    """
    params = dict(PARAMS, objective=objective, seed=seed)
    if alpha is not None:
        params["alpha"] = alpha
    booster = lgb.train(params, lgb.Dataset(X, y, free_raw_data=True), num_boost_round=NUM_ROUNDS)
    return np.maximum(booster.predict(X_pred), 0.0)
