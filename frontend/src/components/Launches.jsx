import React, { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';

const cardStyle = {
  background: '#111118',
  border: '1px solid #222',
  borderRadius: '8px',
  padding: '16px 20px',
  minWidth: '140px',
};

const statLabel = { color: '#666', fontSize: '11px', textTransform: 'uppercase', letterSpacing: '1px' };
const statValue = { fontSize: '28px', fontWeight: 'bold', marginTop: '4px' };

const TIER_COLORS = {
  S: '#ff4444',
  A: '#ffaa00',
  B: '#44aaff',
  C: '#666',
};

function TierPill({ tier }) {
  const c = TIER_COLORS[tier] || '#444';
  return (
    <span style={{
      padding: '2px 8px', borderRadius: '4px',
      background: c, color: '#000',
      fontSize: '11px', fontWeight: 'bold',
    }}>
      {tier || '—'}
    </span>
  );
}

function Launches() {
  const [tier, setTier] = useState('');
  const [status, setStatus] = useState('');

  const { data: overview } = useApi('/launches/overview', { refreshInterval: 20000 });
  const query = new URLSearchParams();
  if (tier) query.set('tier', tier);
  if (status) query.set('status', status);
  query.set('per_page', '100');
  const { data: list } = useApi(`/launches/candidates?${query.toString()}`, { refreshInterval: 20000 });

  const items = list?.items || [];
  const byTier = overview?.by_tier || { S: 0, A: 0, B: 0, C: 0 };

  return (
    <div>
      <h2 style={{ fontSize: '18px', marginBottom: '16px' }}>Launch Detection</h2>

      <div style={{ display: 'flex', gap: '16px', marginBottom: '20px', flexWrap: 'wrap' }}>
        <div style={cardStyle}>
          <div style={statLabel}>S-Tier</div>
          <div style={{ ...statValue, color: TIER_COLORS.S }}>{byTier.S || 0}</div>
        </div>
        <div style={cardStyle}>
          <div style={statLabel}>A-Tier</div>
          <div style={{ ...statValue, color: TIER_COLORS.A }}>{byTier.A || 0}</div>
        </div>
        <div style={cardStyle}>
          <div style={statLabel}>B (Silent)</div>
          <div style={{ ...statValue, color: TIER_COLORS.B }}>{byTier.B || 0}</div>
        </div>
        <div style={cardStyle}>
          <div style={statLabel}>Alerted</div>
          <div style={{ ...statValue, color: '#44ff88' }}>{overview?.alerted || 0}</div>
        </div>
        <div style={cardStyle}>
          <div style={statLabel}>Watching</div>
          <div style={statValue}>{overview?.watching || 0}</div>
        </div>
      </div>

      <div style={{ display: 'flex', gap: '8px', marginBottom: '12px' }}>
        <select
          value={tier}
          onChange={(e) => setTier(e.target.value)}
          style={{ background: '#0a0a0f', color: '#fff', border: '1px solid #333', padding: '6px 10px', borderRadius: '4px' }}
        >
          <option value="">All tiers</option>
          <option value="S">S only</option>
          <option value="A">A only</option>
          <option value="B">B only</option>
          <option value="C">C only</option>
        </select>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          style={{ background: '#0a0a0f', color: '#fff', border: '1px solid #333', padding: '6px 10px', borderRadius: '4px' }}
        >
          <option value="">All statuses</option>
          <option value="watching">watching</option>
          <option value="alerted">alerted</option>
          <option value="expired">expired</option>
        </select>
      </div>

      <div style={{ ...cardStyle, padding: 0, overflow: 'hidden' }}>
        {items.length === 0 ? (
          <div style={{ padding: '40px', textAlign: 'center', color: '#444' }}>
            No launch candidates yet. The detection modules populate this as signals fire.
          </div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
            <thead>
              <tr>
                {['Tier', 'Score', 'Chain', 'Token', 'Contract', 'Deployer', 'Initial LP', 'Signals', 'Last Signal'].map(h => (
                  <th key={h} style={{
                    textAlign: 'left', padding: '10px 12px',
                    color: '#666', fontSize: '11px',
                    textTransform: 'uppercase', letterSpacing: '0.5px',
                    borderBottom: '1px solid #222',
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id} style={{ borderBottom: '1px solid #1a1a1a' }}>
                  <td style={{ padding: '10px 12px' }}><TierPill tier={c.alert_tier} /></td>
                  <td style={{ padding: '10px 12px', fontWeight: 'bold' }}>{c.composite_score}/100</td>
                  <td style={{ padding: '10px 12px', color: '#888' }}>{c.chain}</td>
                  <td style={{ padding: '10px 12px' }}>
                    <div style={{ fontWeight: 'bold' }}>{c.token_symbol || '—'}</div>
                    <div style={{ fontSize: '11px', color: '#555' }}>{c.token_name}</div>
                  </td>
                  <td style={{ padding: '10px 12px', fontFamily: 'monospace', fontSize: '11px', color: '#888' }}>
                    {c.contract_address.slice(0, 10)}…{c.contract_address.slice(-6)}
                  </td>
                  <td style={{ padding: '10px 12px', fontFamily: 'monospace', fontSize: '11px', color: '#888' }}>
                    {c.deployer_address ? `${c.deployer_address.slice(0, 8)}…` : '—'}
                  </td>
                  <td style={{ padding: '10px 12px' }}>
                    {c.initial_lp_usd ? `$${Number(c.initial_lp_usd).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : '—'}
                  </td>
                  <td style={{ padding: '10px 12px' }}>
                    <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                      {Object.keys(c.signal_summary || {}).map((k) => (
                        <span key={k} style={{
                          padding: '2px 6px', borderRadius: '3px',
                          background: '#1a1a2e', color: '#44aaff',
                          fontSize: '10px',
                        }}>{k}</span>
                      ))}
                    </div>
                  </td>
                  <td style={{ padding: '10px 12px', color: '#555' }}>{formatDate(c.last_signal_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export default Launches;
