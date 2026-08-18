/** Shared data types mirroring the FastAPI backend contract (see backend/api.py). */

export interface Experiment {
  experiment_id: string;
  environment: string | null;
  operator_name: string | null;
  notes: string | null;
  purpose?: string | null;
  flow?: string | null;
  initial_oai_config?: string | null;
  created_utc: string | null;
  schema_version: number | null;
}

export interface Condition {
  condition_id: string;
  experiment_id: string;
  environment?: string | null;
  orientation_deg?: number | null;
  incident_power_density_wm2?: number | null;
  stirrer_mode?: string | null;
  stirrer_state?: string | null;
  target_rsrp_dbm?: number | null;
  traffic_condition?: string | null;
  frequency_mhz?: number | null;
  bandwidth_mhz?: number | null;
  tx_gain_db?: number | null;
  rx_gain_db?: number | null;
  pusch_target_snr_db?: number | null;
  pusch_target_snr_x10?: number | null;
  pusch_target_mode?: string | null;
  scheduler_mode?: string | null;
  mcs?: number | null;
  qm?: number | null;
  n_prb?: number | null;
  chamber_metadata_json?: string | null;
  [key: string]: unknown;
}

export interface Run {
  run_id: string;
  experiment_id: string;
  condition_id: string;
  device_id?: string | null;
  session_id?: string | null;
  state: string;
  planned_order?: number | null;
  actual_order?: number | null;
  random_seed?: number | null;
  planned_start_utc_ms?: number | null;
  start_delay_s?: number | null;
  requested_config_json?: string | null;
  actual_config_json?: string | null;
  configuration_id?: number | null;
  configuration_name?: string | null;
  started_utc_ms?: number | null;
  ended_utc_ms?: number | null;
  quality_status?: string | null;
  quality_flags_json?: string | null;
  // injected by GET /api/runs/{run_id}
  condition?: Condition | null;
  requested_config?: Record<string, unknown> | null;
  actual_config?: Record<string, unknown> | null;
  quality_flags?: string[] | null;
  last_error?: string | null;
  [key: string]: unknown;
}

export interface TemplateDef {
  description: string;
  vary: string[];
  fixed: string[];
}

export type Templates = Record<string, TemplateDef>;

export interface LatestRun {
  run_id: string;
  state: string;
  experiment_id: string;
  condition_id: string;
  last_error?: string | null;
}

export interface PlatformStatus {
  phone: {
    state: string;
    device: string | null;
    usb_attached?: boolean;
    agent_url?: string | null;
    serial?: string | null;
    /** Full on-phone /agent/status payload (monitoring, experimentId, phase…). */
    status?: Record<string, unknown> | null;
  };
  oai: {
    healthy: boolean;
    gnb_running: boolean;
    gnb_status: string | null;
    core_running: boolean | null;
    ue_in_sync: boolean;
    frequency_mhz: number | null;
    bandwidth_mhz: number | null;
    telemetry_stale: boolean | null;
    research_stale: boolean | null;
    /** Aggregated gNB goodput sample for the live throughput chart. */
    throughput?: { epochMs: number | null; dlMbps: number; ulMbps: number; nUes: number } | null;
    /** OAI's single shared NetworkTest session. */
    nettest?: {
      sessionId?: string; initiator?: string; state?: string; running?: boolean;
      direction?: string; protocol?: string; requestedMbps?: number; actualMbps?: number;
      averageMbps?: number; errorCode?: string; error?: string;
    } | null;
  };
  clock: { offset_ms: number | null; state: string; delay_ms?: number | null; rtt_min_ms?: number | null };
  experiment: { latest_run: LatestRun | null };
  storage: { n_files: number; bytes: number };
}

export interface Settings {
  oai_base_url: string;
  oai_host: string;
  oai_port: number;
  oai_timeout_s: number;
  oai_control_token_configured: boolean;
  schema_version: number;
  platform_version: string;
}

export interface MergedRow {
  window_ms: number;
  run_id: string;
  condition_id: string;
  experiment_id: string;
  environment: string;
  phase?: string | null;
  [key: string]: unknown;
}

export interface PrepareConfigResult {
  requested: Record<string, unknown>;
  actual: Record<string, unknown>;
  diff: Record<string, { requested: unknown; actual: unknown }>;
}

export interface PrepareResult {
  before: unknown;
  results: Record<string, unknown>;
  actual: Record<string, unknown>;
  verify: { ok: boolean; problems: string[] };
  progress: unknown;
}

export interface ConfigComparisonRow {
  key: string;
  label: string;
  requested: string;
  actual: string;
  mismatch: boolean;
}
