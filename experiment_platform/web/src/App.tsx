import { useEffect, useState } from 'react';
import Dashboard from './pages/Dashboard';
import Experiments from './pages/Experiments';
import Timeline from './pages/Timeline';
import Settings from './pages/Settings';
import PhoneSync from './pages/PhoneSync';
import Chamber from './pages/Chamber';
import { Icon, ToastHost } from './components/ui';

const NAV: { path: string; label: string; icon: string }[] = [
  { path: '/dashboard', label: 'Dashboard', icon: 'grid' },
  { path: '/experiments', label: 'Experiments', icon: 'flask' },
  { path: '/sync', label: 'Phone Sync', icon: 'download' },
  { path: '/chamber', label: 'Chamber RC', icon: 'compare' },
  { path: '/settings', label: 'Settings', icon: 'settings' },
];

const TITLES: Record<string, string> = {
  dashboard: 'Dashboard',
  experiments: 'Experiments',
  timeline: 'Timeline',
  sync: 'Phone Sync',
  chamber: 'Chamber RC',
  settings: 'Settings',
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

export default function App() {
  const [hash, nav] = useHashRoute();
  const [pathPart, queryPart] = hash.split('?');
  const params = new URLSearchParams(queryPart || '');
  const segments = pathPart.replace(/^\/+/, '').split('/').filter(Boolean);
  const section = segments[0] || 'dashboard';
  const initialExperimentId = params.get('exp') ?? undefined;

  const isActive = (path: string) => section === path.slice(1);

  let page: React.ReactNode;
  switch (section) {
    case 'dashboard':
      page = <Dashboard />;
      break;
    case 'experiments':
      page = <Experiments nav={nav} />;
      break;
    case 'timeline':
      page = <Timeline experimentId={decodeURIComponent(segments[1] || '')} />;
      break;
    case 'sync':
      page = <PhoneSync />;
      break;
    case 'chamber':
      page = <Chamber />;
      break;
    case 'settings':
      page = <Settings />;
      break;
    // Removed pages (to be rebuilt later): Run Detail, AC/RC Compare,
    // Matrix, Data. Also Run Planner / Export live inside the Experiments hub.
    case 'run':
    case 'comparison':
    case 'matrix':
    case 'data':
    case 'planner':
    case 'export':
      page = <Experiments nav={nav} />;
      break;
    default:
      page = <Dashboard />;
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
        <main className="page">{page}</main>
      </div>
      <ToastHost />
    </div>
  );
}
