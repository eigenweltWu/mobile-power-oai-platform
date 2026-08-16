"""Typed data models for the OAI control-center API.

Field names here mirror the *actual* API responses captured in
``data/oai_schema/*.json`` (2026-08-15), NOT the spec document wording.
Extra fields are preserved (``extra='allow'``) so the client never drops
unknown fields; callers that need byte-exact preservation should use
``model_validate`` + ``model_dump(exclude_none=False)`` or keep the raw JSON.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class BaseOAI(BaseModel):
    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------------------- #
# /api/gnb/controls
# --------------------------------------------------------------------------- #
class UlScheduler(BaseOAI):
    mode: Optional[str] = None
    coupled: Optional[bool] = None
    mcs: Optional[int] = None
    qm: Optional[int] = None
    nPrb: Optional[int] = None
    mcsTable: Optional[int] = None
    automaticValuesUseNull: Optional[bool] = None
    constraint: Optional[str] = None


class PuschTarget(BaseOAI):
    mode: Optional[str] = None
    targetSnrX10: Optional[int] = None
    targetSnrDb: Optional[float] = None
    autoTargetSnrX10: Optional[int] = None
    autoTargetSnrDb: Optional[float] = None


class ObservedUplink(BaseOAI):
    rnti: Optional[str] = None
    updatedAtUtc: Optional[str] = None
    ageSeconds: Optional[float] = None
    mcsTable: Optional[int] = None
    mcs: Optional[int] = None
    qm: Optional[int] = None
    nPrb: Optional[int] = None
    puschSnrDb: Optional[float] = None


class GnbControls(BaseOAI):
    timestampUtc: Optional[str] = None
    timestampEpochNs: Optional[int] = None
    timestampMonotonicNs: Optional[int] = None
    ulScheduler: Optional[UlScheduler] = None
    puschTarget: Optional[PuschTarget] = None
    observedUplink: Optional[ObservedUplink] = None


# --------------------------------------------------------------------------- #
# /api/research/config
# --------------------------------------------------------------------------- #
class ResearchConfig(BaseOAI):
    puschTargetSnrX10: Optional[int] = None
    pucchTargetSnrX10: Optional[int] = None
    ulschMaxFrameInactivity: Optional[int] = None
    ulBlerTargetUpper: Optional[float] = None
    ulBlerTargetLower: Optional[float] = None
    ulMinMcs: Optional[int] = None
    ulMaxMcs: Optional[int] = None
    dlBlerTargetUpper: Optional[float] = None
    dlBlerTargetLower: Optional[float] = None
    dlMinMcs: Optional[int] = None
    dlMaxMcs: Optional[int] = None
    minGrantPrb: Optional[int] = None
    deltaMcsEnabled: Optional[bool] = None
    ulManualMcs: Optional[int] = None
    ulManualPrb: Optional[int] = None
    puschTargetSnrDb: Optional[float] = None
    timestampUtc: Optional[str] = None
    timestampEpochNs: Optional[int] = None
    timestampMonotonicNs: Optional[int] = None
    configPath: Optional[str] = None
    available: Optional[bool] = None
    error: Optional[str] = None
    frequencyMHz: Optional[float] = None
    bandwidthMHz: Optional[float] = None
    txGainDb: Optional[float] = None
    rxGainDb: Optional[float] = None
    ulSchedulerMode: Optional[str] = None
    puschTargetMode: Optional[str] = None
    controls: Optional[GnbControls] = None


# --------------------------------------------------------------------------- #
# /api/research/ues
# --------------------------------------------------------------------------- #
class UeDownlink(BaseOAI):
    mcs: Optional[int] = None
    mcsTable: Optional[int] = None
    qm: Optional[int] = None
    bler: Optional[float] = None
    harqRounds: Optional[list[int]] = None
    harqErrors: Optional[int] = None
    harqInitialTxDelta: Optional[int] = None
    harqRetransmissionDelta: Optional[int] = None
    harqRetransmissionRatio: Optional[float] = None
    dtx: Optional[int] = None
    dtxDelta: Optional[int] = None
    goodputMbps: Optional[float] = None
    cqi: Optional[int] = None
    ri: Optional[int] = None
    pmi: Optional[int] = None


class UeUplink(BaseOAI):
    mcs: Optional[int] = None
    mcsTable: Optional[int] = None
    qm: Optional[int] = None
    bler: Optional[float] = None
    harqRounds: Optional[list[int]] = None
    harqErrors: Optional[int] = None
    harqInitialTxDelta: Optional[int] = None
    harqRetransmissionDelta: Optional[int] = None
    harqRetransmissionRatio: Optional[float] = None
    dtx: Optional[int] = None
    dtxDelta: Optional[int] = None
    goodputMbps: Optional[float] = None
    ulRi: Optional[int] = None
    tpmi: Optional[int] = None
    nPrb: Optional[int] = None
    puschSnrDb: Optional[float] = None
    puschRssi: Optional[float] = None
    puschRssiUnit: Optional[str] = None


class UePowerControl(BaseOAI):
    phCeCode: Optional[int] = None
    phRawDb: Optional[float] = None
    phNormalizedDb: Optional[float] = None
    pcmaxCeCode: Optional[int] = None
    pcmaxDbm: Optional[float] = None
    pcmaxMinusRawPhDb: Optional[float] = None
    puschTargetSnrX10: Optional[int] = None
    puschTargetSnrDb: Optional[float] = None
    tpcPusch: Optional[int] = None
    tpcInFlightDb: Optional[float] = None
    deltaMcsDb: Optional[float] = None
    updatedAtUtc: Optional[str] = None
    ageSeconds: Optional[float] = None
    phRawUpdatedAtUtc: Optional[str] = None
    phRawAgeSeconds: Optional[float] = None


class ResearchUe(BaseOAI):
    rnti: Optional[str] = None
    cuId: Optional[int] = None
    state: Optional[str] = None
    timestampUtc: Optional[str] = None
    timestampEpochNs: Optional[int] = None
    updatedAtUtc: Optional[str] = None
    ageSeconds: Optional[float] = None
    rsrpDbm: Optional[float] = None
    ssbSinrDb: Optional[float] = None
    downlink: Optional[UeDownlink] = None
    uplink: Optional[UeUplink] = None
    powerControl: Optional[UePowerControl] = None
    imsi: Optional[str] = None
    timestampMonotonicNs: Optional[int] = None


class Collection(BaseOAI):
    available: Optional[bool] = None
    error: Optional[str] = None
    samplingHz: Optional[float] = None
    rssiUnit: Optional[str] = None
    latestAgeSeconds: Optional[float] = None
    stale: Optional[bool] = None


class ResearchUes(BaseOAI):
    timestampUtc: Optional[str] = None
    timestampEpochNs: Optional[int] = None
    timestampMonotonicNs: Optional[int] = None
    experimentId: Optional[str] = None
    source: Optional[str] = None
    collection: Optional[Collection] = None
    ues: list[ResearchUe] = []


# --------------------------------------------------------------------------- #
# /api/research/events
# --------------------------------------------------------------------------- #
class PuschEvent(BaseOAI):
    timestampUtc: Optional[str] = None
    timestampEpochNs: Optional[int] = None
    timestampMonotonicNs: Optional[int] = None
    rnti: Optional[str] = None
    frame: Optional[int] = None
    slot: Optional[int] = None
    puschSnrDb: Optional[float] = None
    phNormalizedDb: Optional[float] = None
    tpcPusch: Optional[int] = None
    tbSizeBytes: Optional[int] = None
    tpcInFlightDb: Optional[float] = None
    deltaMcsDb: Optional[float] = None
    nPrb: Optional[int] = None
    mcs: Optional[int] = None
    rssi: Optional[float] = None
    rssiUnit: Optional[str] = None


class ResearchEvents(BaseOAI):
    timestampUtc: Optional[str] = None
    timestampEpochNs: Optional[int] = None
    timestampMonotonicNs: Optional[int] = None
    limit: Optional[int] = None
    count: Optional[int] = None
    events: list[PuschEvent] = []


# --------------------------------------------------------------------------- #
# /api/gnb/progress
# --------------------------------------------------------------------------- #
class Progress(BaseOAI):
    requestId: Optional[str] = None
    active: Optional[bool] = None
    action: Optional[str] = None
    phase: Optional[str] = None
    message: Optional[str] = None
    progress: Optional[int] = None
    error: Optional[str] = None
    updatedAt: Optional[str] = None

    @property
    def done(self) -> bool:
        return (self.active is False) or (self.phase in {"complete", "done", "ready", "finished"})

    @property
    def failed(self) -> bool:
        return bool(self.error)


# --------------------------------------------------------------------------- #
# /api/status (subset the platform consumes)
# --------------------------------------------------------------------------- #
class GnbStatus(BaseOAI):
    name: Optional[str] = None
    running: Optional[bool] = None
    status: Optional[str] = None
    startedAt: Optional[str] = None
    exitCode: Optional[int] = None


class Radio(BaseOAI):
    band: Optional[int] = None
    arfcn: Optional[int] = None
    pointA: Optional[int] = None
    frequencyMHz: Optional[float] = None
    bandwidthMHz: Optional[float] = None
    carrierPrb: Optional[int] = None
    subcarrierSpacingKhz: Optional[int] = None
    supportedBandwidthMHz: list[int] = []
    txGainDb: Optional[float] = None
    rxGainDb: Optional[float] = None


class CoreStatus(BaseOAI):
    running: Optional[int] = None
    total: Optional[int] = None
    services: list[Any] = []


class StatusUe(BaseOAI):
    rnti: Optional[str] = None
    cuId: Optional[int] = None
    state: Optional[str] = None
    rsrp: Optional[float] = None
    snr: Optional[float] = None
    dlMbps: Optional[float] = None
    ulMbps: Optional[float] = None
    dlMcs: Optional[int] = None
    ulMcs: Optional[int] = None
    dlBler: Optional[float] = None
    ulBler: Optional[float] = None
    imsi: Optional[str] = None


class Status(BaseOAI):
    timestamp: Optional[str] = None
    gnb: Optional[GnbStatus] = None
    core: Optional[CoreStatus] = None
    radio: Optional[Radio] = None
    controls: Optional[GnbControls] = None
    ues: list[StatusUe] = []
    raw: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# /api/rf/calibration
# --------------------------------------------------------------------------- #
class CalibrationAxes(BaseOAI):
    x: Optional[dict] = None
    y: Optional[dict] = None


class CalibrationDevice(BaseOAI):
    frequencyMHz: Optional[float] = None
    points: list[Any] = []
    allPoints: list[Any] = []
    pointCount: Optional[int] = None
    totalPointCount: Optional[int] = None


class RfCalibration(BaseOAI):
    timestamp: Optional[str] = None
    frequencyMHz: Optional[float] = None
    axes: Optional[CalibrationAxes] = None
    devices: dict[str, CalibrationDevice] = {}
