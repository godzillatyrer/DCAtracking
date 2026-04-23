import React from 'react';
import { useApi } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';
import {
  colors, typography, spacing, radius,
  card, pageTitle, pageSubtitle, sectionTitle, pill,
} from '../theme';

function StatusPill({ status }) {
  const cfg = {
    success: { bg: colors.successSoft, color: colors.success, label: 'OK' },
    ok:      { bg: colors.successSoft, color: colors.success, label: 'OK' },
    error:   { bg: colors.dangerSoft,  color: colors.danger,  label: 'ERROR' },
    stale:   { bg: colors.warningSoft, color: colors.warning, label: 'STALE' },
    pending: { bg: colors.bgSubtle,    color: colors.textFaint, label: 'PENDING' },
    not_configured: { bg: colors.bgSubtle, color: colors.textMuted, label: 'NOT SET' },
  };
  const c = cfg[status] || cfg.pending;
  return (
    <span style={{
      ...pill('default'),
      background: c.bg, color: c.color,
    }}>{c.label}</span>
  );
}

function JobRow({ job }) {
  // Heuristic: if last ran > 3x the expected cadence ago, mark stale.
  // We don't know each job's exact cadence here; just surface age.
  let status = job.last_status;
  if (status === 'success' && job.age_min != null && job.age_min > 60) {
    status = 'stale';
  }
  return (
    <div style={{
      padding: `${spacing.sm} ${spacing.lg}`,
      borderBottom: `1px solid ${colors.border}`,
      display: 'grid',
      gridTemplateColumns: '260px 100px 140px 1fr',
      gap: spacing.md, alignItems: 'center',
    }}>
      <div style={{
        fontFamily: typography.mono, fontSize: typography.small,
        color: colors.text,
      }}>{job.name}</div>
      <StatusPill status={status} />
      <div style={{ color: colors.textFaint, fontSize: typography.small }}>
        {job.age_min != null ? `${job.age_min}m ago` : 'never'}
      </div>
      <div style={{ color: colors.textMuted, fontSize: typography.tiny }}>
        {job.last_error || (job.last_run ? formatDate(job.last_run) : '')}
      </div>
    </div>
  );
}

function ApiRow({ api }) {
  const status = api.status === 'ok' ? 'ok' :
                 api.status === 'not_configured' ? 'not_configured' :
                 'error';
  return (
    <div style={{
      padding: `${spacing.sm} ${spacing.lg}`,
      borderBottom: `1px solid ${colors.border}`,
      display: 'grid',
      gridTemplateColumns: '160px 100px 100px 1fr',
      gap: spacing.md, alignItems: 'center',
    }}>
      <div style={{ fontWeight: typography.medium }}>{api.name}</div>
      <StatusPill status={status} />
      <div style={{ color: colors.textFaint, fontSize: typography.small }}>
        {api.latency_ms != null ? `${api.latency_ms}ms` : '—'}
      </div>
      <div style={{ color: colors.textFaint, fontSize: typography.small }}>{api.detail || api.purpose}</div>
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div style={{
      ...card, flex: '1 1 160px', padding: spacing.md,
    }}>
      <div style={{
        fontSize: typography.tiny, color: colors.textFaint,
        textTransform: 'uppercase', letterSpacing: 1, marginBottom: 4,
      }}>{label}</div>
      <div style={{
        fontSize: typography.h2, fontWeight: typography.bold,
        color: color || colors.text, letterSpacing: '-0.02em',
      }}>{value}</div>
    </div>
  );
}

function Health() {
  const { data, loading } = useApi('/health/full', { refreshInterval: 30000 });

  const jobs = data?.jobs || [];
  const apis = data?.apis || [];
  const dbs = data?.db || {};
  const alerts = data?.alerts || {};

  return (
    <div>
      <h1 style={pageTitle}>System Health</h1>
      <div style={pageSubtitle}>
        Live view of scheduler jobs, external APIs, DB inventory, and
        alert throughput. Auto-refreshes every 30s.
      </div>

      <div style={{
        display: 'flex', gap: spacing.md, marginBottom: spacing.xl,
        flexWrap: 'wrap',
      }}>
        <Stat label="Tracked wallets" value={dbs.tracked_wallets ?? '—'} />
        <Stat label="Snipers" value={dbs.snipers ?? 0} color={colors.success} />
        <Stat label="Dormant" value={dbs.dormant_wallets ?? 0} color={colors.textFaint} />
        <Stat label="Activity rows" value={(dbs.activity_rows || 0).toLocaleString()} />
        <Stat label="Alerts (24h)" value={alerts.fired_24h ?? 0} color={colors.accent} />
        <Stat label="Sent (24h)" value={alerts.sent_24h ?? 0} color={colors.success} />
        <Stat label="CEX addrs" value={dbs.cex_addresses ?? 0} />
      </div>

      <div style={{ ...card, padding: 0, marginBottom: spacing.xl }}>
        <div style={{
          padding: `${spacing.md} ${spacing.lg}`,
          borderBottom: `1px solid ${colors.border}`,
        }}>
          <div style={sectionTitle}>Scheduler jobs</div>
        </div>
        {loading && jobs.length === 0 ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            Loading…
          </div>
        ) : (
          jobs.map(j => <JobRow key={j.name} job={j} />)
        )}
      </div>

      <div style={{ ...card, padding: 0, marginBottom: spacing.xl }}>
        <div style={{
          padding: `${spacing.md} ${spacing.lg}`,
          borderBottom: `1px solid ${colors.border}`,
        }}>
          <div style={sectionTitle}>External APIs</div>
        </div>
        {apis.length === 0 ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            Loading…
          </div>
        ) : (
          apis.map(a => <ApiRow key={a.name} api={a} />)
        )}
      </div>
    </div>
  );
}

export default Health;
