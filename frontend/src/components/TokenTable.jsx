import React from 'react';
import { useNavigate } from 'react-router-dom';
import { formatUSD } from '../utils/formatters';
import { colors, typography, th, td, pill } from '../theme';

function ScoreBadge({ score }) {
  let variant = 'default';
  if (score >= 70) variant = 'danger';
  else if (score >= 50) variant = 'warning';
  return (
    <span style={{
      ...pill(variant),
      fontFamily: typography.mono,
      fontSize: typography.small,
      padding: '6px 12px',
    }}>
      {score}
    </span>
  );
}

function TokenTable({ tokens }) {
  const navigate = useNavigate();

  if (!tokens || tokens.length === 0) {
    return (
      <div style={{
        padding: '80px 40px',
        textAlign: 'center',
        color: colors.textFaint,
        fontSize: typography.body,
      }}>
        No tokens on watchlist yet. The scanner populates this automatically as
        pump patterns emerge.
      </div>
    );
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={th}>Score</th>
            <th style={th}>Token</th>
            <th style={th}>Price</th>
            <th style={th}>24h Volume</th>
            <th style={th}>Market Cap</th>
            <th style={th}>Confidence</th>
            <th style={th}>Cluster</th>
            <th style={th}>Exchange Flow</th>
            <th style={th}>Alert</th>
          </tr>
        </thead>
        <tbody>
          {tokens.map((t) => (
            <tr
              key={t.contract_address}
              onClick={() => navigate(`/token/${t.contract_address}`)}
              style={{
                cursor: 'pointer',
                transition: 'background 0.15s',
              }}
              onMouseEnter={(e) => e.currentTarget.style.background = colors.bgHover}
              onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
            >
              <td style={td}>
                <ScoreBadge score={t.current_score || 0} />
              </td>
              <td style={td}>
                <div style={{ fontWeight: typography.semibold, color: colors.text, fontSize: typography.body }}>
                  {t.token_symbol || '—'}
                </div>
                {t.token_name && (
                  <div style={{ fontSize: typography.small, color: colors.textFaint, marginTop: 2 }}>
                    {t.token_name}
                  </div>
                )}
              </td>
              <td style={{ ...td, fontFamily: typography.mono, color: colors.textDim }}>
                {formatUSD(t.price_usd)}
              </td>
              <td style={{ ...td, fontFamily: typography.mono, color: colors.textDim }}>
                {formatUSD(t.volume_24h)}
              </td>
              <td style={{ ...td, fontFamily: typography.mono, color: colors.textDim }}>
                {formatUSD(t.market_cap)}
              </td>
              <td style={td}>
                {t.confidence_level && (
                  <span style={pill(
                    t.confidence_level === 'HIGH' ? 'danger' :
                    t.confidence_level === 'MEDIUM' ? 'warning' : 'default'
                  )}>
                    {t.confidence_level}
                  </span>
                )}
              </td>
              <td style={td}>
                {t.cluster_detected === true ? (
                  <span style={pill('danger')}>YES · {t.cluster_wallet_count}</span>
                ) : t.cluster_detected === false ? (
                  <span style={{ color: colors.textMuted, fontSize: typography.small }}>No</span>
                ) : (
                  <span style={{ color: colors.textMuted, fontSize: typography.small, fontStyle: 'italic' }}>
                    pending
                  </span>
                )}
              </td>
              <td style={td}>
                {t.exchange_deposits_detected ? (
                  <span style={pill('danger')}>DETECTED</span>
                ) : (
                  <span style={{ color: colors.textMuted, fontSize: typography.small }}>—</span>
                )}
              </td>
              <td style={td}>
                {t.alert_fired ? (
                  <span style={pill('danger')}>FIRED</span>
                ) : (
                  <span style={{ color: colors.textMuted, fontSize: typography.small }}>—</span>
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
