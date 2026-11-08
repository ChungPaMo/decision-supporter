"""LightGBM 학습·예측 행 만들기.

한 행 = (제품, 기준 월 t, horizon h). h 를 피처로 넣은 단일 모델이며, 수요 관련 값은 모두 제품의
기준 월 ma12 로 나눠 정규화한다 (26-10-26 실험기록 §8).

기록의 피처 중 주문건수비·반품비·카테고리는 계약 입력(제품×월 수요)에 없어 쓰지 않는다.
세 피처를 뺀 재현 실험에서 검증 h=2 pinball 0.111/0.080 → 0.113/0.080, 커버리지 0.836/0.910 →
0.841/0.910 으로 차이가 작았다. 추세비는 기록대로 ma3 와 같은 값이라 뺐다.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

WINDOW = 12
FEATURES = [*(f"lag{k}" for k in range(WINDOW)), "same_month_ly", "ma3", "ma6", "std6", "std12",
            "zero_months12", "target_month", "h"]
FEATURE_SET_VERSION = "demand-v1"


def base_windows(y: np.ndarray) -> np.ndarray:
    """기준 월 t(로컬 인덱스 11..n-1)마다 끝이 t 인 12개월 창. shape (n-11, 12)."""
    return sliding_window_view(y, WINDOW)


def build(y: np.ndarray, start_month: int, horizons: list[int], *, for_training: bool):
    """한 제품의 학습 행 또는 예측 행을 만든다.

    y: 첫 관측 월부터 trained_until 월까지 월 수요 (0 채움 끝난 값).
    start_month: y[0] 의 절대 월 번호 (year*12 + month-1).
    for_training=True: 목표 월 실적이 있는 모든 (t, h) 행과 정규화된 정답을 돌려준다.
    for_training=False: 마지막 기준 월(t = n-1) 의 h 별 행만 돌려준다.
    반환: (X, target, scale). scale 이 0 인 행(최근 12개월 수요 0)은 제외한다.
    """
    n = len(y)
    if n < WINDOW:
        return np.empty((0, len(FEATURES))), np.empty(0), np.empty(0)
    win = base_windows(y)                       # win[i] 는 기준 월 t = i + 11 에서 끝나는 창
    bases = np.arange(WINDOW - 1, n)
    if not for_training:
        win, bases = win[-1:], bases[-1:]
    scale = win.mean(axis=1)
    safe = np.where(scale > 0, scale, 1.0)
    lags = win[:, ::-1] / safe[:, None]          # lag0 = 기준 월
    ma3 = win[:, -3:].mean(axis=1) / safe
    ma6 = win[:, -6:].mean(axis=1) / safe
    std6 = win[:, -6:].std(axis=1) / safe
    std12 = win.std(axis=1) / safe
    zeros = (win == 0).sum(axis=1)

    xs, ts, ss = [], [], []
    for h in horizons:
        tm = bases + h
        keep = scale > 0
        if for_training:
            keep &= tm < n
        if not keep.any():
            continue
        # 작년 같은 달: 기준 월 이후 값이 들어가지 않도록 h>12 이면 2년 전으로 물러난다
        back = 12 * math.ceil(h / 12)
        same_ly = y[tm[keep] - back] / safe[keep]
        target_month = (start_month + tm[keep]) % 12 + 1
        x = np.column_stack([lags[keep], same_ly, ma3[keep], ma6[keep], std6[keep], std12[keep],
                             zeros[keep], target_month, np.full(keep.sum(), h)])
        xs.append(x)
        ss.append(scale[keep])
        ts.append(y[tm[keep]] / scale[keep] if for_training else np.full(keep.sum(), np.nan))
    if not xs:
        return np.empty((0, len(FEATURES))), np.empty(0), np.empty(0)
    return np.vstack(xs), np.concatenate(ts), np.concatenate(ss)
