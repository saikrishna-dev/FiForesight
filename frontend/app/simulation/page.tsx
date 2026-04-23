"use client";

import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import {
  Box, Container, Typography, Button,
  Grid, CircularProgress, ThemeProvider, createTheme,
  CssBaseline, Paper, Chip, Stack, IconButton, TextField,
  Avatar, LinearProgress, Divider, Alert,
  Table, TableHead, TableBody, TableRow, TableCell,
} from '@mui/material';
import {
  ArrowLeft, Trash2, BarChart2, Trophy, RefreshCw,
  Target, Brain,
} from 'lucide-react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, Legend,
} from 'recharts';

// ── Theme ─────────────────────────────────────────────────────────────────────

const darkTheme = createTheme({
  palette: {
    mode: 'dark',
    primary:    { main: '#00f2ff' },
    secondary:  { main: '#7c4dff' },
    background: { default: '#070b14', paper: '#0d1117' },
    success:    { main: '#10b981' },
    error:      { main: '#ef4444' },
    warning:    { main: '#f59e0b' },
  },
  typography: { fontFamily: '"Inter", "Roboto", sans-serif' },
  components: {
    MuiCssBaseline: {
      styleOverrides: `
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
      `,
    },
  },
});

// ── Types ──────────────────────────────────────────────────────────────────────

type Phase = 'welcome' | 'chat' | 'analyzing' | 'portfolio' | 'race';
type RiskLevel = 'conservative' | 'moderate' | 'aggressive';

interface StockSuggestion {
  symbol:         string;
  name:           string;
  sector:         string;
  currentPrice:   number;
  forecastHigh:   number;
  forecastLow:    number;
  forecastConf:   string;
  direction:      string;
  rsi:            number;
  juryRating:     string;
  juryNote:       string;
  juryConfidence: number;
  allocationPct:  number;
  allocationUsd:  number;
  shares:         number;
}

interface BenchmarkInfo {
  symbol:        string;
  name:          string;
  currentPrice:  number;
  allocationUsd: number;
  shares:        number;
}

interface Holding {
  symbol:        string;
  name:          string;
  shares:        number;
  buyPrice:      number;
  allocationUsd: number;
}

interface SimulationState {
  id:          string;
  startDate:   string;
  budget:      number;
  riskLevel:   string;
  sectors:     string[];
  holdings:    Holding[];
  spyBuyPrice: number;
  spyShares:   number;
}

interface HoldingPnl {
  symbol:       string;
  name:         string;
  shares:       number;
  buyPrice:     number;
  currentPrice: number;
  value:        number;
  pnl:          number;
  pnlPct:       number;
}

interface PerformanceData {
  dates:                 string[];
  portfolioReturns:      number[];
  spyReturns:            number[];
  currentPortfolioValue: number;
  currentSpyValue:       number;
  portfolioReturn:       number;
  spyReturn:             number;
  holdings:              HoldingPnl[];
}

// ── Constants ──────────────────────────────────────────────────────────────────

const BUDGET = 100_000;

const SECTORS = [
  'Technology', 'Healthcare', 'Finance',
  'Energy', 'Consumer', 'Industrial',
];

const RISK_OPTIONS: { key: RiskLevel; label: string; description: string; color: string }[] = [
  { key: 'conservative', label: 'Conservative', description: 'Stable blue-chips, lower volatility', color: '#10b981' },
  { key: 'moderate',     label: 'Moderate',     description: 'Balanced growth and stability',       color: '#00f2ff' },
  { key: 'aggressive',   label: 'Aggressive',   description: 'High growth, higher risk',            color: '#f59e0b' },
];

const ANALYZING_MSGS = [
  'Scanning candidate stocks from your sectors...',
  'Running ensemble ML forecasts (Prophet + SARIMA + RandomForest)...',
  'Consulting the Analyst Jury...',
  'Scoring and ranking by your risk profile...',
  'Building your personalized portfolio...',
];

// Legacy localStorage key — cleared on mount during migration to DB storage
const LEGACY_STORAGE_KEY = 'fiforesight_simulation';

// Environment is baked in at build time via NEXT_PUBLIC_APP_ENV.
// local = dev machine; preview + live share the same simulation pool.
const SIM_ENV: string = (() => {
  const e = process.env.NEXT_PUBLIC_APP_ENV;
  return (e === 'live' || e === 'preview') ? 'live' : 'local';
})();

// ── Helpers ────────────────────────────────────────────────────────────────────

function ratingColor(r: string): string {
  if (r.toLowerCase().includes('strong buy'))  return '#10b981';
  if (r.toLowerCase() === 'buy')               return '#00f2ff';
  if (r.toLowerCase() === 'hold')              return '#f59e0b';
  return '#ef4444';
}

