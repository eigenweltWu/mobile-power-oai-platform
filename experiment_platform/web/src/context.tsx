import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

export type OperatorContext = {
  experimentId: string;
  environment: string;
  configurationId: number | null;
  configurationName: string;
  runId: string;
  status: string;
  quality: string;
};

const EMPTY: OperatorContext = {
  experimentId: '', environment: '', configurationId: null,
  configurationName: '', runId: '', status: '', quality: '',
};

const Context = createContext<{
  value: OperatorContext;
  update: (patch: Partial<OperatorContext>) => void;
}>({ value: EMPTY, update: () => undefined });

export function OperatorContextProvider({ children }: { children: ReactNode }) {
  const [value, setValue] = useState<OperatorContext>(() => {
    try { return { ...EMPTY, ...JSON.parse(localStorage.getItem('operator-context') || '{}') }; }
    catch { return EMPTY; }
  });
  useEffect(() => localStorage.setItem('operator-context', JSON.stringify(value)), [value]);
  const update = useCallback((patch: Partial<OperatorContext>) => setValue((old) => {
    const next = { ...old, ...patch };
    return JSON.stringify(next) === JSON.stringify(old) ? old : next;
  }), []);
  const contextValue = useMemo(() => ({ value, update }), [value, update]);
  return <Context.Provider value={contextValue}>
    {children}
  </Context.Provider>;
}

export function useOperatorContext() { return useContext(Context); }
