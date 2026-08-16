"""Experiment mode presets (task §55–56)."""
from __future__ import annotations

TEMPLATES = {
    "AC_SIGNAL_SWEEP": {
        "description": "改变 TX gain / 标定入射条件，其余固定",
        "vary": ["txGainDb"],
        "fixed": ["bandwidthMHz", "puschTargetMode", "schedulerMode", "traffic_condition"],
    },
    "AC_TARGET_SNR": {
        "description": "固定 RF/业务，仅改变 PUSCH target SNR（最重要）",
        "vary": ["puschTargetSnrX10"],
        "fixed": ["frequencyMHz", "bandwidthMHz", "txGainDb", "rxGainDb", "schedulerMode", "traffic_condition"],
    },
    "AC_ORIENTATION": {
        "description": "改变 DUT orientation",
        "vary": ["orientationDeg"],
        "fixed": ["frequencyMHz", "bandwidthMHz", "txGainDb", "rxGainDb", "puschTargetSnrX10", "schedulerMode"],
    },
    "RC_MATCHED_RSRP": {
        "description": "不同 stirrer realization，匹配 phone RSRP 区间",
        "vary": ["stirrerState"],
        "fixed": ["targetRsrpDbm", "puschTargetSnrX10", "schedulerMode", "traffic_condition"],
    },
    "RC_TARGET_SNR": {
        "description": "在 RC 重复 PUSCH target intervention",
        "vary": ["puschTargetSnrX10"],
        "fixed": ["environment", "stirrerMode", "schedulerMode", "traffic_condition"],
    },
}


def pusch_target_sweep(values: list[int]) -> list[dict]:
    """Generate a condition list for a PUSCH target sweep. Values are NOT hard-coded."""
    return [{"puschTargetMode": "manual", "puschTargetSnrX10": int(v)} for v in values]