function confColor(c: string): string {
  return c === 'high' ? '#10b981' : c === 'medium' ? '#f59e0b' : '#94a3b8';
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function ChatBubble({ bot, children }: { bot?: boolean; children: React.ReactNode }) {
  return (
    <Box sx={{ display: 'flex', justifyContent: bot ? 'flex-start' : 'flex-end' }}>
      {bot && (
        <Avatar sx={{ width: 32, height: 32, mr: 1.5, bgcolor: 'primary.main', fontSize: 13, flexShrink: 0 }}>
          AI
        </Avatar>
      )}
      <Paper sx={{
        p: 1.5, maxWidth: '82%',
        background: bot ? 'rgba(0,242,255,0.06)' : 'rgba(124,77,255,0.14)',
        border: `1px solid ${bot ? 'rgba(0,242,255,0.14)' : 'rgba(124,77,255,0.24)'}`,
        borderRadius: bot ? '4px 12px 12px 12px' : '12px 4px 12px 12px',
      }}>
        {children}
      </Paper>
    </Box>
  );
}

function StatCard({ label, value, sub, color }: { label: string; value: string; sub: string; color: string }) {
  return (
    <Paper sx={{ p: 2, border: '1px solid rgba(255,255,255,0.08)', textAlign: 'center', height: '100%' }}>
      <Typography variant="caption" color="text.secondary" display="block">{label}</Typography>
      <Typography variant="h5" fontWeight={800} sx={{ color, my: 0.5 }}>{value}</Typography>
      <Typography variant="caption" color="text.secondary">{sub}</Typography>
    </Paper>
  );
}

function SuggestionCard({
  stock, shares, onRemove, onSharesChange,
}: {
  stock: StockSuggestion;
  shares: number;
  onRemove: () => void;
  onSharesChange: (n: number) => void;
}) {
  const forecastRef = stock.direction === 'Bullish' ? stock.forecastHigh : stock.forecastLow;
  const upside      = stock.currentPrice > 0 ? ((forecastRef - stock.currentPrice) / stock.currentPrice * 100) : 0;
  const allocUsd   = shares * stock.currentPrice;
  return (
    <Paper sx={{
      p: 2, height: '100%', position: 'relative',
      border: '1px solid rgba(255,255,255,0.08)',
      '&:hover': { border: '1px solid rgba(0,242,255,0.22)' },
      transition: 'border 0.2s',
    }}>
      <IconButton size="small" onClick={onRemove} sx={{
        position: 'absolute', top: 6, right: 6,
        color: 'rgba(255,255,255,0.25)',
        '&:hover': { color: '#ef4444' },
      }}>
        <Trash2 size={14} />
      </IconButton>

      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h6" fontWeight={800}>{stock.symbol}</Typography>
        <Chip label={stock.sector} size="small" sx={{
          fontSize: 10, height: 18,
          bgcolor: 'rgba(255,255,255,0.05)', color: 'text.secondary',
        }} />
      </Stack>

      <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1.5, pr: 3 }}>
        {stock.name}
      </Typography>

      <Stack direction="row" justifyContent="space-between" sx={{ mb: 1.5 }}>
        <Box>
          <Typography variant="caption" color="text.secondary">Price</Typography>
          <Typography fontWeight={700}>${stock.currentPrice.toFixed(2)}</Typography>
        </Box>
        <Box textAlign="right">
          <Typography variant="caption" color="text.secondary">5d Forecast</Typography>
          <Typography fontWeight={700} color={stock.direction === 'Bullish' ? '#10b981' : '#ef4444'}>
            {stock.direction === 'Bullish' ? '▲' : '▼'} {upside.toFixed(1)}%
          </Typography>
        </Box>
      </Stack>

      <Stack direction="row" spacing={0.75} sx={{ mb: 1.5, flexWrap: 'wrap', gap: 0.5 }}>
        <Chip label={stock.juryRating} size="small" sx={{
          bgcolor: `${ratingColor(stock.juryRating)}20`,
          color: ratingColor(stock.juryRating), fontWeight: 700, fontSize: 10,
        }} />
        <Chip label={`${stock.forecastConf} conf`} size="small" sx={{
          bgcolor: 'rgba(255,255,255,0.04)',
          color: confColor(stock.forecastConf), fontSize: 10,
        }} />
        <Chip label={`RSI ${stock.rsi}`} size="small" sx={{
          bgcolor: 'rgba(255,255,255,0.04)',
          color: stock.rsi > 70 ? '#ef4444' : stock.rsi < 30 ? '#10b981' : '#94a3b8',
          fontSize: 10,
        }} />
      </Stack>

      {stock.juryNote && (
        <Typography variant="caption" color="text.secondary" sx={{
          display: '-webkit-box', WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical', overflow: 'hidden',
          lineHeight: 1.5, mb: 1.5, fontStyle: 'italic',
        }}>
          &ldquo;{stock.juryNote}&rdquo;
        </Typography>
      )}

      <Divider sx={{ my: 1, borderColor: 'rgba(255,255,255,0.06)' }} />

      <Stack direction="row" justifyContent="space-between" alignItems="flex-end">
        <Box>
          <Typography variant="caption" color="text.secondary">Allocation</Typography>
          <Typography variant="subtitle2" fontWeight={700} color="primary.main">
            ${allocUsd.toLocaleString('en-US', { maximumFractionDigits: 0 })}
          </Typography>
        </Box>
        <Box textAlign="right">
          <Typography variant="caption" color="text.secondary" display="block">Shares</Typography>
          <Stack direction="row" alignItems="center" spacing={0.25}>
            <IconButton
              size="small"
              onClick={() => onSharesChange(Math.max(1, shares - 1))}
              sx={{ p: 0.25, color: 'text.secondary', '&:hover': { color: 'primary.main' } }}
            >
              <Typography sx={{ fontSize: 16, lineHeight: 1, userSelect: 'none' }}>−</Typography>
            </IconButton>
            <TextField
              size="small"
              type="number"
              value={shares}
              onChange={e => {
                const v = parseInt(e.target.value, 10);
                if (!isNaN(v) && v > 0) onSharesChange(v);
              }}
              inputProps={{ min: 1, style: { textAlign: 'center', padding: '2px 4px', width: 54, fontSize: 13 } }}
              sx={{ '& .MuiOutlinedInput-root': { height: 28 } }}
            />
            <IconButton
              size="small"
              onClick={() => onSharesChange(shares + 1)}
              sx={{ p: 0.25, color: 'text.secondary', '&:hover': { color: 'primary.main' } }}
            >
              <Typography sx={{ fontSize: 16, lineHeight: 1, userSelect: 'none' }}>+</Typography>
            </IconButton>
          </Stack>
        </Box>
      </Stack>
    </Paper>
  );
}

// ── Main Page ──────────────────────────────────────────────────────────────────

