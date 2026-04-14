import React from 'react';
import { useNavigate } from 'react-router-dom';
import { formatUSD, formatPct, scoreColor } from '../utils/formatters';
import ScoreGauge from './ScoreGauge';

const tableStyle = {
  width: '100%',
  borderCollapse: 'collapse',
  fontSize: '13px',
};

const thStyle = {
  textAlign: 'left',
  padding: '10px 12px',
  color: '#666',
  fontSize: '11px',
  textTransform: 'uppercase',
  letterSpacing: '0.5px',
  borderBottom: '1px solid #222',
  whiteSpace: 'nowrap',
};

const tdStyle = {
  padding: '10px 12px',
  borderBottom: '1px solid #1a1a1a',
  whiteSpace: 'nowrap',
};

function getRowBg(score) {
  if (score >= 70) return 'rgba(255, 68, 68, 0.08)';
  if (score >= 50) return 'rgba(255, 170, 0, 0.06)';
  return 'transparent';
}

function TokenTable({ tokens }) {
  const navigate = useNavigate();

  if (!tokens || tokens.length === 0) {
    return (
      <div style={{ padding: '40px', textAlign: 'center', color: '#444' }}>
        No tokens on watchlist yet. Scanner will populate this automatically.
      </div>
    );
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={tableStyle}>
        <thead>
          <tr>
            <th style={thStyle}>Score</th>
            <th style={thStyle}>Token</th>
            <th style={thStyle}>Price</th>
            <th style={thStyle}>24h Volume</th>
            <th style={thStyle}>MCap</th>
            <th style={thStyle}>Confidence</th>
            <th style={thStyle}>Cluster</th>
            <th style={thStyle}>Exchange Flow</th>
            <th style={thStyle}>Social</th>
            <th style={thStyle}>Alert</th>
          </tr>
        </thead>
        <tbody>
          {tokens.map((t) => (
            <tr
              key={t.contract_address}
              onClick={() => navigate(`/token/${t.contract_address}`)}
              style={{
                cursor: 'pointer',
                background: getRowBg(t.current_score),
                transition: 'background 0.2s',
              }}
              onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(255,255,255,0.03)'}
              onMouseLeave={(e) => e.currentTarget.style.background = getRowBg(t.current_score)}
            >
              <td style={tdStyle}>
                <ScoreGauge score={t.current_score || 0} size={40} />
              </td>
              <td style={tdStyle}>
                <div style={{ fontWeight: 'bold', color: '#fff' }}>{t.token_symbol || '?'}</div>
                <div style={{ fontSize: '11px', color: '#555' }}>{t.token_name}</div>
              </td>
              <td style={tdStyle}>{formatUSD(t.price_usd)}</td>
              <td style={tdStyle}>{formatUSD(t.volume_24h)}</td>
              <td style={tdStyle}>{formatUSD(t.market_cap)}</td>
              <td style={tdStyle}>
                <span style={{
                  color: t.confidence_level === 'HIGH' ? '#ff4444' :
                         t.confidence_level === 'MEDIUM' ? '#ffaa00' : '#666',
                  fontWeight: 'bold',
                  fontSize: '11px',
                }}>
                  {t.confidence_level || '-'}
                </span>
              </td>
              <td style={tdStyle}>
                {t.cluster_detected === true ? (
                  <span style={{ color: '#ff4444' }}>YES ({t.cluster_wallet_count})</span>
                ) : t.cluster_detected === false ? (
                  <span style={{ color: '#444' }}>No</span>
                ) : (
                  <span style={{ color: '#555', fontStyle: 'italic' }} title="Wallet analyzer hasn't scored this token yet">pending</span>
                )}
              </td>
              <td style={tdStyle}>
                {t.exchange_deposits_detected ? (
                  <span style={{ color: '#ff4444' }}>DETECTED</span>
                ) : (
                  <span style={{ color: '#444' }}>No</span>
                )}
              </td>
              <td style={tdStyle}>
                {t.social_signal_detected ? (
                  <span style={{ color: '#ffaa00' }}>Active</span>
                ) : (
                  <span style={{ color: '#444' }}>-</span>
                )}
              </td>
              <td style={tdStyle}>
                {t.alert_fired ? (
                  <span style={{ color: '#ff4444', fontWeight: 'bold' }}>FIRED</span>
                ) : (
                  <span style={{ color: '#444' }}>-</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default TokenTable;
