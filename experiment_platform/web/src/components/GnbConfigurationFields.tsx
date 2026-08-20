import { Field } from './ui';

export type GnbConfigurationValue = {
  frequencyMHz: string | number; bandwidthMHz: string | number;
  txGainDb: string | number; rxGainDb: string | number;
  puschTargetSnrDb: string | number; schedulerMode: string;
  qm: string | number; mcs: string | number; nPrb: string | number;
  ulTrafficMbps?: string | number;
};

export type GnbConfigurationKey = keyof GnbConfigurationValue;

export const DEFAULT_GNB_CONFIGURATION = {
  frequencyMHz: 3349.92, bandwidthMHz: 100, txGainDb: 60, rxGainDb: 40,
  puschTargetSnrDb: 8.9, schedulerMode: 'auto', qm: 8, mcs: 27, nPrb: 50,
} as const;

export const TABLE2_MCS_RANGE: Record<number, [number, number]> = {
  2: [0, 4], 4: [5, 10], 6: [11, 19], 8: [20, 27],
};

export function GnbConfigurationFields({ value, onChange, disabled = false, errors = {}, supportedBandwidthMHz, includeTraffic = false }: {
  value: GnbConfigurationValue;
  onChange: (key: GnbConfigurationKey, value: string) => void;
  disabled?: boolean;
  errors?: Record<string, string>;
  supportedBandwidthMHz?: number[];
  includeTraffic?: boolean;
}) {
  const field = (label: string, key: GnbConfigurationKey, hint?: string) => <Field label={label} hint={hint}><input className="mono" type="number" disabled={disabled} value={value[key] ?? ''} onChange={(event) => onChange(key, event.target.value)} />{errors[key] && <span className="field-error">{errors[key]}</span>}</Field>;
  const bandwidths = [...new Set([...(supportedBandwidthMHz || [20, 40, 100]), Number(value.bandwidthMHz)])].filter(Number.isFinite).sort((a, b) => a - b);
  const qm = TABLE2_MCS_RANGE[Number(value.qm)] ? Number(value.qm) : DEFAULT_GNB_CONFIGURATION.qm;
  const [mcsMin, mcsMax] = TABLE2_MCS_RANGE[qm];
  const mcs = Math.min(mcsMax, Math.max(mcsMin, Number(value.mcs) || mcsMin));

  return <div className="stack gnb-configuration-fields">
    <section><h3>RF</h3><div className="grid cols-4">{field('Frequency', 'frequencyMHz', 'MHz')}<Field label="Bandwidth" hint="MHz"><select disabled={disabled} value={value.bandwidthMHz} onChange={(event) => onChange('bandwidthMHz', event.target.value)}>{bandwidths.map((item) => <option key={item} value={item}>{item} MHz</option>)}</select>{errors.bandwidthMHz && <span className="field-error">{errors.bandwidthMHz}</span>}</Field>{field('TX Gain', 'txGainDb', 'dB')}{field('RX Gain', 'rxGainDb', 'dB')}</div></section>
    <section><h3>PUSCH Target · Manual</h3><div className="grid cols-2"><Field label="Target SNR" hint="Calculation 0.01 dB · OAI applies 0.1 dB"><div className="row"><input className="grow" type="range" min={0} max={40} step={0.01} disabled={disabled} value={value.puschTargetSnrDb} onChange={(event) => onChange('puschTargetSnrDb', event.target.value)} /><b className="mono">{Number(value.puschTargetSnrDb).toFixed(2)} dB</b></div>{errors.puschTargetSnrDb && <span className="field-error">{errors.puschTargetSnrDb}</span>}</Field></div></section>
    <section><h3>UL Scheduler</h3><div className="grid cols-4"><Field label="Mode"><select disabled={disabled} value={value.schedulerMode} onChange={(event) => onChange('schedulerMode', event.target.value)}><option value="auto">Auto</option><option value="manual">Manual</option></select></Field>{value.schedulerMode === 'manual' && <><Field label="Qm" hint="MCS Table 2 only"><select disabled={disabled} value={qm} onChange={(event) => { const nextQm = Number(event.target.value); const [low, high] = TABLE2_MCS_RANGE[nextQm]; onChange('qm', event.target.value); onChange('mcs', String(Math.min(high, Math.max(low, mcs)))); }}>{[2, 4, 6, 8].map((item) => <option key={item} value={item}>Qm {item}</option>)}</select>{errors.qm && <span className="field-error">{errors.qm}</span>}</Field><Field label="MCS" hint={`Table 2 · ${mcsMin}–${mcsMax}`}><div className="row"><input className="grow" type="range" min={mcsMin} max={mcsMax} step={1} disabled={disabled} value={mcs} onChange={(event) => onChange('mcs', event.target.value)} /><b className="mono">{mcs}</b></div>{errors.mcs && <span className="field-error">{errors.mcs}</span>}</Field>{field('N_PRB', 'nPrb')}</>}</div></section>
    {includeTraffic && <section><h3>Traffic</h3><div className="grid cols-3">{field('UL Traffic', 'ulTrafficMbps', 'Mbps · values ≥100 request saturation')}</div></section>}
  </div>;
}
