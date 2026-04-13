import React from 'react';
import { useApi } from '../hooks/useApi';
import TokenTable from './TokenTable';

const cardStyle = {
  background: '#111118',
  border: '1px solid #222',
  borderRadius: '8px',
  padding: '16px 20px',
  minWidth: '140px',
};

const statLabel = {
  color: '#666',
  fontSize: '11px',
  textTransform: 'uppercase',
  letterSpacing: '1px',
};

const statValue = {
  fontSize: '28px',
  fontWeight: 'bold',
  marginTop: '4px',
};

function Dashboard() {
  const { data: overview, loading } = useApi('/dashboard/overview', { refreshInterval: 30000 });
  const { data: watchlist } = useApi('/dashboard/watchlist?per_page=100', { refreshInterval: 30000 });

  if (loading && !overview) {
    return <div style={{ color: '#666', padding: '40px' }}>Loading scanner data...</div>;
  }

  const stats = overview || {};
  const tokens = watchlist?.items || [];

  return (
    <div>
      {/* Stats bar */}
      <div style={{ display: 'flex', gap: '16px', marginBottom: '24px', flexWrap: 'wrap' }}>
        <div style={cardStyle}>
          <div style={statLabel}>Active Flags</div>
          <div style={{ ...statValue, color: '#ffaa00' }}>{stats.active_flags || 0}</div>
        </div>
        <div style={cardStyle}>
          <div style={statLabel}>Watchlist</div>
          <div style={{ ...statValue, color: '#44aaff' }}>{stats.watchlist_count || 0}</div>
        </div>
        <div style={cardStyle}>
          <div style={statLabel}>Alerts Today</div>
          <div style={{ ...statValue, color: '#ff4444' }}>{stats.alerts_today || 0}</div>
        </div>
        <div style={cardStyle}>
          <div style={statLabel}>Total Alerts</div>
          <div style={statValue}>{stats.alerts_total || 0}</div>
        </div>
        <div style={cardStyle}>
          <div style={statLabel}>Win Rate</div>
          <div style={{ ...statValue, color: stats.win_rate > 50 ? '#44ff44' : '#ffaa00' }}>
            {stats.win_rate || 0}%
          </div>
        </div>
        <div style={cardStyle}>
          <div style={statLabel}>Known Wallets</div>
          <div style={statValue}>{stats.known_wallets || 0}</div>
        </div>
      </div>

      {/* Token table */}
      <div style={{ ...cardStyle, padding: '0', overflow: 'hidden' }}>
        <div style={{ padding: '16px 20px', borderBottom: '1px solid #222' }}>
          <h2 style={{ fontSize: '16px', margin: 0 }}>Watchlist & Candidates</h2>
        </div>
        <TokenTable tokens={tokens} />
      </div>
    </div>
  );
}

export default Dashboard;
