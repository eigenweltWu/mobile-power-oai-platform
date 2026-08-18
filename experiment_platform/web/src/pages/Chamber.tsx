import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { Badge, Card, ErrorBox, Icon, StatCard, toast } from '../components/ui';

type StirrerStatus = {
  simulated: boolean;
  opened: boolean;
  connected: boolean;
  position_deg?: number | null;
  running?: boolean;
  exe_ready?: boolean;
  dll_found?: boolean;
  last_error?: string;
};

export default function Chamber({ onBack }: { initialExperimentId?: string; onBack?: () => void }) {
  const [status, setStatus] = useState<StirrerStatus | null>(null);
  const [simulated, setSimulated] = useState(false);
  const [jogDegrees, setJogDegrees] = useState(5);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try { setStatus(await api.get(`/api/stirrer/status?simulate=${simulated}`)); setError(null); }
    catch (cause) { setError(cause); }
  }, [simulated]);

  useEffect(() => { load(); const timer = setInterval(load, 5000); return () => clearInterval(timer); }, [load]);

  const action = async (path: string, body: Record<string, unknown>, success: string) => {
    setBusy(true);
    try { await api.post(path, body); await load(); toast('ok', success); }
    catch (cause) { setError(cause); toast('err', cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };

  return <div className="stack">
    <div className="page-head"><div><div className="title">RC Hardware Diagnostics</div><div className="subtitle">连接检查与人工点动；配置、Preflight 和 Campaign 执行仅在 Dashboard / Run Control。</div></div>{onBack && <button className="btn" onClick={onBack}>← Advanced</button>}</div>
    {error != null && <ErrorBox error={error} />}
    <div className="notice-box">此页不会保存 RC Configuration，也不会启动或停止 Run。Execution Mode 由选中的 Configuration 唯一决定。</div>
    <div className="stat-grid">
      <StatCard label="Controller" value={status?.connected ? 'CONNECTED' : 'DISCONNECTED'} sub={status?.last_error || 'MT_API · USB'} tone={status?.connected ? 'good' : 'bad'} />
      <StatCard label="Position" value={status?.position_deg == null ? 'Unavailable' : `${status.position_deg.toFixed(1)}°`} sub={status?.running ? 'Motor moving' : 'Motor idle'} tone={status?.running ? 'warn' : 'muted'} />
      <StatCard label="Runtime" value={status?.simulated ? 'SIMULATION' : 'REAL HARDWARE'} sub="Diagnostic connection only" tone={status?.simulated ? 'warn' : 'accent'} />
    </div>
    <Card title="Driver Readiness" sub="Read-only checks used by Run Control Preflight."><div className="row"><Badge tone={status?.dll_found ? 'good' : 'bad'}>MT_API.dll {status?.dll_found ? 'FOUND' : 'MISSING'}</Badge><Badge tone={status?.exe_ready ? 'good' : 'bad'}>Helper {status?.exe_ready ? 'READY' : 'NOT READY'}</Badge><Badge tone={status?.opened ? 'good' : 'muted'}>Session {status?.opened ? 'OPEN' : 'CLOSED'}</Badge></div></Card>
    <Card title="Connection & Manual Jog" sub="Advanced diagnostic actions are disabled while a command is in progress."><div className="grid cols-3"><label className="field"><span>Diagnostic mode</span><select value={simulated ? 'simulation' : 'hardware'} disabled={busy || status?.connected} onChange={(event) => setSimulated(event.target.value === 'simulation')}><option value="hardware">Real hardware</option><option value="simulation">Simulation</option></select></label><label className="field"><span>Jog angle (°)</span><input type="number" min={0.1} step={0.5} value={jogDegrees} disabled={busy} onChange={(event) => setJogDegrees(Number(event.target.value))} /></label><div className="row"><button className="btn primary" disabled={busy || status?.connected} onClick={() => action('/api/stirrer/connect', { simulate: simulated }, 'Diagnostic session connected.')}><Icon name="play" size={14} /> Connect</button><button className="btn" disabled={busy || !status?.connected} onClick={() => action('/api/stirrer/disconnect', { simulate: simulated }, 'Diagnostic session disconnected.')}>Disconnect</button></div></div><div className="row" style={{ marginTop: 12 }}><button className="btn" disabled={busy || !status?.connected} onClick={() => action('/api/stirrer/move', { deg: -jogDegrees, simulate: simulated }, `Jogged −${jogDegrees}°.`)}>−{jogDegrees}°</button><button className="btn" disabled={busy || !status?.connected} onClick={() => action('/api/stirrer/move', { deg: jogDegrees, simulate: simulated }, `Jogged +${jogDegrees}°.`)}>+{jogDegrees}°</button><button className="btn" disabled={busy} onClick={load}>Refresh</button></div></Card>
  </div>;
}
