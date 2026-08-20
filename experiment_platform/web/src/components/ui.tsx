import { useEffect, useState, type ReactNode } from 'react';

/* ------------------------------------------------------------------ */
/* Icons (inline stroke SVGs, no external dependency)                  */
/* ------------------------------------------------------------------ */
const ICONS: Record<string, ReactNode> = {
  grid: (
    <>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </>
  ),
  flask: (
    <>
      <path d="M9 3h6M10 3v5l-5.5 9.5A2 2 0 0 0 6.2 21h11.6a2 2 0 0 0 1.7-3.5L14 8V3" />
      <path d="M7.5 15h9" />
    </>
  ),
  route: (
    <>
      <circle cx="6" cy="18" r="2.5" />
      <circle cx="18" cy="6" r="2.5" />
      <path d="M8.5 18h4a3.5 3.5 0 0 0 0-7h-1a3.5 3.5 0 0 1 0-7h4" />
    </>
  ),
  play: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M10 8.5v7l6-3.5z" fill="currentColor" stroke="none" />
    </>
  ),
  compare: (
    <>
      <path d="M12 3v18" />
      <path d="M4 7l3-3 3 3M4 17l3 3 3-3" />
    </>
  ),
  matrix: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M3 9h18M3 15h18M9 3v18M15 3v18" />
    </>
  ),
  data: (
    <>
      <ellipse cx="12" cy="5" rx="8" ry="3" />
      <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z" />
    </>
  ),
  download: (
    <>
      <path d="M12 3v12M7 10l5 5 5-5M4 20h16" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="11" width="14" height="9" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </>
  ),
  check: <path d="M20 6 9 17l-5-5" />,
  x: <path d="M18 6 6 18M6 6l12 12" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8h.01M12 11v5" />
    </>
  ),
  refresh: (
    <>
      <path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" />
    </>
  ),
};

export function Icon({ name, size = 18, className }: { name: string; size?: number; className?: string }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICONS[name] ?? null}
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/* Card                                                               */
/* ------------------------------------------------------------------ */
export function Card({
  title,
  sub,
  right,
  children,
  className,
}: {
  title?: ReactNode;
  sub?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className ?? ''}`}>
      {(title || sub || right) && (
        <div className="card-header">
          <div>
            {title ? <h2>{title}</h2> : null}
            {sub ? <div className="card-sub">{sub}</div> : null}
          </div>
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* StatCard                                                           */
/* ------------------------------------------------------------------ */
export function StatCard({
  label,
  value,
  sub,
  tone = 'muted',
  icon,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: 'good' | 'warn' | 'bad' | 'muted' | 'accent';
  icon?: string;
}) {
  return (
    <div className={`stat-card ${tone}`}>
      <div className="stat-label">
        {icon ? <Icon name={icon} className="icon" /> : null}
        {label}
      </div>
      <div className="stat-value">{value}</div>
      {sub ? <div className="stat-sub">{sub}</div> : null}
    </div>
  );
}

export function Badge({ tone = 'muted', children }: { tone?: 'good' | 'warn' | 'bad' | 'muted' | 'accent'; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function ErrorBox({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return <div className="error-box">{msg}</div>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>;
}

export function Spinner({ label = 'loading…' }: { label?: string }) {
  return <div className="spinner">{label}</div>;
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field" data-hint={hint}>
      <span>{label}</span>
      {children}
      {hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  );
}

/* ------------------------------------------------------------------ */
/* Modal (floating window)                                            */
/* ------------------------------------------------------------------ */
export function Modal({
  title,
  sub,
  onClose,
  children,
  footer,
  size,
}: {
  title: ReactNode;
  sub?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  size?: 'md' | 'lg';
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onPointerDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className={`modal ${size === 'lg' ? 'lg' : ''}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h3>{title}</h3>
            {sub ? <div className="modal-sub">{sub}</div> : null}
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="close">
            <Icon name="x" size={18} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-footer">{footer}</div> : null}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Toast host + imperative `toast()`                                   */
/* ------------------------------------------------------------------ */
type ToastItem = { id: number; kind: 'ok' | 'err' | 'info'; text: string };

let nextToastId = 1;
let pushToast: ((t: ToastItem) => void) | null = null;

export function toast(kind: 'ok' | 'err' | 'info', text: string) {
  pushToast?.({ id: nextToastId++, kind, text });
}

export function ToastHost() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => {
    pushToast = (t) => {
      setItems((prev) => [...prev, t]);
      window.setTimeout(() => {
        setItems((prev) => prev.filter((x) => x.id !== t.id));
      }, 5000);
    };
    return () => {
      pushToast = null;
    };
  }, []);

  return (
    <div className="toast-host">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <span className="t-icon">
            <Icon name={t.kind === 'err' ? 'x' : t.kind === 'ok' ? 'check' : 'info'} size={15} />
          </span>
          <div className="t-body">{t.text}</div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Static (read-only) value — for fields users must not edit           */
/* ------------------------------------------------------------------ */
export function StaticValue({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="static-value" title={title}>
      <Icon name="lock" size={12} />
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Full-screen blocking loader                                         */
/* ------------------------------------------------------------------ */
export function FullScreenLoader({ title, steps, active }: { title: string; steps: string[]; active: number }) {
  return (
    <div className="fs-backdrop">
      <div className="fs-panel">
        <div className="fs-spinner" />
        <div className="fs-title">{title}</div>
        <div className="fs-steps">
          {steps.map((s, i) => (
            <div key={i} className={`fs-step ${i < active ? 'done' : i === active ? 'active' : ''}`}>
              <span className="fs-dot">{i < active ? '✓' : ''}</span>
              {s}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* tone helpers                                                       */
/* ------------------------------------------------------------------ */
export function qualityTone(status: string | null | undefined): 'good' | 'warn' | 'bad' | 'muted' {
  if (status === 'PASS' || status === 'COMPLETE') return 'good';
  if (status === 'WARNING') return 'warn';
  if (status === 'FAILED') return 'bad';
  return 'muted';
}

export function stateTone(state: string | null | undefined): 'good' | 'warn' | 'bad' | 'muted' {
  if (!state) return 'muted';
  if (state === 'COMPLETE') return 'good';
  if (state === 'WARNING') return 'warn';
  if (state === 'FAILED') return 'bad';
  return 'muted';
}

export function runTone(state: string | null | undefined): 'good' | 'warn' | 'bad' | 'muted' {
  if (state === 'FAILED') return 'bad';
  if (state === 'WARNING') return 'warn';
  if (state === 'COMPLETE') return 'good';
  return 'muted';
}

export function envTone(env: string | null | undefined): 'accent' | 'muted' {
  return env === 'AC' || env === 'RC' ? 'accent' : 'muted';
}
