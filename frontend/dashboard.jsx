import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { Play, Square } from 'lucide-react';
import { AreaChart, Area, ResponsiveContainer, XAxis, YAxis, Tooltip } from 'recharts';

const C = {
  bg: '#12151C',
  panel: '#1A1F29',
  panelAlt: '#1F2530',
  hairline: '#2A3040',
  amber: '#E8A33D',
  long: '#4CAE7C',
  short: '#D9584F',
  paper: '#ECE9E2',
  muted: '#838B9C',
};

const DEFAULT_API_BASE = 'http://localhost:8000';

// ---------------- API helpers ----------------

async function apiGet(base, path) {
  const res = await fetch(`${base}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function apiPost(base, path, body) {
  const res = await fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function timeAgo(unixSeconds) {
  if (!unixSeconds) return '—';
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - unixSeconds);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// ---------------- polling hook ----------------
// setTimeout-chained (not setInterval) so a slow request never overlaps the
// next tick. fetchFn is read through a ref so callers can pass a fresh
// closure every render without restarting the timer.

function usePolling(fetchFn, intervalMs) {
  const [state, setState] = useState({ data: null, loading: true, error: null });
  const fetchRef = useRef(fetchFn);
  fetchRef.current = fetchFn;

  const runOnce = useCallback(async () => {
    try {
      const data = await fetchRef.current();
      setState({ data, loading: false, error: null });
      return data;
    } catch (err) {
      setState((s) => ({ data: s.data, loading: false, error: err.message || 'Request failed' }));
      throw err;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer;
    async function tick() {
      try { await runOnce(); } catch (e) { /* recorded in state already */ }
      if (!cancelled) timer = setTimeout(tick, intervalMs);
    }
    tick();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [runOnce, intervalMs]);

  return { ...state, refetch: runOnce };
}

function StructureMark({ color = C.amber, size = 14 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none"
      className="inline-block align-middle mr-1.5 flex-shrink-0">
      <rect x="1.5" y="5" width="13" height="6" rx="1" fill={color} fillOpacity="0.18" stroke={color} strokeWidth="1.1" />
      <path d="M1.5 5V2.5M14.5 5V2.5" stroke={color} strokeWidth="1.1" strokeLinecap="round" />
    </svg>
  );
}

function PositionCard({ pos }) {
  const isLong = pos.direction === 'long';
  const dirColor = isLong ? C.long : C.short;
  const legs = pos.tp_legs || [];
  const breakevenActive = pos.breakeven_applied === 1;
  return (
    <div style={{ background: C.panel, border: `1px solid ${C.hairline}` }} className="rounded-lg p-4">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="font-display font-semibold">{pos.symbol}</span>
          <span className="text-xs px-2 py-0.5 rounded font-medium" style={{ background: `${dirColor}22`, color: dirColor }}>
            {isLong ? 'LONG' : 'SHORT'}
          </span>
          {breakevenActive && (
            <span className="text-xs px-2 py-0.5 rounded" style={{ color: C.amber, background: `${C.amber}1a` }}>
              SL at breakeven
            </span>
          )}
        </div>
        <span className="font-mono-data text-sm" style={{ color: C.muted }}>{pos.position_size} units</span>
      </div>

      <div className="grid grid-cols-2 gap-3 mb-2 font-mono-data text-sm">
        <div><span style={{ color: C.muted }}>Entry </span>{pos.entry_price.toFixed(4)}</div>
        <div><span style={{ color: C.muted }}>Stop </span><span style={{ color: C.short }}>{pos.stop_loss.toFixed(4)}</span></div>
      </div>
      {pos.sl_reason && (
        <div className="text-xs mb-4" style={{ color: C.muted }}>
          <StructureMark color={C.short} size={12} />{pos.sl_reason}
        </div>
      )}

      <div className="space-y-2">
        {legs.map((tp) => (
          <div key={tp.id} className="flex items-center justify-between text-sm gap-2 flex-wrap">
            <div className="flex items-center" style={{ color: tp.hit === 1 ? C.long : C.paper }}>
              <StructureMark color={tp.hit === 1 ? C.long : C.amber} />
              <span>
                TP{tp.level} <span className="font-mono-data">{tp.price.toFixed(4)}</span>
                <span style={{ color: C.muted }}> · {(tp.close_fraction * 100).toFixed(0)}%</span>
                {tp.hit === 1 && <span style={{ color: C.long }}> · hit</span>}
              </span>
            </div>
            <span className="text-xs" style={{ color: C.muted }}>{tp.reason}</span>
          </div>
        ))}
      </div>
      <div className="text-xs mt-3" style={{ color: C.muted }}>confidence {(pos.confidence * 100).toFixed(0)}%</div>
    </div>
  );
}

function SignalRow({ s }) {
  const dirColor = s.direction === 'long' ? C.long : C.short;
  return (
    <div className="p-3 flex items-center justify-between gap-3 text-sm">
      <div className="flex items-center gap-2 min-w-0">
        <span className="font-medium flex-shrink-0" style={{ color: dirColor }}>{s.direction.toUpperCase()}</span>
        <span className="flex-shrink-0" style={{ color: C.muted }}>{s.symbol}</span>
        <span className="truncate hidden sm:inline" style={{ color: C.muted }}>{(s.reasons || [])[0]}</span>
      </div>
      <div className="flex items-center gap-3 flex-shrink-0">
        <span className="text-xs font-mono-data hidden md:inline" style={{ color: C.muted }}>{timeAgo(s.ts)}</span>
        <div className="w-14 h-1.5 rounded-full" style={{ background: C.hairline }}>
          <div className="h-1.5 rounded-full" style={{ width: `${s.confidence * 100}%`, background: C.amber }} />
        </div>
        <span className="text-xs px-2 py-0.5 rounded whitespace-nowrap" style={{
          background: s.taken === 1 ? `${C.long}22` : C.panelAlt,
          color: s.taken === 1 ? C.long : C.muted,
        }}>
          {s.taken === 1 ? 'Taken' : 'Skipped'}
        </span>
      </div>
    </div>
  );
}

function HistoryRow({ h }) {
  const pnl = h.realized_pnl || 0;
  const win = pnl >= 0;
  return (
    <div className="p-3 flex items-center justify-between text-sm">
      <div className="flex items-center gap-2 min-w-0">
        <span className="font-medium">{h.symbol}</span>
        <span style={{ color: C.muted }}>{h.direction}</span>
        <span className="text-xs truncate" style={{ color: C.muted }}>{h.close_reason}</span>
      </div>
      <span className="font-mono-data flex-shrink-0" style={{ color: win ? C.long : C.short }}>
        {win ? '+' : ''}{pnl.toFixed(2)}
      </span>
    </div>
  );
}

function LogRow({ log }) {
  const levelColor = log.level === 'error' ? C.short : log.level === 'warning' ? C.amber : C.muted;
  return (
    <div className="p-3 flex items-start gap-3 text-sm" style={{ borderLeft: `3px solid ${levelColor}` }}>
      <span className="text-xs font-mono-data flex-shrink-0 mt-0.5 w-14" style={{ color: C.muted }}>{timeAgo(log.ts)}</span>
      <span className="text-xs uppercase font-medium flex-shrink-0 mt-0.5 w-16" style={{ color: levelColor }}>{log.level}</span>
      <span className="text-xs px-1.5 py-0.5 rounded flex-shrink-0" style={{ background: C.panelAlt, color: C.muted }}>{log.source}</span>
      <span className="flex-1 min-w-0 break-words font-mono-data text-xs leading-relaxed" style={{ color: C.paper }}>{log.message}</span>
    </div>
  );
}

function Field({ label, value, onChange, step = 0.1 }) {
  return (
    <div className="mb-3">
      <label className="text-xs block mb-1" style={{ color: C.muted }}>{label}</label>
      <input type="number" step={step} value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full px-2 py-1.5 rounded text-sm font-mono-data outline-none"
        style={{ background: C.bg, border: `1px solid ${C.hairline}`, color: C.paper }} />
    </div>
  );
}

function TextField({ label, value, onChange, type = 'text', placeholder = '' }) {
  return (
    <div className="mb-3">
      <label className="text-xs block mb-1" style={{ color: C.muted }}>{label}</label>
      <input type={type} value={value} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)} autoComplete="off"
        className="w-full px-2 py-1.5 rounded text-sm font-mono-data outline-none"
        style={{ background: C.bg, border: `1px solid ${C.hairline}`, color: C.paper }} />
    </div>
  );
}

function CredentialForm({ mode, apiBase, credInfo, onSaved }) {
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [pin, setPin] = useState('');
  const [state, setState] = useState('idle'); // idle | saving | saved | error
  const [error, setError] = useState(null);
  const label = mode === 'live' ? 'Live (real money)' : 'Demo (paper trading)';

  async function handleSave() {
    setState('saving');
    setError(null);
    try {
      const res = await apiPost(apiBase, '/api/credentials', {
        mode, api_key: apiKey, api_secret: apiSecret, passphrase, pin,
      });
      if (res.error) throw new Error(res.error);
      setState('saved');
      setApiKey(''); setApiSecret(''); setPassphrase(''); setPin('');
      onSaved && onSaved();
      setTimeout(() => setState('idle'), 2000);
    } catch (err) {
      setState('error');
      setError(err.message);
    }
  }

  return (
    <div style={{ background: C.panel, border: `1px solid ${C.hairline}` }} className="rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-display text-sm" style={{ color: mode === 'live' ? C.short : C.paper }}>{label}</h3>
        <span className="text-xs px-2 py-0.5 rounded" style={{
          background: credInfo?.configured ? `${C.long}22` : C.panelAlt,
          color: credInfo?.configured ? C.long : C.muted,
        }}>
          {credInfo?.configured ? 'Attached' : 'Not attached'}
        </span>
      </div>
      <TextField label="API Key" value={apiKey} onChange={setApiKey} placeholder={credInfo?.configured ? 'Leave blank to keep current' : 'Paste your Bitget API key'} />
      <TextField label="API Secret" value={apiSecret} onChange={setApiSecret} type="password" placeholder={credInfo?.configured ? 'Leave blank to keep current' : ''} />
      <TextField label="Passphrase" value={passphrase} onChange={setPassphrase} type="password" placeholder={credInfo?.configured ? 'Leave blank to keep current' : ''} />
      <TextField label={mode === 'live' ? 'PIN (required for live)' : 'PIN'} value={pin} onChange={setPin} type="password" placeholder="••••" />
      <button onClick={handleSave} disabled={state === 'saving' || !apiKey || !apiSecret || !passphrase}
        className="w-full py-2 rounded text-sm font-medium mt-1 transition-colors disabled:opacity-50"
        style={{ background: state === 'saved' ? C.long : C.amber, color: C.bg }}>
        {state === 'saving' ? 'Saving…' : state === 'saved' ? 'Saved' : `Save ${label.split(' ')[0]} keys`}
      </button>
      {state === 'error' && <div className="text-xs mt-2" style={{ color: C.short }}>{error}</div>}
      <p className="text-xs mt-3" style={{ color: C.muted }}>
        Sent straight to your own server and encrypted before it touches the database. Never shown again after saving — only a masked hint.
      </p>
    </div>
  );
}

function ModeSwitcher({ apiBase, status, credsQ, onChanged }) {
  const [open, setOpen] = useState(false);
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const isLive = !!status.data?.live_mode_active;

  async function submit(goLive) {
    setBusy(true);
    setError(null);
    try {
      const res = await apiPost(apiBase, '/api/mode/set', { live: goLive, pin: goLive ? pin : '' });
      if (res.error) throw new Error(res.error);
      setOpen(false);
      setPin('');
      onChanged && onChanged();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative">
      <div className="flex items-center gap-1.5">
        <span className="text-xs px-2 py-1 rounded font-mono-data font-medium" style={{
          background: isLive ? `${C.short}22` : C.panelAlt,
          color: isLive ? C.short : C.amber,
          border: `1px solid ${C.hairline}`,
        }}>
          {!status.data ? '···' : isLive ? 'LIVE — REAL MONEY' : 'DEMO MODE'}
        </span>
        <button onClick={() => { setOpen(!open); setError(null); }}
          className="text-xs px-2 py-1 rounded transition-colors"
          style={{ background: C.panelAlt, color: C.muted, border: `1px solid ${C.hairline}` }}>
          Switch
        </button>
      </div>

      {open && (
        <div style={{ background: C.panel, border: `1px solid ${C.hairline}` }}
          className="absolute right-0 mt-2 p-3 rounded-lg shadow-lg z-10 w-64">
          {isLive ? (
            <>
              <p className="text-xs mb-2" style={{ color: C.muted }}>Switch back to demo? No PIN needed.</p>
              <button onClick={() => submit(false)} disabled={busy}
                className="w-full py-1.5 rounded text-sm font-medium" style={{ background: C.amber, color: C.bg }}>
                {busy ? 'Switching…' : 'Confirm — switch to demo'}
              </button>
            </>
          ) : (
            <>
              <p className="text-xs mb-2" style={{ color: C.muted }}>
                Enter the PIN to go live. Requires live keys already attached in the Connect tab.
              </p>
              <input type="password" value={pin} onChange={(e) => setPin(e.target.value)} placeholder="PIN"
                className="w-full px-2 py-1.5 rounded text-sm font-mono-data outline-none mb-2"
                style={{ background: C.bg, border: `1px solid ${C.hairline}`, color: C.paper }} />
              <button onClick={() => submit(true)} disabled={busy || !pin}
                className="w-full py-1.5 rounded text-sm font-medium disabled:opacity-50"
                style={{ background: C.short, color: '#fff' }}>
                {busy ? 'Switching…' : 'Confirm — go live'}
              </button>
            </>
          )}
          {error && <div className="text-xs mt-2" style={{ color: C.short }}>{error}</div>}
        </div>
      )}
    </div>
  );
}

export default function KehloDashboard() {
  const [apiBase, setApiBase] = useState(DEFAULT_API_BASE);
  const [apiBaseInput, setApiBaseInput] = useState(DEFAULT_API_BASE);
  const [activeTab, setActiveTab] = useState('positions');
  const [logFilter, setLogFilter] = useState('all');

  const status = usePolling(useCallback(() => apiGet(apiBase, '/api/status'), [apiBase]), 5000);
  const openTrades = usePolling(useCallback(() => apiGet(apiBase, '/api/trades/open'), [apiBase]), 5000);
  const history = usePolling(useCallback(() => apiGet(apiBase, '/api/trades/history?limit=100'), [apiBase]), 10000);
  const signalsQ = usePolling(useCallback(() => apiGet(apiBase, '/api/signals/recent?limit=50'), [apiBase]), 8000);
  const logsQ = usePolling(useCallback(() => apiGet(apiBase,
    `/api/logs?limit=200${logFilter !== 'all' ? '&level=' + logFilter : ''}`), [apiBase, logFilter]), 5000);
  const statsQ = usePolling(useCallback(() => apiGet(apiBase, '/api/stats'), [apiBase]), 10000);
  const credsQ = usePolling(useCallback(() => apiGet(apiBase, '/api/credentials/status'), [apiBase]), 10000);

  // force an immediate refresh of everything when the API base changes,
  // instead of waiting for each hook's own timer
  useEffect(() => {
    [status, openTrades, history, signalsQ, logsQ, statsQ, credsQ].forEach((q) => q.refetch().catch(() => {}));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase]);

  // settings form — hydrate ONCE from the server, then leave it alone so
  // polling doesn't overwrite what the person is mid-typing
  const settingsHydrated = useRef(false);
  const [riskPct, setRiskPct] = useState(1.0);
  const [maxPositions, setMaxPositions] = useState(3);
  const [maxDailyLoss, setMaxDailyLoss] = useState(5.0);
  const [symbolsText, setSymbolsText] = useState('');
  const [timeframe, setTimeframe] = useState('15m');

  useEffect(() => {
    if (status.data?.settings && !settingsHydrated.current) {
      const s = status.data.settings;
      setRiskPct(s.risk_per_trade_pct);
      setMaxPositions(s.max_concurrent_positions);
      setMaxDailyLoss(s.max_daily_loss_pct);
      setSymbolsText((s.symbols || []).join(', '));
      setTimeframe(s.timeframe);
      settingsHydrated.current = true;
    }
  }, [status.data]);

  const [saveState, setSaveState] = useState('idle');
  const [saveError, setSaveError] = useState(null);

  async function handleSaveSettings() {
    setSaveState('saving');
    setSaveError(null);
    try {
      await apiPost(apiBase, '/api/settings', {
        risk_per_trade_pct: riskPct,
        max_concurrent_positions: maxPositions,
        max_daily_loss_pct: maxDailyLoss,
        symbols: symbolsText.split(',').map((s) => s.trim()).filter(Boolean),
        timeframe,
      });
      setSaveState('saved');
      status.refetch().catch(() => {});
      setTimeout(() => setSaveState('idle'), 1500);
    } catch (err) {
      setSaveState('error');
      setSaveError(err.message);
    }
  }

  const [botActionPending, setBotActionPending] = useState(false);
  const [botActionError, setBotActionError] = useState(null);
  const [confirmingLiveStart, setConfirmingLiveStart] = useState(false);

  async function doToggleBot() {
    setBotActionPending(true);
    setBotActionError(null);
    try {
      if (status.data?.bot_running) {
        await apiPost(apiBase, '/api/bot/stop');
      } else {
        await apiPost(apiBase, '/api/bot/start');
      }
      await status.refetch();
    } catch (err) {
      setBotActionError(err.message);
    } finally {
      setBotActionPending(false);
    }
  }

  function handleBotButtonClick() {
    const aboutToStartLive = !status.data?.bot_running && status.data?.live_mode_active;
    if (aboutToStartLive && !confirmingLiveStart) {
      setConfirmingLiveStart(true);
      setTimeout(() => setConfirmingLiveStart(false), 4000);
      return;
    }
    setConfirmingLiveStart(false);
    doToggleBot();
  }

  const equityCurve = useMemo(() => {
    if (!history.data || history.data.length === 0) return [];
    const sorted = [...history.data].sort((a, b) => (a.closed_at || 0) - (b.closed_at || 0));
    let cum = 0;
    return sorted.map((t, i) => { cum += (t.realized_pnl || 0); return { t: i, equity: cum }; });
  }, [history.data]);

  const connState = (status.loading && !status.data) ? 'connecting' : status.error ? 'error' : 'ok';
  const hasErrorLogs = (logsQ.data || []).some((l) => l.level === 'error');

  const TABS = [
    { id: 'positions', label: 'Positions', count: openTrades.data?.length },
    { id: 'signals', label: 'Signals', count: null },
    { id: 'history', label: 'History', count: null },
    { id: 'logs', label: 'Logs', count: null, alert: hasErrorLogs },
    { id: 'connect', label: 'Connect', count: null, alert: credsQ.data && !credsQ.data.demo?.configured && !credsQ.data.live?.configured },
    { id: 'settings', label: 'Settings', count: null },
  ];

  return (
    <div style={{ background: C.bg, color: C.paper, minHeight: '100vh' }} className="w-full font-sans">
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');
        .font-sans { font-family: 'Inter', system-ui, sans-serif; }
        .font-display { font-family: 'Space Grotesk', sans-serif; }
        .font-mono-data { font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums; }
        @keyframes pulseDot { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
        .pulse { animation: pulseDot 1.8s ease-in-out infinite; }
      `}</style>

      {/* connection banner */}
      {connState === 'error' && (
        <div style={{ background: `${C.short}18`, borderColor: C.short, color: C.short }}
          className="border-b px-5 py-2 text-xs flex items-center gap-2 flex-wrap">
          <span>Can't reach the API at {apiBase} — {status.error}. Check that api.py is running there and the URL below is correct.</span>
        </div>
      )}

      {/* top bar */}
      <div style={{ borderColor: C.hairline }} className="border-b px-5 py-4 flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <StructureMark size={20} />
          <span className="font-display text-lg" style={{ letterSpacing: '0.02em' }}>
            KEHLO <span style={{ color: C.muted, fontWeight: 400 }}>TRADING</span>
          </span>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-1.5">
            <span style={{
              width: 7, height: 7, borderRadius: 999, display: 'inline-block',
              background: connState === 'ok' ? C.long : connState === 'connecting' ? C.amber : C.short,
            }} className={connState !== 'error' ? 'pulse' : ''} />
            <input value={apiBaseInput} onChange={(e) => setApiBaseInput(e.target.value)}
              onBlur={() => setApiBase(apiBaseInput.trim() || DEFAULT_API_BASE)}
              onKeyDown={(e) => e.key === 'Enter' && setApiBase(apiBaseInput.trim() || DEFAULT_API_BASE)}
              className="text-xs px-2 py-1 rounded font-mono-data outline-none w-44"
              style={{ background: C.panelAlt, border: `1px solid ${C.hairline}`, color: C.muted }} />
          </div>

          <ModeSwitcher apiBase={apiBase} status={status} credsQ={credsQ}
            onChanged={() => { status.refetch().catch(() => {}); }} />

          <div className="flex items-center gap-2 text-sm" style={{ color: C.muted }}>
            <span className={status.data?.bot_running ? 'pulse' : ''}
              style={{ width: 8, height: 8, borderRadius: 999, background: status.data?.bot_running ? C.long : C.short, display: 'inline-block' }} />
            {status.data?.bot_running ? 'Watching markets' : 'Stopped'}
          </div>

          <button onClick={handleBotButtonClick} disabled={botActionPending || connState === 'error'}
            className="px-4 py-1.5 rounded text-sm font-medium flex items-center gap-1.5 transition-colors disabled:opacity-50"
            style={{
              background: confirmingLiveStart ? C.short : status.data?.bot_running ? 'transparent' : C.amber,
              color: confirmingLiveStart ? '#fff' : status.data?.bot_running ? C.short : C.bg,
              border: `1px solid ${confirmingLiveStart ? C.short : status.data?.bot_running ? C.short : C.amber}`,
            }}>
            {status.data?.bot_running ? <Square size={13} /> : <Play size={13} />}
            {confirmingLiveStart ? 'Confirm — real money' : status.data?.bot_running ? 'Stop bot' : 'Start bot'}
          </button>
        </div>
      </div>
      {botActionError && <div className="px-5 pt-2 text-xs" style={{ color: C.short }}>{botActionError}</div>}

      {/* hero stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-px" style={{ background: C.hairline }}>
        {[
          { label: 'Open positions', value: status.data?.open_position_count ?? '···', color: C.paper },
          {
            label: 'Total P&L', color: (statsQ.data?.total_pnl ?? 0) >= 0 ? C.long : C.short,
            value: statsQ.data ? `${statsQ.data.total_pnl >= 0 ? '+' : ''}$${statsQ.data.total_pnl.toFixed(2)}` : '···',
          },
          { label: 'Win rate', value: statsQ.data ? `${statsQ.data.win_rate_pct.toFixed(0)}%` : '···', color: C.paper },
          { label: 'Closed trades', value: statsQ.data?.closed_trades ?? '···', color: C.paper },
        ].map((s) => (
          <div key={s.label} style={{ background: C.bg }} className="p-5">
            <div className="text-xs uppercase mb-2" style={{ color: C.muted, letterSpacing: '0.08em' }}>{s.label}</div>
            <div className="font-mono-data text-2xl" style={{ color: s.color }}>{s.value}</div>
          </div>
        ))}
      </div>

      {/* equity curve */}
      <div className="px-5 pt-5">
        <section style={{ background: C.panel, border: `1px solid ${C.hairline}` }} className="rounded-lg p-4">
          <h2 className="font-display text-sm mb-3" style={{ color: C.muted, letterSpacing: '0.06em' }}>EQUITY CURVE (closed trades)</h2>
          {equityCurve.length === 0 ? (
            <div className="text-sm py-6 text-center" style={{ color: C.muted }}>No closed trades yet.</div>
          ) : (
            <ResponsiveContainer width="100%" height={120}>
              <AreaChart data={equityCurve}>
                <defs>
                  <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={C.amber} stopOpacity={0.35} />
                    <stop offset="100%" stopColor={C.amber} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="t" hide />
                <YAxis hide domain={['dataMin - 10', 'dataMax + 10']} />
                <Tooltip contentStyle={{ background: C.panelAlt, border: `1px solid ${C.hairline}`, borderRadius: 6, fontSize: 12 }}
                  labelFormatter={() => ''} formatter={(v) => [`$${v.toFixed(2)}`, 'Cumulative P&L']} />
                <Area type="monotone" dataKey="equity" stroke={C.amber} strokeWidth={2} fill="url(#pnlGrad)" />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </section>
      </div>

      {/* tabs */}
      <div className="flex items-center gap-1 px-5 mt-5 border-b overflow-x-auto" style={{ borderColor: C.hairline }}>
        {TABS.map((tab) => (
          <button key={tab.id} onClick={() => setActiveTab(tab.id)}
            className="px-3 py-3 text-sm font-medium whitespace-nowrap flex items-center gap-1.5 transition-colors"
            style={{ color: activeTab === tab.id ? C.paper : C.muted, borderBottom: activeTab === tab.id ? `2px solid ${C.amber}` : '2px solid transparent' }}>
            {tab.label}
            {tab.count != null && <span className="text-xs">({tab.count})</span>}
            {tab.alert && <span style={{ width: 6, height: 6, borderRadius: 999, background: C.short, display: 'inline-block' }} />}
          </button>
        ))}
      </div>

      <div className="p-5">
        {activeTab === 'positions' && (
          openTrades.loading && !openTrades.data ? (
            <div className="text-sm" style={{ color: C.muted }}>Loading positions…</div>
          ) : (openTrades.data || []).length === 0 ? (
            <div style={{ background: C.panel, border: `1px dashed ${C.hairline}`, color: C.muted }} className="rounded-lg p-6 text-sm text-center">
              No open positions — the bot is watching for its next setup.
            </div>
          ) : (
            <div className="space-y-3 max-w-2xl">
              {openTrades.data.map((pos) => <PositionCard key={pos.id} pos={pos} />)}
            </div>
          )
        )}

        {activeTab === 'signals' && (
          <div style={{ background: C.panel, border: `1px solid ${C.hairline}` }} className="rounded-lg max-w-2xl">
            {signalsQ.loading && !signalsQ.data ? (
              <div className="p-4 text-sm" style={{ color: C.muted }}>Loading signals…</div>
            ) : (signalsQ.data || []).length === 0 ? (
              <div className="p-6 text-sm text-center" style={{ color: C.muted }}>No signals logged yet.</div>
            ) : signalsQ.data.map((s, i) => (
              <div key={s.id ?? i} style={{ borderColor: C.hairline }} className={i > 0 ? 'border-t' : ''}>
                <SignalRow s={s} />
              </div>
            ))}
          </div>
        )}

        {activeTab === 'history' && (
          <div style={{ background: C.panel, border: `1px solid ${C.hairline}` }} className="rounded-lg max-w-2xl">
            {history.loading && !history.data ? (
              <div className="p-4 text-sm" style={{ color: C.muted }}>Loading history…</div>
            ) : (history.data || []).length === 0 ? (
              <div className="p-6 text-sm text-center" style={{ color: C.muted }}>No closed trades yet.</div>
            ) : history.data.map((h, i) => (
              <div key={h.id ?? i} style={{ borderColor: C.hairline }} className={i > 0 ? 'border-t' : ''}>
                <HistoryRow h={h} />
              </div>
            ))}
          </div>
        )}

        {activeTab === 'logs' && (
          <div className="max-w-3xl">
            <div className="flex gap-2 mb-3">
              {['all', 'info', 'warning', 'error'].map((lvl) => (
                <button key={lvl} onClick={() => setLogFilter(lvl)}
                  className="px-3 py-1 rounded text-xs font-medium capitalize transition-colors"
                  style={{ background: logFilter === lvl ? C.amber : C.panelAlt, color: logFilter === lvl ? C.bg : C.muted }}>
                  {lvl}
                </button>
              ))}
            </div>
            <div style={{ background: C.panel, border: `1px solid ${C.hairline}` }} className="rounded-lg">
              {logsQ.loading && !logsQ.data ? (
                <div className="p-6 text-sm text-center" style={{ color: C.muted }}>Loading logs…</div>
              ) : (logsQ.data || []).length === 0 ? (
                <div className="p-6 text-sm text-center" style={{ color: C.muted }}>No {logFilter} logs.</div>
              ) : logsQ.data.map((log, i) => (
                <div key={log.id ?? i} style={{ borderColor: C.hairline }} className={i > 0 ? 'border-t' : ''}>
                  <LogRow log={log} />
                </div>
              ))}
            </div>
          </div>
        )}

        {activeTab === 'connect' && (
          <div className="max-w-3xl">
            <p className="text-sm mb-4" style={{ color: C.muted }}>
              Attach your Bitget API keys here instead of editing .env by hand. Demo and live need
              separate keys — Bitget doesn't let one work in the other mode. Both are encrypted
              before they're stored, and a PIN is required to save or to switch into live.
            </p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <CredentialForm mode="demo" apiBase={apiBase} credInfo={credsQ.data?.demo}
                onSaved={() => credsQ.refetch().catch(() => {})} />
              <CredentialForm mode="live" apiBase={apiBase} credInfo={credsQ.data?.live}
                onSaved={() => credsQ.refetch().catch(() => {})} />
            </div>
          </div>
        )}

        {activeTab === 'settings' && (
          <section style={{ background: C.panel, border: `1px solid ${C.hairline}` }} className="rounded-lg p-4 max-w-sm">
            <Field label="Risk per trade (%)" value={riskPct} onChange={setRiskPct} />
            <Field label="Max concurrent positions" value={maxPositions} onChange={setMaxPositions} step={1} />
            <Field label="Max daily loss (%)" value={maxDailyLoss} onChange={setMaxDailyLoss} />
            <div className="mb-3">
              <label className="text-xs block mb-1" style={{ color: C.muted }}>Timeframe</label>
              <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}
                className="w-full px-2 py-1.5 rounded text-sm outline-none"
                style={{ background: C.bg, border: `1px solid ${C.hairline}`, color: C.paper }}>
                {['1m', '5m', '15m', '30m', '1H', '4H', '1D'].map((tf) => <option key={tf} value={tf}>{tf}</option>)}
              </select>
            </div>
            <div className="mb-3">
              <label className="text-xs block mb-1" style={{ color: C.muted }}>Symbols (comma separated)</label>
              <input type="text" value={symbolsText} onChange={(e) => setSymbolsText(e.target.value)}
                className="w-full px-2 py-1.5 rounded text-sm outline-none"
                style={{ background: C.bg, border: `1px solid ${C.hairline}`, color: C.paper }} />
            </div>
            <button onClick={handleSaveSettings} disabled={saveState === 'saving'}
              className="w-full py-2 rounded text-sm font-medium mt-1 transition-colors disabled:opacity-60"
              style={{ background: saveState === 'saved' ? C.long : C.amber, color: C.bg }}>
              {saveState === 'saving' ? 'Saving…' : saveState === 'saved' ? 'Saved' : 'Save settings'}
            </button>
            {saveState === 'error' && <div className="text-xs mt-2" style={{ color: C.short }}>{saveError}</div>}
            <p className="text-xs mt-3" style={{ color: C.muted }}>
              Demo/live mode isn't changeable here on purpose — it's controlled only by BITGET_DEMO in the server's .env file, so a stray click can never turn on real-money trading.
            </p>
          </section>
        )}
      </div>

      <div className="px-5 pb-6 text-xs" style={{ color: C.muted }}>
        Live-wired to your FastAPI backend at the address above — point it at your deployed server once it's running there.
      </div>
    </div>
  );
}
