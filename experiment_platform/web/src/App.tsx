import { Component, useEffect, useState, type ErrorInfo, type ReactNode } from 'react';
import Dashboard from './pages/Dashboard';
import Experiments from './pages/Experiments';
import Timeline from './pages/Timeline';
import Settings from './pages/Settings';
import PhoneSync from './pages/PhoneSync';
import Chamber from './pages/Chamber';
import { Badge, Card, Icon, ToastHost } from './components/ui';
import { OperatorContextProvider, useOperatorContext } from './context';

class PageErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('Page render failed', error, info); }
  render() { return this.state.error ? <div className="error-box"><b>Page render failed</b><div>{this.state.error.message}</div><div>Reload the page or return to Dashboard.</div></div> : this.props.children; }
}

const NAV: { path: string; label: string; icon: string }[] = [
  { path: '/dashboard', label: 'Dashboard', icon: 'grid' },
  { path: '/experiments', label: 'Experiments', icon: 'flask' },
  { path: '/sync', label: 'Data Import', icon: 'download' },
  { path: '/advanced', label: 'Advanced', icon: 'settings' },
];

const TITLES: Record<string, string> = {
  dashboard: 'Dashboard',
  experiments: 'Experiments',
  timeline: 'Result Workspace',
  sync: 'Data Import',
  advanced: 'Advanced',
};

function useHashRoute(): [string, (path: string) => void] {
  const [hash, setHash] = useState<string>(() => window.location.hash.slice(1) || '/dashboard');
  useEffect(() => {
    const onHash = () => setHash(window.location.hash.slice(1) || '/dashboard');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const nav = (path: string) => {
    window.location.hash = path;
  };
  return [hash, nav];
}

function ContextBar() {
  const { value } = useOperatorContext();
  return <div className="context-bar" aria-label="Current experiment and run context">
    <div><span>Experiment</span><b>{value.experimentId || '—'}</b></div>
    <div><span>Environment</span><b>{value.environment || '—'}</b></div>
    <div><span>Configuration</span><b>{value.configurationName || '—'}</b></div>
    <div><span>Run</span><b>{value.runId || '—'}</b></div>
    <div><span>Status</span>{value.status ? <Badge tone={value.status === 'RUNNING' ? 'warn' : value.status === 'COMPLETE' ? 'good' : 'muted'}>{value.status}</Badge> : <b>—</b>}</div>
    <div><span>Quality</span><b>{value.quality || '—'}</b></div>
  </div>;
}

function Advanced({ nav }: { nav: (path: string) => void }) {
  return <div className="stack"><div className="page-head"><div><div className="title">Advanced / Research Tools</div><div className="subtitle">Hardware diagnostics and administrative settings outside the normal Run workflow.</div></div></div>
    <div className="grid cols-2"><Card title="RC Hardware" sub="Connection, manual jog, DLL, helper and simulation diagnostics."><button className="btn" onClick={() => nav('/advanced/rc-hardware')}>Open diagnostics</button></Card><Card title="System Settings" sub="OAI connectivity and administrative settings."><button className="btn" onClick={() => nav('/advanced/settings')}>Open settings</button></Card></div>
  </div>;
}

function AppShell() {
  const [hash, nav] = useHashRoute();
  const pathPart = hash.split('?')[0];
  const segments = pathPart.replace(/^\/+/, '').split('/').filter(Boolean);
  const section = segments[0] || 'dashboard';

  const isActive = (path: string) => section === path.slice(1);

  let page: React.ReactNode;
  switch (section) {
    case 'dashboard':
      page = <Dashboard nav={nav} />;
      break;
    case 'experiments':
      page = segments[1] && segments[2] === 'rc'
        ? <Chamber initialExperimentId={decodeURIComponent(segments[1])} onBack={() => nav('/experiments')} />
        : <Experiments nav={nav} initialExperimentId={segments[1] ? decodeURIComponent(segments[1]) : ''} />;
      break;
    case 'timeline':
      page = <Timeline experimentId={decodeURIComponent(segments[1] || '')}
        initialRunId={decodeURIComponent(segments[2] || '')} onBack={() => nav('/experiments')} />;
      break;
    case 'sync':
      page = <PhoneSync />;
      break;
    case 'advanced':
      page = segments[1] === 'rc-hardware' ? <Chamber onBack={() => nav('/advanced')} />
        : segments[1] === 'settings' ? <Settings />
        : <Advanced nav={nav} />;
      break;
    // Removed pages (to be rebuilt later): Run Detail, AC/RC Compare,
    // Matrix, Data and Export live inside the Experiments hub.
    case 'run':
    case 'comparison':
    case 'matrix':
    case 'data':
    case 'export':
    case 'settings':
      page = <Experiments nav={nav} />;
      break;
    default:
      page = <Dashboard nav={nav} />;
  }

  const title = TITLES[section] ?? 'Dashboard';

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="logo">
            <svg viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2v20M4 7l3-3 3 3M4 17l3 3 3-3M16 4h4M16 9h4M16 14h4M16 19h4" />
            </svg>
          </div>
          <div>
            <div className="brand-name">5G Energy Platform</div>
            <div className="brand-sub">research instrument</div>
          </div>
        </div>
        <nav className="side-nav">
          {NAV.map((n) => (
            <button key={n.path} className={isActive(n.path) ? 'active' : ''} onClick={() => nav(n.path)}>
              <Icon name={n.icon} />
              {n.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">v0.1.0 · OAI lab</div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <div className="topbar-title">
            <span className="crumb">platform /</span>
            {title}
          </div>
          <div className="topbar-actions">
            <span className="live-dot">live</span>
          </div>
        </header>
        <ContextBar />
        <main className="page"><PageErrorBoundary key={hash}>{page}</PageErrorBoundary></main>
      </div>
      <ToastHost />
    </div>
  );
}

export default function App() {
  return <OperatorContextProvider><AppShell /></OperatorContextProvider>;
}
