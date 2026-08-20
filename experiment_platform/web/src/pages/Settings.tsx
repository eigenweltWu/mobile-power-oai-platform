import { useState } from 'react';
import { api } from '../api';
import type { Settings } from '../types';
import { Badge, Card, ErrorBox, Field, Spinner, toast } from '../components/ui';
import { useLoad } from '../components/DataView';

export default function Settings() {
  const load = useLoad<Settings>(() => api.get('/api/settings'), []);
  const [form, setForm] = useState({ oai_host: '', oai_port: '', token: '' });
  const [clearToken, setClearToken] = useState(false);
  const [busy, setBusy] = useState(false);

  const s = load.data;

  const save = async () => {
    setBusy(true);
    try {
      const payload: Record<string, unknown> = {};
      if (form.oai_host.trim()) payload['oai_host'] = form.oai_host.trim();
      if (form.oai_port.trim()) payload['oai_port'] = Number(form.oai_port.trim());
      if (clearToken) payload['oai_control_token'] = '';
      else if (form.token) payload['oai_control_token'] = form.token;
      const updated = await api.put<Settings>('/api/settings', payload);
      load.setData(updated);
      setForm({ oai_host: '', oai_port: '', token: '' });
      setClearToken(false);
      toast('ok', 'settings saved (OAI client reconnected)');
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack page-workspace settings-workspace">
      <div className="page-head">
        <div>
          <div className="title">Settings</div>
          <div className="subtitle">OAI connectivity and control token</div>
        </div>
      </div>

      <Card title="OAI Connectivity Settings">
        {load.loading && !s ? (
          <Spinner />
        ) : load.error ? (
          <ErrorBox error={load.error} />
        ) : s ? (
          <>
            <div className="kv" style={{ marginBottom: 16 }}>
              <dt>oai_base_url</dt><dd>{s.oai_base_url}</dd>
              <dt>oai_host</dt><dd>{s.oai_host}</dd>
              <dt>oai_port</dt><dd>{s.oai_port}</dd>
              <dt>oai_timeout_s</dt><dd>{s.oai_timeout_s}</dd>
              <dt>control token</dt>
              <dd>{s.oai_control_token_configured ? <Badge tone="accent">configured (masked)</Badge> : <Badge tone="muted">not set</Badge>}</dd>
              <dt>schema_version</dt><dd>{s.schema_version}</dd>
              <dt>platform_version</dt><dd>{s.platform_version}</dd>
            </div>

            <div className="grid cols-3">
              <Field label="OAI host" hint="leave blank to keep current">
                <input className="mono" value={form.oai_host} placeholder={s.oai_host} onChange={(e) => setForm({ ...form, oai_host: e.target.value })} />
              </Field>
              <Field label="OAI port" hint="leave blank to keep current">
                <input className="mono" value={form.oai_port} placeholder={String(s.oai_port)} onChange={(e) => setForm({ ...form, oai_port: e.target.value })} />
              </Field>
              <Field
                label="Control token"
                hint={s.oai_control_token_configured ? 'a token is configured — leave blank to keep it' : 'optional'}
              >
                <input
                  className="mono"
                  type="password"
                  value={form.token}
                  placeholder={s.oai_control_token_configured ? '•••••• (unchanged)' : 'not set'}
                  disabled={clearToken}
                  onChange={(e) => setForm({ ...form, token: e.target.value })}
                />
              </Field>
            </div>
            <div className="row" style={{ marginTop: 14 }}>
              <button className="btn primary" onClick={save} disabled={busy}>
                {busy ? 'saving…' : 'Save settings'}
              </button>
              {s.oai_control_token_configured && (
                <label className="field" style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                  <input type="checkbox" checked={clearToken} onChange={(e) => setClearToken(e.target.checked)} />
                  <span>clear configured token</span>
                </label>
              )}
            </div>
          </>
        ) : null}
      </Card>

      <Card title="Note">
        <div className="notice-box">
          The control token is only ever read from environment / local secret config and is never returned in plaintext. This
          page shows whether a token is configured, and lets you set or clear it — but never displays its value.
        </div>
      </Card>
    </div>
  );
}