export default function SimulationPage() {
  const [phase, setPhase]                   = useState<Phase>('welcome');
  const [riskLevel, setRiskLevel]           = useState<RiskLevel>('moderate');
  const [selectedSectors, setSelectedSectors] = useState<string[]>([]);
  const [chatStep, setChatStep]             = useState(0);
  const [analysisMsg, setAnalysisMsg]       = useState(0);
  const [suggestions, setSuggestions]       = useState<StockSuggestion[]>([]);
  const [benchmark, setBenchmark]           = useState<BenchmarkInfo | null>(null);
  const [removed, setRemoved]               = useState<Set<string>>(new Set());
  const [addTicker, setAddTicker]           = useState('');
  const [addingTicker, setAddingTicker]     = useState(false);
  const [simulation, setSimulation]         = useState<SimulationState | null>(null);
  const [perfData, setPerfData]             = useState<PerformanceData | null>(null);
  const [perfLoading, setPerfLoading]       = useState(false);
  const [error, setError]                   = useState('');
  const [customShares, setCustomShares]     = useState<Record<string, number>>({});
  const [chartInterval, setChartInterval]   = useState<'1m' | '5m' | '1h' | '1d'>('1h');
  const [savedSims, setSavedSims]           = useState<SimulationState[]>([]);
  const [simsLoading, setSimsLoading]       = useState(false);
  const msgTimer     = useRef<ReturnType<typeof setInterval> | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  // Per-sim-id pending save promise, so resets can await in-flight saves
  // before deleting (prevents the save from "resurrecting" a deleted sim).
  const pendingSaves = useRef<Map<string, Promise<unknown>>>(new Map());
  // Ids the user has explicitly deleted — late-resolving saves check this
  // before re-adding to savedSims.
  const deletedSims  = useRef<Set<string>>(new Set());

  // On mount: migrate any legacy localStorage save to the DB, then load sims.
  useEffect(() => {
    void migrateLegacyAndLoad();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function migrateLegacyAndLoad() {
    let legacyRaw: string | null = null;
    try { legacyRaw = localStorage.getItem(LEGACY_STORAGE_KEY); } catch { /* noop */ }

    if (legacyRaw) {
      try {
        const legacy = JSON.parse(legacyRaw) as SimulationState;
        if (legacy && typeof legacy.id === 'string') {
          await axios.post('/api/simulation/state', {
            sim_id: legacy.id,
            env:    SIM_ENV,
            state:  legacy,
          });
          // Only clear once the DB write succeeded.
          try { localStorage.removeItem(LEGACY_STORAGE_KEY); } catch { /* noop */ }
        }
      } catch (e) {
        // Keep the legacy value — user can still recover on a future load.
        console.warn('[Simulation] legacy save migration failed; retaining localStorage copy:', e);
      }
    }
    await loadSavedSims();
  }

  async function loadSavedSims() {
    setSimsLoading(true);
    try {
      const { data } = await axios.get<{ simulations: SimulationState[] }>(
        `/api/simulation/state?env=${SIM_ENV}`
      );
      setSavedSims(data.simulations ?? []);
    } catch {
      setSavedSims([]);
    } finally {
      setSimsLoading(false);
    }
  }

  // Cycle analysis progress messages
  useEffect(() => {
    if (phase !== 'analyzing') return;
    msgTimer.current = setInterval(() => setAnalysisMsg(i => (i + 1) % ANALYZING_MSGS.length), 2200);
    return () => { if (msgTimer.current) clearInterval(msgTimer.current); };
  }, [phase]);

  // Fetch performance when entering race phase or interval changes
  useEffect(() => {
    if (phase === 'race' && simulation) fetchPerf(simulation, chartInterval);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, chartInterval]);

  // Auto-refresh every 60s for intraday intervals
  useEffect(() => {
    if (refreshTimer.current) clearInterval(refreshTimer.current);
    if (phase !== 'race' || !simulation || chartInterval === '1d') return;
    refreshTimer.current = setInterval(() => fetchPerf(simulation, chartInterval), 60_000);
    return () => { if (refreshTimer.current) clearInterval(refreshTimer.current); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, simulation, chartInterval]);

  async function fetchPerf(sim: SimulationState, iv = chartInterval) {
    setPerfLoading(true);
    try {
      const activeHoldings = sim.holdings.filter(h => h.shares > 0);
      if (!activeHoldings.length) return;
      if (!(sim.spyBuyPrice > 0)) {
        // Refuse to manufacture a fake benchmark basis — ask the user to restart.
        setPerfData(null);
        setError('Benchmark price unavailable. Please reset and restart the race.');
        return;
      }
      const { data } = await axios.post('/api/simulation/performance', {
        holdings:      activeHoldings,
        start_date:    sim.startDate,
        spy_buy_price: sim.spyBuyPrice,
        budget:        sim.budget,
        interval:      iv,
      });
      setPerfData(data);
    } catch (e) { console.error('[SimPerf] performance fetch failed:', e); }
    finally     { setPerfLoading(false); }
  }

  async function handleAnalyze() {
    setPhase('analyzing');
    setAnalysisMsg(0);
    setError('');
    try {
      const { data } = await axios.post('/api/simulation/suggest', {
        sectors:    selectedSectors,
        risk_level: riskLevel,
        budget:     BUDGET,
      });
      if (data.error && !(data.suggestions?.length)) {
        setError(data.error);
        setPhase('chat');
        return;
      }
      setSuggestions(data.suggestions ?? []);
      setBenchmark(data.benchmark ?? null);
      setRemoved(new Set());
      setCustomShares({});
      setPhase('portfolio');
    } catch (e: unknown) {
      const err = e as { response?: { status?: number; data?: { detail?: string; error?: string } } };
      const status = err?.response?.status;
      const detail = err?.response?.data?.detail ?? err?.response?.data?.error;
      const msg = status === 503
        ? 'Backend server is not running. Start it with: pnpm run app:dev'
        : detail ?? 'Analysis failed. Please try again.';
      setError(msg);
      setPhase('chat');
    }
  }

  function handleStartRace() {
    const active = suggestions.filter(s => !removed.has(s.symbol));
    if (!active.length) return;
    const sim: SimulationState = {
      id:          crypto.randomUUID(),
      startDate:   new Date().toISOString(),
      budget:      BUDGET,
      riskLevel,
      sectors:     selectedSectors,
      holdings:    active.map(s => {
        const sh = effectiveShares(s);
        return {
          symbol:        s.symbol,
          name:          s.name,
          shares:        sh,
          buyPrice:      s.currentPrice,
          allocationUsd: sh * s.currentPrice,
        };
      }),
      spyBuyPrice: benchmark?.currentPrice ?? 0,
      spyShares:   benchmark && benchmark.currentPrice > 0 ? Math.floor(totalInvested / benchmark.currentPrice) : 0,
    };
    // Persist to DB (non-blocking — simulation starts regardless). Track the
    // in-flight promise so a subsequent reset can await it before deleting.
    const savePromise = axios
      .post('/api/simulation/state', { sim_id: sim.id, env: SIM_ENV, state: sim })
      .then(() => {
        // Guard: if the user has already reset this sim, don't resurrect it.
        if (deletedSims.current.has(sim.id)) return;
        setSavedSims(prev => [sim, ...prev.filter(s => s.id !== sim.id)]);
      })
      .catch(() => { /* silent — user still gets the sim */ })
      .finally(() => { pendingSaves.current.delete(sim.id); });
    pendingSaves.current.set(sim.id, savePromise);
    setSimulation(sim);
    setPhase('race');
  }

  function handleReset() {
    if (simulation) {
      const simId = simulation.id;
      // Mark deleted synchronously so an in-flight save can't re-add it.
      deletedSims.current.add(simId);
      const pending = pendingSaves.current.get(simId) ?? Promise.resolve();
      void pending.then(() =>
        axios.delete(`/api/simulation/state/${simId}?env=${SIM_ENV}`)
          .then(() => setSavedSims(prev => prev.filter(s => s.id !== simId)))
          .catch(() => { /* silent */ })
      );
    }
    setSimulation(null);
    setPerfData(null);
    setSuggestions([]);
    setBenchmark(null);
    setCustomShares({});
    setRiskLevel('moderate');
    setSelectedSectors([]);
    setChatStep(0);
    setPhase('welcome');
  }

  async function handleAddTicker() {
    const sym = addTicker.trim().toUpperCase();
    if (!sym || suggestions.some(s => s.symbol === sym)) return;
    setAddingTicker(true);
    setError('');
    try {
      const { data } = await axios.post('/api/predict', { data: sym });
      const price     = parseFloat(data.currentPrice ?? '0');
      const activeList = suggestions.filter(s => !removed.has(s.symbol));
      const spent      = activeList.reduce((s, h) => s + (customShares[h.symbol] ?? h.shares) * h.currentPrice, 0);
      const remaining  = Math.max(0, BUDGET - spent);
      const shares     = price > 0 ? Math.floor(remaining / price) : 0;
      setSuggestions(prev => [...prev, {
        symbol:         sym,
        name:           sym,
        sector:         data.metrics?.sector ?? 'Unknown',
        currentPrice:   price,
        forecastHigh:   parseFloat(data.prediction?.highRange ?? String(price)),
        forecastLow:    parseFloat(data.prediction?.lowRange  ?? String(price)),
        forecastConf:   data.confidence ?? 'medium',
        direction:      data.prediction?.trend ?? 'Bullish',
        rsi:            parseFloat(data.rsi ?? '50'),
        juryRating:     data.juryAnalysts?.[0]?.rating ?? 'Hold',
        juryNote:       data.analystNote ?? '',
        juryConfidence: data.juryAnalysts?.[0]?.confidence ?? 50,
        allocationPct:  BUDGET > 0 ? Math.round((shares * price) / BUDGET * 100) : 0,
        allocationUsd:  parseFloat((shares * price).toFixed(2)),
        shares,
      }]);
      setAddTicker('');
    } catch {
      setError(`Could not fetch data for ${sym}. Check the ticker symbol.`);
    } finally {
      setAddingTicker(false);
    }
  }

  const active        = suggestions.filter(s => !removed.has(s.symbol));
  const effectiveShares = (s: StockSuggestion) => customShares[s.symbol] ?? s.shares;
  const totalInvested = active.reduce((sum, s) => sum + effectiveShares(s) * s.currentPrice, 0);

  // ── Screens ────────────────────────────────────────────────────────────────

  function Welcome() {
    return (
      <Box sx={{ textAlign: 'center', py: 8 }}>

        {/* Saved simulations list — env-scoped, DB-backed */}
        {simsLoading && (
          <LinearProgress sx={{ mb: 3, borderRadius: 1 }} />
        )}
        {!simsLoading && savedSims.length > 0 && (
          <Paper sx={{
            mb: 4, p: 2, border: '1px solid rgba(0,242,255,0.2)',
            background: 'rgba(0,242,255,0.04)', textAlign: 'left',
          }}>
            <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.5 }}>
              <Typography variant="subtitle2" fontWeight={700} color="primary.main">
                Saved simulations ({SIM_ENV})
              </Typography>
              <Chip label={SIM_ENV} size="small" sx={{
                background: SIM_ENV === 'live' ? 'rgba(16,185,129,0.15)' : 'rgba(0,242,255,0.12)',
                color: SIM_ENV === 'live' ? '#10b981' : '#00f2ff',
                fontSize: 11, fontWeight: 700,
              }} />
            </Stack>
            <Stack spacing={1}>
              {savedSims.map(sim => (
                <Stack key={sim.id} direction="row" alignItems="center"
                  justifyContent="space-between" flexWrap="wrap" gap={1}
                  sx={{ p: 1.2, borderRadius: 1, border: '1px solid rgba(255,255,255,0.06)', background: 'rgba(255,255,255,0.02)' }}
                >
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Typography variant="body2" fontWeight={600} noWrap>
                      {sim.riskLevel.charAt(0).toUpperCase() + sim.riskLevel.slice(1)} · {sim.sectors.join(', ')}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      Started {new Date(sim.startDate).toLocaleString()} · ${sim.budget.toLocaleString()} budget
                    </Typography>
                  </Box>
                  <Stack direction="row" spacing={0.5}>
                    <Button size="small" variant="outlined"
                      onClick={() => { setSimulation(sim); setPhase('race'); }}
                      sx={{ borderColor: 'primary.main', color: 'primary.main', fontWeight: 700, fontSize: 11 }}
                    >
                      Resume
                    </Button>
                    <Button size="small" variant="text" color="error" sx={{ fontSize: 11 }}
                      onClick={() => {
                        deletedSims.current.add(sim.id);
                        const pending = pendingSaves.current.get(sim.id) ?? Promise.resolve();
                        void pending.then(() =>
                          axios.delete(`/api/simulation/state/${sim.id}?env=${SIM_ENV}`).catch(() => {})
                        );
                        setSavedSims(prev => prev.filter(s => s.id !== sim.id));
                      }}
                    >
                      Delete
                    </Button>
                  </Stack>
                </Stack>
              ))}
            </Stack>
          </Paper>
        )}
        <Typography variant="h3" sx={{
          fontWeight: 800, mb: 1.5,
          background: 'linear-gradient(90deg,#00f2ff,#7c4dff)',
          WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent',
        }}>
          Portfolio Race
        </Typography>
        <Typography variant="h6" color="text.secondary" sx={{ mb: 1 }}>
          AI-Powered Portfolio vs S&amp;P 500
        </Typography>
        <Typography color="text.secondary" sx={{ mb: 5, maxWidth: 480, mx: 'auto', lineHeight: 1.7 }}>
          Our AI picks stocks using ensemble ML models and a 3-analyst jury, then races
          your portfolio against the S&amp;P 500 — starting the moment you hit Go.
        </Typography>

        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} justifyContent="center" sx={{ mb: 6 }}>
          {[
            { icon: <Brain size={22} />,    label: 'ML Forecasts',   sub: 'Prophet + SARIMA + RF' },
            { icon: <Target size={22} />,   label: 'Analyst Jury',   sub: '3 AI analysts debate'  },
            { icon: <Trophy size={22} />,   label: 'Beat the Market', sub: 'Race vs S&P 500'       },
          ].map(c => (
            <Paper key={c.label} sx={{
              p: 2.5, textAlign: 'center', minWidth: 140, flex: '1 1 140px',
              border: '1px solid rgba(255,255,255,0.08)',
            }}>
              <Box sx={{ color: 'primary.main', mb: 1 }}>{c.icon}</Box>
              <Typography variant="subtitle2" fontWeight={700}>{c.label}</Typography>
              <Typography variant="caption" color="text.secondary">{c.sub}</Typography>
            </Paper>
          ))}
        </Stack>

        <Button
          variant="contained" size="large"
          onClick={() => { setPhase('chat'); setChatStep(0); }}
          sx={{
            px: 5, py: 1.5, fontSize: '1.05rem', fontWeight: 700,
            background: 'linear-gradient(90deg,rgba(0,242,255,0.15),rgba(124,77,255,0.15))',
            border: '1px solid rgba(0,242,255,0.5)',
            '&:hover': { background: 'linear-gradient(90deg,rgba(0,242,255,0.28),rgba(124,77,255,0.28))' },
          }}
        >
          Build My Portfolio →
        </Button>
      </Box>
    );
  }

  function Chat() {
    return (
      <Box sx={{ maxWidth: 600, mx: 'auto', py: 4 }}>
        <Stack spacing={2.5}>
          <ChatBubble bot>
            <Typography variant="body2">
              Hey! I&apos;m your AI portfolio advisor. Let&apos;s build a portfolio to race the S&amp;P 500.
              First — what&apos;s your <strong>risk tolerance</strong>?
            </Typography>
          </ChatBubble>

          <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap>
            {RISK_OPTIONS.map(opt => (
              <Paper
                key={opt.key}
                onClick={() => { setRiskLevel(opt.key); if (chatStep < 1) setChatStep(1); }}
                sx={{
                  p: 2, cursor: 'pointer', flex: '1 1 130px', textAlign: 'center',
                  border: riskLevel === opt.key ? `2px solid ${opt.color}` : '2px solid rgba(255,255,255,0.07)',
                  background: riskLevel === opt.key ? `${opt.color}18` : 'transparent',
                  transition: 'all 0.18s',
                  '&:hover': { borderColor: opt.color, background: `${opt.color}10` },
                }}
              >
                <Typography variant="subtitle2" fontWeight={700} sx={{ color: opt.color }}>
                  {opt.label}
                </Typography>
                <Typography variant="caption" color="text.secondary">{opt.description}</Typography>
              </Paper>
            ))}
          </Stack>

          {chatStep >= 1 && (
            <>
              <ChatBubble>{RISK_OPTIONS.find(r => r.key === riskLevel)!.label}</ChatBubble>
              <ChatBubble bot>
                <Typography variant="body2">
                  Which <strong>sectors</strong> interest you? Pick 2–3.
                </Typography>
              </ChatBubble>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
                {SECTORS.map(s => (
                  <Chip
                    key={s} label={s}
                    onClick={() => {
                      setSelectedSectors(prev =>
                        prev.includes(s) ? prev.filter(x => x !== s) : [...prev, s]
                      );
                      if (chatStep < 2) setChatStep(2);
                    }}
                    variant="outlined"
                    sx={{
                      cursor: 'pointer',
                      borderColor:  selectedSectors.includes(s) ? 'primary.main' : 'rgba(255,255,255,0.14)',
                      background:   selectedSectors.includes(s) ? 'rgba(0,242,255,0.1)' : 'transparent',
                      color:        selectedSectors.includes(s) ? 'primary.main' : 'text.secondary',
                      fontWeight:   selectedSectors.includes(s) ? 700 : 400,
                    }}
                  />
                ))}
              </Box>
            </>
          )}

          {chatStep >= 2 && selectedSectors.length > 0 && (
            <>
              <ChatBubble>{selectedSectors.join(', ')}</ChatBubble>
              <ChatBubble bot>
                <Typography variant="body2">
                  You&apos;ll start with <strong>$100,000</strong> in practice money.
                  I&apos;ll analyze stocks using ML models and 3 AI analysts. Ready?
                </Typography>
              </ChatBubble>
              {error && <Alert severity="error" onClose={() => setError('')}>{error}</Alert>}
              <Button
                variant="contained" size="large"
                onClick={handleAnalyze}
                sx={{
                  alignSelf: 'flex-start', fontWeight: 700,
                  background: 'linear-gradient(90deg,rgba(0,242,255,0.15),rgba(124,77,255,0.15))',
                  border: '1px solid rgba(0,242,255,0.5)',
                }}
              >
                Analyze Stocks →
              </Button>
            </>
          )}
        </Stack>
      </Box>
    );
  }

  function Analyzing() {
    return (
      <Box sx={{ textAlign: 'center', py: 10 }}>
        <CircularProgress size={52} sx={{ color: 'primary.main', mb: 3 }} />
        <Typography variant="h6" sx={{ mb: 1, color: 'primary.main' }}>
          Analyzing Your Portfolio
        </Typography>
        <Typography color="text.secondary" sx={{ minHeight: 26 }}>
          {ANALYZING_MSGS[analysisMsg]}
        </Typography>
        <LinearProgress sx={{
          mt: 4, maxWidth: 340, mx: 'auto', borderRadius: 4,
          bgcolor: 'rgba(255,255,255,0.05)',
          '& .MuiLinearProgress-bar': { background: 'linear-gradient(90deg,#00f2ff,#7c4dff)' },
        }} />
      </Box>
    );
  }

  function Portfolio() {
    return (
      <Box sx={{ py: 3 }}>
        <Typography variant="h5" fontWeight={800} sx={{ mb: 0.5 }}>
          AI-Suggested Portfolio
        </Typography>
        <Typography color="text.secondary" sx={{ mb: 3 }}>
          {RISK_OPTIONS.find(r => r.key === riskLevel)?.label} risk ·{' '}
          {selectedSectors.join(', ')} · $100k budget.
          Remove stocks you don&apos;t want or add your own.
        </Typography>

        <Grid container spacing={2} sx={{ mb: 3 }}>
          {active.map(s => (
            <Grid size={{ xs: 12, sm: 6, md: 4 }} key={s.symbol}>
              <SuggestionCard
                stock={s}
                shares={effectiveShares(s)}
                onRemove={() => setRemoved(prev => new Set([...prev, s.symbol]))}
                onSharesChange={n => setCustomShares(prev => ({ ...prev, [s.symbol]: n }))}
              />
            </Grid>
          ))}
        </Grid>

        {/* Add custom ticker */}
        <Paper sx={{ p: 2, mb: 3, border: '1px solid rgba(255,255,255,0.07)' }}>
          <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1.5 }}>
            + Add Your Own Stock
          </Typography>
          <Stack direction="row" spacing={1}>
            <TextField
              size="small" placeholder="e.g. AMZN" value={addTicker}
              onChange={e => setAddTicker(e.target.value.toUpperCase())}
              onKeyDown={e => e.key === 'Enter' && handleAddTicker()}
              sx={{ flex: 1 }}
            />
            <Button
              variant="outlined" onClick={handleAddTicker}
              disabled={!addTicker.trim() || addingTicker}
              sx={{ borderColor: 'primary.main', color: 'primary.main', minWidth: 72 }}
            >
              {addingTicker ? <CircularProgress size={16} /> : 'Add'}
            </Button>
          </Stack>
          {error && <Alert severity="error" sx={{ mt: 1 }} onClose={() => setError('')}>{error}</Alert>}
        </Paper>

        {/* Allocation summary */}
        <Paper sx={{
          p: 2.5, mb: 3,
          border: '1px solid rgba(0,242,255,0.18)',
          background: 'rgba(0,242,255,0.03)',
        }}>
          <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" gap={2}>
            <Box>
              <Typography variant="caption" color="text.secondary">Portfolio</Typography>
              <Typography variant="h5" fontWeight={800} color="primary.main">
                ${totalInvested.toLocaleString('en-US', { maximumFractionDigits: 0 })}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {active.length} stocks selected
              </Typography>
            </Box>
            {benchmark && benchmark.currentPrice > 0 && (
              <Box textAlign={{ xs: 'left', sm: 'right' }}>
                <Typography variant="caption" color="text.secondary">Benchmark (SPY)</Typography>
                <Typography variant="h6" fontWeight={700} color="text.secondary">
                  {Math.floor(totalInvested / benchmark.currentPrice)} shares
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  ${totalInvested.toLocaleString('en-US', { maximumFractionDigits: 0 })} @ ${benchmark.currentPrice.toFixed(2)}/share
                </Typography>
              </Box>
            )}
          </Stack>
        </Paper>

        <Stack direction="row" spacing={2}>
          <Button
            variant="outlined" onClick={() => setPhase('chat')}
            sx={{ borderColor: 'rgba(255,255,255,0.18)', color: 'text.secondary' }}
          >
            ← Back
          </Button>
          <Button
            variant="contained" size="large"
            disabled={active.length === 0}
            onClick={handleStartRace}
            sx={{
              fontWeight: 700, flex: 1, maxWidth: 260,
              background: 'linear-gradient(90deg,#00f2ff,#7c4dff)',
              '&:hover': { background: 'linear-gradient(90deg,#00b8c2,#5e35b1)' },
            }}
          >
            🏁 Start the Race
          </Button>
        </Stack>
      </Box>
    );
  }

  function Race() {
    const sim         = simulation!;
    const portReturn  = perfData?.portfolioReturn ?? 0;
    const spyReturn   = perfData?.spyReturn ?? 0;
    const diff        = portReturn - spyReturn;
    const isWinning   = diff >= 0;
    const daysSince   = Math.floor((Date.now() - new Date(sim.startDate).getTime()) / 86_400_000);
    const initialCost = sim.holdings.reduce((s, h) => s + h.shares * h.buyPrice, 0);

    const isIntraday  = chartInterval !== '1d';
    const dates       = perfData?.dates ?? [];
    const spansDays   = dates.length > 1 && dates[0]?.slice(0, 10) !== dates[dates.length - 1]?.slice(0, 10);

    const fmtLabel = (d: string) => {
      if (!isIntraday) return d.slice(5);               // "MM-DD"
      const [datePart, timePart] = d.split(' ');
      const time = timePart?.slice(0, 5) ?? '';         // "HH:MM"
      return spansDays ? `${datePart.slice(5)} ${time}` : time;
    };

    const chartData = dates.reduce<{ date: string; full: string; portfolio: number; spy: number }[]>((acc, d, i) => {
      const p = perfData!.portfolioReturns[i];
      const s = perfData!.spyReturns[i];
      if (p !== undefined && s !== undefined) acc.push({ date: fmtLabel(d), full: d, portfolio: p, spy: s });
      return acc;
    }, []);

    const xTickEvery = chartData.length <= 12 ? 0 : Math.floor(chartData.length / 8);

    const riskLabel = RISK_OPTIONS.find(r => r.key === sim.riskLevel)?.label ?? sim.riskLevel;
    const holdingsRows: HoldingPnl[] = perfData?.holdings ?? sim.holdings.map(h => ({
      symbol: h.symbol, name: h.name, shares: h.shares,
      buyPrice: h.buyPrice, currentPrice: h.buyPrice,
      value: h.shares * h.buyPrice, pnl: 0, pnlPct: 0,
    }));

    return (
      <Box sx={{ py: 3 }}>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start" sx={{ mb: 3 }}>
          <Box>
            <Typography variant="h5" fontWeight={800}>Portfolio Race</Typography>
            <Typography color="text.secondary" variant="body2">
              Started {new Date(sim.startDate).toLocaleDateString()} ·{' '}
              {daysSince === 0 ? 'Just started today' : `${daysSince}d running`}
            </Typography>
          </Box>
          <Stack direction="row" spacing={1} alignItems="center">
            <IconButton
              size="small"
              onClick={() => fetchPerf(sim, chartInterval)}
              disabled={perfLoading}
              sx={{ border: '1px solid rgba(255,255,255,0.1)' }}
            >
              <RefreshCw size={15} className={perfLoading ? 'animate-spin' : ''} />
            </IconButton>
            <Button
              size="small" variant="outlined" onClick={handleReset}
              sx={{ borderColor: 'rgba(255,255,255,0.18)', color: 'text.secondary', fontSize: 12 }}
            >
              Reset
            </Button>
          </Stack>
        </Stack>

        {/* Stats row */}
        <Grid container spacing={2} sx={{ mb: 3 }}>
          <Grid size={{ xs: 6, md: 3 }}>
            <StatCard
              label="Your Portfolio"
              value={`${portReturn >= 0 ? '+' : ''}${portReturn.toFixed(2)}%`}
              sub={`$${(perfData?.currentPortfolioValue ?? initialCost).toLocaleString('en-US', { maximumFractionDigits: 0 })}`}
              color={portReturn >= 0 ? '#10b981' : '#ef4444'}
            />
          </Grid>
          <Grid size={{ xs: 6, md: 3 }}>
            <StatCard
              label="S&P 500 (SPY)"
              value={`${spyReturn >= 0 ? '+' : ''}${spyReturn.toFixed(2)}%`}
              sub={`$${(perfData?.currentSpyValue ?? initialCost).toLocaleString('en-US', { maximumFractionDigits: 0 })}`}
              color={spyReturn >= 0 ? '#10b981' : '#ef4444'}
            />
          </Grid>
          <Grid size={{ xs: 6, md: 3 }}>
            <StatCard
              label="vs S&P 500"
              value={`${diff >= 0 ? '+' : ''}${diff.toFixed(2)}%`}
              sub={isWinning ? '🏆 You\'re winning!' : '📉 SPY leads'}
              color={isWinning ? '#f59e0b' : '#94a3b8'}
            />
          </Grid>
          <Grid size={{ xs: 6, md: 3 }}>
            <StatCard
              label="Portfolio"
              value={String(sim.holdings.length)}
              sub={`${riskLabel} · ${sim.sectors.length} sectors`}
              color="#00f2ff"
            />
          </Grid>
        </Grid>

        {/* Race chart */}
        <Paper sx={{ p: 2.5, mb: 3, border: '1px solid rgba(255,255,255,0.07)' }}>
          <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
            <Stack direction="row" alignItems="center" spacing={1.5}>
              <Typography variant="subtitle1" fontWeight={700}>Cumulative Return (%)</Typography>
              {chartInterval !== '1d' && (
                <Box sx={{
                  display: 'flex', alignItems: 'center', gap: 0.5,
                  px: 1, py: 0.25, borderRadius: 1,
                  bgcolor: 'rgba(16,185,129,0.12)', border: '1px solid rgba(16,185,129,0.3)',
                }}>
                  <Box sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: '#10b981', animation: 'pulse 2s infinite' }} />
                  <Typography variant="caption" sx={{ color: '#10b981', fontSize: 10 }}>LIVE · 60s</Typography>
                </Box>
              )}
            </Stack>
            <Stack direction="row" spacing={0.5}>
              {(['1m', '5m', '1h', '1d'] as const).map(iv => (
                <Button
                  key={iv}
                  size="small"
                  variant={chartInterval === iv ? 'contained' : 'outlined'}
                  onClick={() => setChartInterval(iv)}
                  disabled={perfLoading}
                  sx={{
                    minWidth: 38, px: 0.75, py: 0.25, fontSize: 11, fontWeight: 700,
                    ...(chartInterval === iv
                      ? { background: 'rgba(0,242,255,0.18)', color: 'primary.main', border: '1px solid rgba(0,242,255,0.5)' }
                      : { borderColor: 'rgba(255,255,255,0.12)', color: 'text.secondary' }),
                  }}
                >
                  {iv.toUpperCase()}
                </Button>
              ))}
            </Stack>
          </Stack>
          {perfLoading ? (
            <Box sx={{ height: 280, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <CircularProgress sx={{ color: 'primary.main' }} />
            </Box>
          ) : chartData.length === 0 ? (
            <Box sx={{ height: 200, display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: 1 }}>
              <BarChart2 size={32} color="#334155" />
              <Typography color="text.secondary" variant="body2">
                {chartInterval === '1m' || chartInterval === '5m'
                  ? 'No intraday data yet — markets may be closed or data is still loading.'
                  : daysSince === 0
                    ? 'Race just started — try 1H view or check back after the next trading session.'
                    : 'No performance data available yet.'}
              </Typography>
            </Box>
          ) : (
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={chartData} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.04)" />
                <XAxis
                  dataKey="date"
                  tick={{ fill: '#64748b', fontSize: 11 }}
                  tickLine={false}
                  interval={xTickEvery}
                />
                <YAxis
                  tick={{ fill: '#64748b', fontSize: 11 }} tickLine={false}
                  tickFormatter={v => `${(v as number).toFixed(1)}%`}
                  width={52}
                />
                <Tooltip
                  contentStyle={{ background: '#0d1117', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8 }}
                  labelFormatter={(label, payload) => {
                    const full = (payload?.[0]?.payload as { full?: string })?.full ?? label;
                    return <span style={{ color: '#94a3b8', fontSize: 11 }}>{full}</span>;
                  }}
                  formatter={(v: number, name: string) => [
                    `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`,
                    name === 'portfolio' ? 'Your Portfolio' : 'S&P 500',
                  ]}
                />
                <ReferenceLine y={0} stroke="rgba(255,255,255,0.18)" strokeDasharray="4 4" />
                <Legend
                  formatter={v => v === 'portfolio' ? 'Your Portfolio' : 'S&P 500 (Benchmark)'}
                  wrapperStyle={{ color: '#94a3b8', fontSize: 12 }}
                />
                <Line type="monotone" dataKey="portfolio" stroke="#00f2ff" strokeWidth={2.5} dot={chartData.length < 30} activeDot={{ r: 4 }} name="portfolio" />
                <Line type="monotone" dataKey="spy"       stroke="#7c4dff" strokeWidth={2}   dot={chartData.length < 30} activeDot={{ r: 4 }} name="spy" />
              </LineChart>
            </ResponsiveContainer>
          )}
        </Paper>

        {/* Holdings table */}
        <Paper sx={{ p: 2.5, border: '1px solid rgba(255,255,255,0.07)' }}>
          <Typography variant="subtitle1" fontWeight={700} sx={{ mb: 2 }}>Holdings</Typography>
          <Box sx={{ overflowX: 'auto' }}>
            <Table size="small">
              <TableHead>
                <TableRow sx={{ borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
                  {['Symbol', 'Shares', 'Buy Price', 'Current', 'Value', 'P&L', 'Return'].map(h => (
                    <TableCell key={h} sx={{ color: '#475569', fontWeight: 600, fontSize: 12, borderBottom: 'none' }}>
                      {h}
                    </TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {holdingsRows.map(h => (
                  <TableRow key={h.symbol} sx={{ borderBottom: '1px solid rgba(255,255,255,0.04)', '&:last-child td': { borderBottom: 0 } }}>
                    <TableCell sx={{ borderBottom: 'none' }}>
                      <Typography variant="subtitle2" fontWeight={700}>{h.symbol}</Typography>
                      <Typography variant="caption" color="text.secondary">{h.name}</Typography>
                    </TableCell>
                    <TableCell sx={{ color: '#94a3b8', fontSize: 14, borderBottom: 'none' }}>{h.shares}</TableCell>
                    <TableCell sx={{ color: '#94a3b8', fontSize: 14, borderBottom: 'none' }}>${h.buyPrice.toFixed(2)}</TableCell>
                    <TableCell sx={{ fontSize: 14, borderBottom: 'none' }}>${h.currentPrice.toFixed(2)}</TableCell>
                    <TableCell sx={{ fontSize: 14, borderBottom: 'none' }}>
                      ${h.value.toLocaleString('en-US', { maximumFractionDigits: 0 })}
                    </TableCell>
                    <TableCell sx={{ fontSize: 14, borderBottom: 'none', color: h.pnl >= 0 ? '#10b981' : '#ef4444' }}>
                      {h.pnl >= 0 ? '+' : ''}${h.pnl.toFixed(2)}
                    </TableCell>
                    <TableCell sx={{ fontSize: 14, fontWeight: 700, borderBottom: 'none', color: h.pnlPct >= 0 ? '#10b981' : '#ef4444' }}>
                      {h.pnlPct >= 0 ? '+' : ''}{h.pnlPct.toFixed(2)}%
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
        </Paper>
      </Box>
    );
  }

  // ── Root render ────────────────────────────────────────────────────────────

  return (
    <ThemeProvider theme={darkTheme}>
      <CssBaseline />
      <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
        {/* Top bar */}
        <Box sx={{
          borderBottom: '1px solid rgba(255,255,255,0.06)',
          px: 3, py: 1.5,
          display: 'flex', alignItems: 'center', gap: 2,
        }}>
          <IconButton component="a" href="/" size="small" sx={{ color: 'text.secondary' }}>
            <ArrowLeft size={18} />
          </IconButton>
          <Typography variant="subtitle1" fontWeight={700} sx={{ color: 'primary.main' }}>
            FiForesight
          </Typography>
          <Typography color="text.secondary" variant="subtitle1">/</Typography>
          <Typography variant="subtitle1" fontWeight={600}>Portfolio Race</Typography>
        </Box>

        <Container maxWidth="lg" sx={{ py: 4 }}>
          {phase === 'welcome'   && Welcome()}
          {phase === 'chat'      && Chat()}
          {phase === 'analyzing' && Analyzing()}
          {phase === 'portfolio' && Portfolio()}
          {phase === 'race'      && simulation && Race()}
        </Container>
      </Box>
    </ThemeProvider>
  );
}
