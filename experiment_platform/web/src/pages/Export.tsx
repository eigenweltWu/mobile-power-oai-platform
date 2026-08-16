import { useState } from 'react';
import { api, downloadFile } from '../api';
import type { Experiment } from '../types';
import { Badge, Card, ErrorBox, Field, Spinner, toast } from '../components/ui';
import { useLoad } from '../components/DataView';
import { fmtIso } from '../format';

export default function Export() {
  const load = useLoad<Experiment[]>(() => api.get('/api/experiments'), []);
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);

  const doExport = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await downloadFile(`/api/experiments/${encodeURIComponent(selected)}/export`, `${selected}.zip`);
      toast('ok', `download started for ${selected}`);
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">Export</div>
          <div className="subtitle">bundle an experiment into a portable ZIP</div>
        </div>
      </div>

      <Card title="Export Experiment ZIP">
        {load.loading ? (
          <Spinner />
        ) : load.error ? (
          <ErrorBox error={load.error} />
        ) : (
          <>
            <div className="grid cols-2">
              <Field label="Experiment">
                <select value={selected} onChange={(e) => setSelected(e.target.value)}>
                  <option value="">— select —</option>
                  {load.data?.map((x) => (
                    <option key={x.experiment_id} value={x.experiment_id}>
                      {x.experiment_id} ({x.environment})
                    </option>
                  ))}
                </select>
              </Field>
              <div style={{ alignSelf: 'end' }}>
                <button className="btn primary" disabled={!selected || busy} onClick={doExport}>
                  {busy ? 'exporting…' : 'Download ZIP'}
                </button>
              </div>
            </div>

            {selected && load.data && (
              <div className="row gap-sm" style={{ marginTop: 14 }}>
                <Badge tone="accent">{selected}</Badge>
                <Badge tone="muted">created {fmtIso(load.data.find((x) => x.experiment_id === selected)?.created_utc)}</Badge>
              </div>
            )}
          </>
        )}
      </Card>

      <Card title="ZIP contents">
        <div className="notice-box">
          The export includes <span className="mono">manifest.json</span>, <span className="mono">runs.csv</span>,{' '}
          <span className="mono">conditions.csv</span>, <span className="mono">sync.csv</span>, raw phone + OAI files,{' '}
          <span className="mono">processed/merged_1s.csv</span>, <span className="mono">processed/run_summary.csv</span>, feature
          sets and calibration metadata (see DATA_SCHEMA.md §7).
        </div>
      </Card>
    </div>
  );
}
