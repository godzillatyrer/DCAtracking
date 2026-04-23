import React from 'react';
import { useApi } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';
import {
  colors, typography, spacing, radius,
  card, pageTitle, pageSubtitle, sectionTitle, th, td, pill,
} from '../theme';

function SolscanLink({ address, children }) {
  if (!address) return children;
  return (
    <a href={`https://solscan.io/token/${address}`} target="_blank" rel="noreferrer"
      style={{
        color: colors.accent, textDecoration: 'none',
        fontFamily: typography.mono, fontSize: typography.small,
      }}>{children}</a>
  );
}

function fmtUsd(n) {
  if (n == null) return '—';
  const v = Number(n) || 0;
  if (Math.abs(v) >= 1_000_000) return `$${(v/1_000_000).toFixed(2)}M`;
  if (Math.abs(v) >= 1_000) return `$${(v/1_000).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}

function fmtPct(n) {
  if (n == null) return '—';
  const v = Number(n);
  const sign = v >= 0 ? '+' : '';
  return `${sign}${v.toFixed(0)}%`;
}

function typeVariant(t) {
  if (t === 'cabal_convergence') return 'accent';
  if (t === 'sniper_solo') return 'success';
  if (t === 'cabal_exit') return 'danger';
  return 'default';
}

function AlertHistory() {
  const { data } = useApi('/outcomes?limit=100', { refreshInterval: 30000 });
  const { data: summary } = useApi('/outcomes/summary', { refreshInterval: 60000 });
  const items = data?.items || [];
  const perType = summary?.summary || [];

  return (
    <div>
      <h1 style={pageTitle}>Alert History</h1>
      <div style={pageSubtitle}>
        Every alert fired, tracked over 24h. Peak % shows the biggest
        post-alert MC pump; drawdown is current vs peak. "Still
        holding" is a 15-min snapshot of cabal wallets from the alert.
      </div>

      {/* Summary cards */}
      {perType.length > 0 && (
        <div style={{
          display: 'flex', gap: spacing.md, marginBottom: spacing.xl,
          flexWrap: 'wrap',
        }}>
          {perType.map(s => (
            <div key={s.alert_type} style={{
              ...card, flex: '1 1 260px', minWidth: 260,
            }}>
              <div style={{
                fontSize: typography.tiny, textTransform: 'uppercase',
                letterSpacing: 1, color: colors.textFaint,
                marginBottom: 6,
              }}>{s.alert_type?.replace('_', ' ')}</div>
              <div style={{
                fontSize: typography.h2, fontWeight: typography.bold,
                letterSpacing: '-0.02em',
              }}>{s.count} closed</div>
              <div style={{ marginTop: spacing.sm, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4, fontSize: typography.small }}>
                <span style={{ color: colors.textFaint }}>Avg peak</span>
                <span style={{ fontFamily: typography.mono, textAlign: 'right' }}>
                  {fmtPct(s.avg_peak_pct)}
                </span>
                <span style={{ color: colors.textFaint }}>≥ 2x</span>
                <span style={{ fontFamily: typography.mono, textAlign: 'right', color: colors.success }}>{s.ge_2x}</span>
                <span style={{ color: colors.textFaint }}>≥ 5x</span>
                <span style={{ fontFamily: typography.mono, textAlign: 'right', color: colors.success }}>{s.ge_5x}</span>
                <span style={{ color: colors.textFaint }}>≥ 10x</span>
                <span style={{ fontFamily: typography.mono, textAlign: 'right', color: colors.success }}>{s.ge_10x}</span>
                <span style={{ color: colors.textFaint }}>Losers</span>
                <span style={{ fontFamily: typography.mono, textAlign: 'right', color: colors.danger }}>{s.losers}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      <div style={{ ...card, padding: 0 }}>
        <div style={{
          padding: `${spacing.md} ${spacing.lg}`,
          borderBottom: `1px solid ${colors.border}`,
        }}>
          <div style={sectionTitle}>Recent alerts</div>
        </div>
        <div style={{ maxHeight: 700, overflowY: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead style={{ position: 'sticky', top: 0, background: colors.bgElev1 }}>
              <tr>
                <th style={th}>Fired</th>
                <th style={th}>Type</th>
                <th style={th}>Mint / Symbol</th>
                <th style={th}>MC at alert</th>
                <th style={th}>Peak</th>
                <th style={th}>Peak %</th>
                <th style={th}>Now</th>
                <th style={th}>Drawdown</th>
                <th style={th}>Held</th>
              </tr>
            </thead>
            <tbody>
              {items.map(o => (
                <tr key={o.alert_id}>
                  <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>
                    {formatDate(o.fired_at)}
                  </td>
                  <td style={td}>
                    <span style={pill(typeVariant(o.alert_type))}>
                      {o.alert_type?.replace('_', ' ') || '—'}
                    </span>
                  </td>
                  <td style={td}>
                    <SolscanLink address={o.mint}>{o.mint_short}</SolscanLink>
                    {o.symbol && (
                      <span style={{ marginLeft: 8, color: colors.text, fontWeight: typography.medium }}>
                        {o.symbol}
                      </span>
                    )}
                  </td>
                  <td style={{ ...td, fontFamily: typography.mono }}>{fmtUsd(o.mc_at_alert)}</td>
                  <td style={{ ...td, fontFamily: typography.mono, color: colors.success }}>{fmtUsd(o.peak_mc_usd)}</td>
                  <td style={{ ...td, fontFamily: typography.mono, color: (o.peak_pct || 0) > 0 ? colors.success : colors.textFaint }}>
                    {fmtPct(o.peak_pct)}
                  </td>
                  <td style={{ ...td, fontFamily: typography.mono }}>{fmtUsd(o.current_mc_usd)}</td>
                  <td style={{ ...td, fontFamily: typography.mono, color: (o.drawdown_pct || 0) < 0 ? colors.danger : colors.textFaint }}>
                    {fmtPct(o.drawdown_pct)}
                  </td>
                  <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>
                    {o.holders_summary || '…'}
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr><td colSpan={9} style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
                  No alerts fired yet. Once the tracker finds convergences or snipers, they'll show up here with live outcome tracking.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default AlertHistory;
