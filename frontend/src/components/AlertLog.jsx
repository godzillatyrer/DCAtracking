import React from 'react';
import { useApi } from '../hooks/useApi';
import { shortAddress, formatDate } from '../utils/formatters';
import {
  colors, typography, spacing,
  card, pageTitle, pageSubtitle, sectionTitle,
  th, td, pill, metricValue, metricLabel,
} from '../theme';

function Metric({ label, value, color }) {
  return (
    <div style={{ ...card, flex: 1, minWidth: 140 }}>
      <div style={metricLabel}>{label}</div>
      <div style={{ ...metricValue, color: color || colors.text, fontSize: typography.h1 }}>
        {value}
      </div>
    </div>
  );
}

function AlertLog() {
  const { data: alerts } = useApi('/dashboard/alerts?per_page=100', { refreshInterval: 60000 });
  const { data: stats } = useApi('/dashboard/stats', { refreshInterval: 60000 });

  const items = alerts?.items || [];
  const s = stats || {};

  return (
    <div>
      <h1 style={pageTitle}>Alert History</h1>
      <div style={pageSubtitle}>
        Every Telegram alert fired, with outcomes for accuracy tracking.
      </div>

      <div style={{ display: 'flex', gap: spacing.md, marginBottom: spacing.xl, flexWrap: 'wrap' }}>
        <Metric label="Total Alerts" value={(s.total_alerts ?? 0).toLocaleString()} />
        <Metric label="Reviewed" value={(s.total_reviewed ?? 0).toLocaleString()} />
        <Metric label="Wins" value={(s.wins ?? 0).toLocaleString()} color={colors.success} />
        <Metric label="Fizzles" value={(s.fizzles ?? 0).toLocaleString()} color={colors.danger} />
        <Metric label="Win Rate" value={`${s.win_rate ?? 0}%`} color={colors.accent} />
      </div>

      <div style={{ ...card, padding: 0, overflow: 'hidden' }}>
        <div style={{
          padding: `${spacing.md} ${spacing.lg}`,
          borderBottom: `1px solid ${colors.border}`,
        }}>
          <div style={sectionTitle}>Recent Alerts</div>
          <div style={{ fontSize: typography.small, color: colors.textFaint }}>
            {items.length} alert{items.length === 1 ? '' : 's'}
          </div>
        </div>
        {items.length === 0 ? (
          <div style={{ padding: '64px 24px', textAlign: 'center', color: colors.textFaint }}>
            No alerts yet.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={th}>Score</th>
                  <th style={th}>Token</th>
                  <th style={th}>Trigger</th>
                  <th style={th}>Price</th>
                  <th style={th}>Outcome</th>
                  <th style={th}>Fired</th>
                </tr>
              </thead>
              <tbody>
                {items.map((a) => (
                  <tr key={a.id}>
                    <td style={td}>
                      <span style={pill(
                        a.score_at_alert >= 70 ? 'danger' :
                        a.score_at_alert >= 50 ? 'warning' : 'default'
                      )}>
                        {a.score_at_alert}
                      </span>
                    </td>
                    <td style={td}>
                      <div style={{ color: colors.text, fontWeight: typography.medium }}>
                        {a.token_symbol || shortAddress(a.contract_address)}
                      </div>
                      <div style={{ color: colors.textFaint, fontSize: typography.tiny, fontFamily: typography.mono, marginTop: 2 }}>
                        {shortAddress(a.contract_address)}
                      </div>
                    </td>
                    <td style={{ ...td, color: colors.textDim, fontSize: typography.small, maxWidth: 400 }}>
                      {a.trigger_reason || '—'}
                    </td>
                    <td style={{ ...td, fontFamily: typography.mono, color: colors.textDim }}>
                      {a.price_at_alert ? `$${a.price_at_alert}` : '—'}
                    </td>
                    <td style={td}>
                      {a.outcome === 'pumped' ? <span style={pill('success')}>PUMPED</span> :
                       a.outcome === 'fizzled' ? <span style={pill('danger')}>FIZZLED</span> :
                       <span style={{ color: colors.textMuted, fontSize: typography.small }}>pending</span>}
                    </td>
                    <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>
                      {formatDate(a.fired_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export default AlertLog;
