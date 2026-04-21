import React, { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';
import {
  colors, typography, spacing, radius,
  card, pageTitle, pageSubtitle, sectionTitle,
  buttonPrimary, pill,
} from '../theme';

const JOB_LABELS = {
  volume_scanner:   { label: 'Volume Scanner',    interval: '15m', desc: 'Scans DEX Screener for BSC volume spikes' },
  profile_checker:  { label: 'Profile Checker',   interval: '30m', desc: 'Enriches tokens via BscScan (supply, holders, contract)' },
  wallet_analyzer:  { label: 'Wallet Analyzer',   interval: '4h',  desc: 'Traces holder funding sources for cluster detection' },
  exchange_flow:    { label: 'Exchange Flow',     interval: '30m', desc: 'Monitors deposits/withdrawals to known exchange wallets' },
  social_scanner:   { label: 'Social Scanner',    interval: '2h',  desc: 'CoinGecko trending + social mentions' },
  wallet_tracker:   { label: 'Wallet Tracker',    interval: '30m', desc: 'Follows known operator wallets for new activity' },
  scorer:           { label: 'Score Calculator',  interval: '30m', desc: 'Recalculates 0–100 scores and fires Telegram alerts' },
  cleanup:          { label: 'Cleanup',           interval: '6h',  desc: 'Expires old flagged tokens' },
  wallet_seeder:    { label: 'Wallet Seeder',     interval: 'manual', desc: 'Extracts known wallets from confirmed BSC pumps' },
  solana_seeder:    { label: 'Solana Seeder',     interval: 'manual', desc: 'Bootstraps Solana infrastructure rows' },
  solana_graph_walk:{ label: 'Solana Graph Walk', interval: '15m', desc: 'Follows cabal money flow to discover rotated wallets' },
};

function StatusBadge({ status }) {
  const variants = {
    success: { variant: 'success', text: 'Success' },
    error:   { variant: 'danger',  text: 'Error' },
    never_run:{ variant: 'default', text: 'Never run' },
  };
  const v = variants[status] || { variant: 'default', text: status };
  return <span style={pill(v.variant)}>{v.text}</span>;
}

function JobCard({ job }) {
  const meta = JOB_LABELS[job.job_name] || { label: job.job_name, interval: '?', desc: '' };
  return (
    <div style={{
      ...card,
      borderColor: job.last_status === 'error' ? colors.danger : colors.border,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: spacing.sm }}>
        <div style={{
          fontSize: typography.h3,
          fontWeight: typography.semibold,
          color: colors.text,
          letterSpacing: '-0.01em',
        }}>
          {meta.label}
        </div>
        <StatusBadge status={job.last_status} />
      </div>
      <div style={{ color: colors.textFaint, fontSize: typography.small, marginBottom: spacing.md, lineHeight: 1.5 }}>
        {meta.desc}
      </div>
      <div style={{ display: 'flex', gap: spacing.md, fontSize: typography.tiny, color: colors.textFaint, marginBottom: spacing.sm }}>
        <span>Interval: <span style={{ color: colors.text, fontWeight: typography.medium }}>{meta.interval}</span></span>
        <span>Runs: <span style={{ color: colors.text, fontWeight: typography.medium }}>{job.total_runs}</span></span>
        {job.errors > 0 && (
          <span style={{ color: colors.danger }}>Errors: {job.errors}</span>
        )}
      </div>
      {job.last_run && (
        <div style={{ fontSize: typography.tiny, color: colors.textMuted, marginBottom: spacing.sm }}>
          Last: {formatDate(job.last_run)}
          {job.last_duration_seconds != null && ` (${job.last_duration_seconds}s)`}
        </div>
      )}
      {job.last_details && (
        <div style={{
          fontSize: typography.tiny,
          color: colors.textDim,
          background: colors.bg,
          padding: '8px 10px',
          borderRadius: radius.sm,
          lineHeight: 1.5,
          fontFamily: typography.mono,
        }}>
          {job.last_details}
        </div>
      )}
    </div>
  );
}

function ScanActivity() {
  const { data: summary } = useApi('/dashboard/scan-logs/summary', { refreshInterval: 15000 });
  const [selectedJob, setSelectedJob] = useState(null);
  const { data: logs } = useApi(
    `/dashboard/scan-logs?per_page=30${selectedJob ? `&job_name=${selectedJob}` : ''}`,
    { refreshInterval: 15000 }
  );
  const [seeding, setSeeding] = useState(false);
  const [seedMsg, setSeedMsg] = useState(null);

  async function triggerSeed() {
    setSeeding(true);
    setSeedMsg(null);
    try {
      const res = await fetch('/api/dashboard/seed-wallets', { method: 'POST' });
      const data = await res.json();
      setSeedMsg(data.message);
    } catch (err) {
      setSeedMsg('Error: ' + err.message);
    }
    setSeeding(false);
  }

  return (
    <div>
      <h1 style={pageTitle}>Scanner Activity</h1>
      <div style={pageSubtitle}>
        Every scheduled job in the background pipeline and what it just ran.
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
        gap: spacing.md,
        marginBottom: spacing.xl,
      }}>
        {(summary || []).map((j) => (
          <div key={j.job_name} onClick={() => setSelectedJob(selectedJob === j.job_name ? null : j.job_name)} style={{ cursor: 'pointer' }}>
            <JobCard job={j} />
          </div>
        ))}
      </div>

      <div style={{ ...card, marginBottom: spacing.xl, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: spacing.md, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: typography.h3, fontWeight: typography.semibold, color: colors.text, marginBottom: 4 }}>
            Seed Known Wallets
          </div>
          <div style={{ color: colors.textFaint, fontSize: typography.small }}>
            Extracts operator wallets from confirmed BSC pumps (RAVE, SIREN, RIVER, ARIA, STO, DEXE).
          </div>
          {seedMsg && (
            <div style={{ color: colors.success, fontSize: typography.small, marginTop: 8 }}>{seedMsg}</div>
          )}
        </div>
        <button
          onClick={triggerSeed}
          disabled={seeding}
          style={{ ...buttonPrimary, opacity: seeding ? 0.5 : 1 }}
        >
          {seeding ? 'Running…' : 'Run Seeder'}
        </button>
      </div>

      <div style={{ ...card, padding: 0 }}>
        <div style={{ padding: `${spacing.md} ${spacing.lg}`, borderBottom: `1px solid ${colors.border}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={sectionTitle}>
            {selectedJob ? `Logs: ${JOB_LABELS[selectedJob]?.label || selectedJob}` : 'All Recent Logs'}
          </div>
          {selectedJob && (
            <button
              onClick={() => setSelectedJob(null)}
              style={{
                background: 'transparent',
                border: `1px solid ${colors.borderStrong}`,
                color: colors.textDim,
                padding: '6px 12px',
                borderRadius: radius.sm,
                fontSize: typography.small,
                cursor: 'pointer',
              }}
            >
              Show all
            </button>
          )}
        </div>
        <div style={{ maxHeight: 600, overflowY: 'auto' }}>
          {(logs?.items || []).length === 0 ? (
            <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
              No logs yet.
            </div>
          ) : (
            (logs?.items || []).map((entry) => (
              <div key={entry.id} style={{
                padding: `${spacing.md} ${spacing.lg}`,
                borderBottom: `1px solid ${colors.border}`,
                fontSize: typography.small,
              }}>
                <div style={{ display: 'flex', gap: spacing.sm, alignItems: 'center', marginBottom: 4, flexWrap: 'wrap' }}>
                  <span style={pill('accent')}>
                    {JOB_LABELS[entry.job_name]?.label || entry.job_name}
                  </span>
                  <StatusBadge status={entry.status} />
                  <span style={{ color: colors.textFaint }}>{formatDate(entry.started_at)}</span>
                  {entry.duration_seconds != null && (
                    <span style={{ color: colors.textMuted }}>{entry.duration_seconds}s</span>
                  )}
                </div>
                {entry.details && (
                  <div style={{ color: colors.textDim, lineHeight: 1.5, fontFamily: typography.mono, fontSize: typography.tiny }}>
                    {entry.details}
                  </div>
                )}
                {entry.error_message && (
                  <div style={{ color: colors.danger, marginTop: 4, fontFamily: typography.mono, fontSize: typography.tiny }}>
                    {entry.error_message}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

export default ScanActivity;
