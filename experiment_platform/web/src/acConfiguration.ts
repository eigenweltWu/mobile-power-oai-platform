export type AcPhase = {
  name: 'idle' | 'loaded'; durationSeconds: number; configurationId?: number; standard?: boolean;
};

export type AcTemplateRow = {
  configurationId: number; warmupSeconds: number; testSeconds: number;
};

export const DEFAULT_WARMUP_SECONDS = 15;
export const DEFAULT_TEST_SECONDS = 120;

const phases = (json: string | null | undefined): AcPhase[] => {
  try { const value = JSON.parse(json || '[]'); return Array.isArray(value) ? value : []; } catch { return []; }
};

export function readAcExecution(json: string | null | undefined) {
  const source = phases(json);
  const standard = source.filter((phase) => phase.standard);
  const warmupSeconds = Number(standard.find((phase) => phase.name === 'idle')?.durationSeconds) || DEFAULT_WARMUP_SECONDS;
  const testSeconds = Number(standard.find((phase) => phase.name === 'loaded')?.durationSeconds) || DEFAULT_TEST_SECONDS;
  const rows: AcTemplateRow[] = [];
  let warmup = DEFAULT_WARMUP_SECONDS;
  for (const phase of source.filter((item) => !item.standard)) {
    if (phase.name === 'idle') warmup = Number(phase.durationSeconds) || DEFAULT_WARMUP_SECONDS;
    if (phase.name === 'loaded' && phase.configurationId) {
      rows.push({ configurationId: Number(phase.configurationId), warmupSeconds: warmup, testSeconds: Number(phase.durationSeconds) || DEFAULT_TEST_SECONDS });
      warmup = DEFAULT_WARMUP_SECONDS;
    }
  }
  return { warmupSeconds, testSeconds, rows };
}

export const templatePhases = (rows: AcTemplateRow[]): AcPhase[] => rows.flatMap((row) => [
  { name: 'idle', durationSeconds: row.warmupSeconds },
  { name: 'loaded', durationSeconds: row.testSeconds, configurationId: row.configurationId },
]);

export const standardPhases = (warmupSeconds: number, testSeconds: number): AcPhase[] => [
  { name: 'idle', durationSeconds: warmupSeconds, standard: true },
  { name: 'loaded', durationSeconds: testSeconds, standard: true },
];
