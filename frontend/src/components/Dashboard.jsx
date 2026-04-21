import React from 'react';
import { useApi } from '../hooks/useApi';
import TokenTable from './TokenTable';
import {
  colors, typography, spacing, radius,
  card, cardHero, metricValue, metricLabel,
  sectionTitle, pageTitle, pageSubtitle,
} from '../theme';

function Metric({ label, value, color, suffix = '' }) {
  return (
    <div style={{
      ...card,
      flex: 1,
      minWidth: 180,
      background: colors.bgElev1,
    }}>
      <div style={metricLabel}>{label}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
        <div style={{ ...metricValue, color: color || colors.text }}>
          {value}
        </div>
        {suffix && (
          <div style={{ fontSize: typography.body, color: colors.textFaint }}>
            {suffix}
          </div>
        )}
      </div>
    </div>
  );
}

function Dashboard() {
  const { data: overview, loading } = useApi('/dashboard/overview', { refreshInterval: 30000 });
  const { data: watchlist } = useApi('/dashboard/watchlist?per_page=100', { refreshInterval: 30000 });

  const stats = overview || {};
  const tokens = watchlist?.items || [];

  return (
    <div>
      <div>
        <h1 style={pageTitle}>Dashboard</h1>
        <div style={pageSubtitle}>
          Real-time detection of coordinated pumps on Binance Smart Chain.
        </div>
      </div>

      {loading && !overview && (
        <div style={{ color: colors.textFaint, padding: spacing.xxl, textAlign: 'center' }}>
          Loading…
        </div>
      )}

      <div style={{ display: 'flex', gap: spacing.md, marginBottom: spacing.xl, flexWrap: 'wrap' }}>
        <Metric label="Active Flags" value={(stats.active_flags ?? 0).toLocaleString()} />
        <Metric label="Watchlist" value={(stats.watchlist_count ?? 0).toLocaleString()} color={colors.accent} />
        <Metric label="Alerts Today" value={(stats.alerts_today ?? 0).toLocaleString()} color={colors.danger} />
        <Metric label="Total Alerts" value={(stats.alerts_total ?? 0).toLocaleString()} />
        <Metric label="Win Rate" value={`${stats.win_rate ?? 0}`} suffix="%" color={stats.win_rate > 50 ? colors.success : colors.textDim} />
        <Metric label="Known Wallets" value={(stats.known_wallets ?? 0).toLocaleString()} />
      </div>

      <div style={{ ...card, padding: 0, overflow: 'hidden' }}>
        <div style={{
          padding: `${spacing.md} ${spacing.lg}`,
          borderBottom: `1px solid ${colors.border}`,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <div>
            <div style={sectionTitle}>Watchlist & Candidates</div>
            <div style={{ fontSize: typography.small, color: colors.textFaint }}>
              Tokens scoring 50+ on the pump-detection model.
            </div>
          </div>
          <div style={{ fontSize: typography.small, color: colors.textFaint }}>
            {tokens.length} token{tokens.length === 1 ? '' : 's'}
          </div>
        </div>
        <TokenTable tokens={tokens} />
      </div>
    </div>
  );
}

export default Dashboard;
