import React, { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';

const cardStyle = {
  background: '#111118',
  border: '1px solid #222',
  borderRadius: '8px',
  padding: '20px',
  marginBottom: '16px',
};

const JOB_LABELS = {
  volume_scanner: { label: 'Volume Scanner', icon: '~15m', desc: 'Scans DEX Screener for BSC volume spikes' },
  profile_checker: { label: 'Profile Checker', icon: '~1h', desc: 'Enriches tokens via BscScan (supply, holders, contract)' },
  wallet_analyzer: { label: 'Wallet Analyzer', icon: '~4h', desc: 'Traces holder funding sources for cluster detection' },
  exchange_flow: { label: 'Exchange Flow', icon: '~30m', desc: 'Monitors deposits/withdrawals to known exchange wallets' },
  social_scanner: { label: 'Social Scanner', icon: '~2h', desc: 'Checks CoinGecko trending and social mentions' },
  wallet_tracker: { label: 'Wallet Tracker', icon: '~30m', desc: 'Follows known operator wallets for new activity' },
  scorer: { label: 'Score Calculator', icon: '~30m', desc: 'Recalculates 0-100 scores for all active tokens' },
  cleanup: { label: 'Cleanup', icon: '~24h', desc: 'Expires old flagged tokens not seen in 7+ days' },
  wallet_seeder: { label: 'Wallet Seeder', icon: 'manual', desc: 'Extracts known wallets from confirmed pump tokens (RAVE, SIREN, RIVER, ARIA, STO)' },
};

function statusBadge(status) {
  const colors = {
    success: { bg: '#113311', color: '#44ff44' },
    error: { bg: '#331111', color: '#ff4444' },
    never_run: { bg: '#222', color: '#666' },
  };
  const c = colors[status] || colors.never_run;
  return (
    <span style={{
      padding: '2px 8px', borderRadius: '4px', fontSize: '11px',
      background: c.bg, color: c.color, fontWeight: 'bold',
    }}>
      {status === 'never_run' ? 'NEVER RUN' : status.toUpperCase()}
    </span>
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
      <h2 style={{ fontSize: '18px', marginBottom: '20px' }}>Scanner Activity Log</h2>

      {/* Job status cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '12px', marginBottom: '24px' }}>
        {(summary || []).map((job) => {
          const meta = JOB_LABELS[job.job_name] || { label: job.job_name, icon: '?', desc: '' };
          return (
            <div
              key={job.job_name}
              onClick={() => setSelectedJob(selectedJob === job.job_name ? null : job.job_name)}
              style={{
                ...cardStyle,
                marginBottom: 0,
                cursor: 'pointer',
                borderColor: selectedJob === job.job_name ? '#44aaff' : '#222',
                transition: 'border-color 0.2s',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <span style={{ fontWeight: 'bold', color: '#fff', fontSize: '14px' }}>
                  {meta.label}
                </span>
                {statusBadge(job.last_status)}
              </div>
              <div style={{ color: '#555', fontSize: '11px', marginBottom: '8px' }}>{meta.desc}</div>
              <div style={{ display: 'flex', gap: '16px', fontSize: '12px', color: '#888' }}>
                <span>Interval: {meta.icon}</span>
                <span>Runs: {job.total_runs}</span>
                {job.errors > 0 && <span style={{ color: '#ff4444' }}>Errors: {job.errors}</span>}
              </div>
              {job.last_run && (
                <div style={{ fontSize: '11px', color: '#555', marginTop: '6px' }}>
                  Last: {formatDate(job.last_run)}
                  {job.last_duration_seconds != null && ` (${job.last_duration_seconds}s)`}
                </div>
              )}
              {job.last_details && (
                <div style={{
                  fontSize: '11px', color: '#aaa', marginTop: '6px',
                  background: '#0a0a0f', padding: '6px 8px', borderRadius: '4px',
                  lineHeight: '1.4',
                }}>
                  {job.last_details}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Seed wallets button */}
      <div style={cardStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <div>
            <h3 style={{ fontSize: '14px', color: '#fff', marginBottom: '4px' }}>Seed Known Wallets</h3>
            <div style={{ color: '#666', fontSize: '12px' }}>
              Extracts deployers, top holders, and cluster patterns from RAVE, SIREN, RIVER, ARIA, STO.
              Only needs to run once — then the wallet tracker monitors them automatically.
            </div>
          </div>
          <button
            onClick={triggerSeed}
            disabled={seeding}
            style={{
              background: seeding ? '#222' : '#1a1a2e',
              border: '1px solid #44aaff',
              color: seeding ? '#555' : '#44aaff',
              padding: '10px 24px', borderRadius: '4px', cursor: seeding ? 'default' : 'pointer',
              fontSize: '13px', whiteSpace: 'nowrap', fontWeight: 'bold',
            }}
          >
            {seeding ? 'Running...' : 'Seed Wallets'}
          </button>
        </div>
        {seedMsg && (
          <div style={{ color: '#44ff44', fontSize: '12px', marginTop: '10px' }}>{seedMsg}</div>
        )}
      </div>

      {/* Detailed log entries */}
      <div style={cardStyle}>
        <h3 style={{ fontSize: '14px', marginBottom: '12px', color: '#888' }}>
          {selectedJob ? `Logs: ${JOB_LABELS[selectedJob]?.label || selectedJob}` : 'All Recent Logs'}
          {selectedJob && (
            <button
              onClick={() => setSelectedJob(null)}
              style={{
                background: 'none', border: '1px solid #333', color: '#666',
                padding: '2px 8px', borderRadius: '4px', cursor: 'pointer',
                fontSize: '11px', marginLeft: '12px',
              }}
            >
              Show All
            </button>
          )}
        </h3>
        <div style={{ maxHeight: '500px', overflowY: 'auto' }}>
          {(logs?.items || []).length === 0 ? (
            <div style={{ color: '#444', fontSize: '13px', padding: '20px', textAlign: 'center' }}>
              No scan logs yet. The scanner runs on a schedule — first logs will appear within 15 minutes of deployment.
            </div>
          ) : (
            (logs?.items || []).map((entry) => (
              <div key={entry.id} style={{
                padding: '12px',
                borderBottom: '1px solid #1a1a1a',
                fontSize: '12px',
              }}>
                <div style={{ display: 'flex', gap: '10px', alignItems: 'center', marginBottom: '4px' }}>
                  <span style={{
                    padding: '2px 6px', borderRadius: '3px', fontSize: '10px',
                    background: '#1a1a2e', color: '#44aaff',
                  }}>
                    {JOB_LABELS[entry.job_name]?.label || entry.job_name}
                  </span>
                  {statusBadge(entry.status)}
                  <span style={{ color: '#555' }}>{formatDate(entry.started_at)}</span>
                  {entry.duration_seconds != null && (
                    <span style={{ color: '#444' }}>{entry.duration_seconds}s</span>
                  )}
                  {entry.tokens_checked > 0 && (
                    <span style={{ color: '#888' }}>Checked: {entry.tokens_checked}</span>
                  )}
                  {entry.tokens_flagged > 0 && (
                    <span style={{ color: '#ffaa00' }}>Flagged: {entry.tokens_flagged}</span>
                  )}
                </div>
                {entry.details && (
                  <div style={{ color: '#aaa', lineHeight: '1.4' }}>{entry.details}</div>
                )}
                {entry.error_message && (
                  <div style={{ color: '#ff4444', marginTop: '4px' }}>{entry.error_message}</div>
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
