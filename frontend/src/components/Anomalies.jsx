import React, { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';
import {
  colors, typography, spacing, radius,
  card, pageTitle, pageSubtitle, sectionTitle, th, td, pill,
} from '../theme';

function fmtUsd(n) {
  if (n == null) return '—';
  const v = Number(n) || 0;
  if (Math.abs(v) >= 1_000_000) return `$${(v/1_000_000).toFixed(2)}M`;
  if (Math.abs(v) >= 1_000) return `$${(v/1_000).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}

function shortAddr(a) {
  if (!a) return '—';
  return a.length > 12 ? `${a.slice(0,6)}…${a.slice(-4)}` : a;
}

function sideBadge(side) {
  if (side === 'long')  return { bg: colors.successSoft, color: colors.success, label: 'LONG' };
  if (side === 'short') return { bg: colors.dangerSoft,  color: colors.danger,  label: 'SHORT' };
  if (side === 'buy')   return { bg: colors.successSoft, color: colors.success, label: 'BUY' };
  if (side === 'sell')  return { bg: colors.dangerSoft,  color: colors.danger,  label: 'SELL' };
  return { bg: colors.bgSubtle, color: colors.textFaint, label: side?.toUpperCase() || '—' };
}

function HlAddrLink({ address, children }) {
  if (!address) return children || '—';
  return (
    <a href={`https://app.hyperliquid.xyz/explorer/address/${address}`}
       target="_blank" rel="noreferrer"
       style={{
         color: colors.accent, textDecoration: 'none',
         fontFamily: typography.mono, fontSize: typography.small,
       }}>{children || shortAddr(address)}</a>
  );
}

function CoinLink({ source, coin }) {
  if (!coin) return '—';
  if (source === 'hyperliquid') {
    return (
      <a href={`https://app.hyperliquid.xyz/trade/${coin}`}
         target="_blank" rel="noreferrer"
         style={{ color: colors.text, fontWeight: typography.semibold, textDecoration: 'none' }}>
        {coin}
      </a>
    );
  }
  return <span>{coin}</span>;
}

function SourceFilter({ value, onChange, sources }) {
  return (
    <div style={{ display: 'flex', gap: spacing.xs }}>
      <button
        onClick={() => onChange('')}
        style={{
          background: !value ? colors.accent : 'transparent',
          color: !value ? '#fff' : colors.textDim,
          border: `1px solid ${!value ? colors.accent : colors.borderStrong}`,
          padding: '6px 12px',
          borderRadius: radius.sm,
          fontSize: typography.small,
          fontWeight: typography.medium,
          cursor: 'pointer',
        }}
      >All</button>
      {sources.map(s => (
        <button
          key={s}
          onClick={() => onChange(s)}
          style={{
            background: value === s ? colors.accent : 'transparent',
            color: value === s ? '#fff' : colors.textDim,
            border: `1px solid ${value === s ? colors.accent : colors.borderStrong}`,
            padding: '6px 12px',
            borderRadius: radius.sm,
            fontSize: typography.small,
            fontWeight: typography.medium,
            cursor: 'pointer',
          }}
        >{s}</button>
      ))}
    </div>
  );
}

function Anomalies() {
  const [source, setSource] = useState('');
  const [onlyAlerted, setOnlyAlerted] = useState(false);
  const params = new URLSearchParams();
  params.set('limit', 100);
  if (source) params.set('source', source);
  if (onlyAlerted) params.set('only_alerted', 'true');
  const { data, loading } = useApi(`/anomalies?${params.toString()}`,
    { refreshInterval: 30000 });
  const items = data?.items || [];

  return (
    <div>
      <h1 style={pageTitle}>Anomalies</h1>
      <div style={pageSubtitle}>
        Real-time on-chain whale events from external watchers. Currently
        Hyperliquid; Solana DEX whale swaps and EVM whale flows arrive next.
        Auto-refresh 30s.
      </div>

      <div style={{
        display: 'flex', gap: spacing.lg, alignItems: 'end',
        marginBottom: spacing.lg, flexWrap: 'wrap',
      }}>
        <div>
          <div style={{
            fontSize: typography.tiny, color: colors.textFaint,
            marginBottom: 6, textTransform: 'uppercase', letterSpacing: 1,
          }}>Source</div>
          <SourceFilter
            value={source} onChange={setSource}
            sources={['hyperliquid']}
          />
        </div>
        <button
          onClick={() => setOnlyAlerted(!onlyAlerted)}
          style={{
            background: onlyAlerted ? colors.accent : 'transparent',
            color: onlyAlerted ? '#fff' : colors.textDim,
            border: `1px solid ${onlyAlerted ? colors.accent : colors.borderStrong}`,
            padding: '6px 12px', borderRadius: radius.sm,
            fontSize: typography.small, fontWeight: typography.medium,
            cursor: 'pointer',
          }}
        >Alerted only</button>
        <span style={{ marginLeft: 'auto', color: colors.textFaint, fontSize: typography.small }}>
          {items.length} events
        </span>
      </div>

      <div style={{ ...card, padding: 0 }}>
        {loading && items.length === 0 ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            Loading…
          </div>
        ) : items.length === 0 ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            Nothing yet. Hyperliquid watcher polls every minute — once a
            whale trade ≥ the threshold lands on a non-major coin, it'll
            show here.
          </div>
        ) : (
          <div style={{ maxHeight: 720, overflowY: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead style={{ position: 'sticky', top: 0, background: colors.bgElev1 }}>
                <tr>
                  <th style={th}>When</th>
                  <th style={th}>Source</th>
                  <th style={th}>Coin</th>
                  <th style={th}>Side</th>
                  <th style={th}>Size</th>
                  <th style={th}>Wallet</th>
                  <th style={th}>Fresh</th>
                  <th style={th}>Alert</th>
                </tr>
              </thead>
              <tbody>
                {items.map(a => {
                  const sb = sideBadge(a.side);
                  return (
                    <tr key={a.id}>
                      <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>
                        {formatDate(a.detected_at)}
                      </td>
                      <td style={td}>
                        <span style={pill('purple')}>{a.source}</span>
                      </td>
                      <td style={td}>
                        <CoinLink source={a.source} coin={a.coin} />
                      </td>
                      <td style={td}>
                        <span style={{
                          ...pill('default'),
                          background: sb.bg, color: sb.color,
                        }}>{sb.label}</span>
                      </td>
                      <td style={{ ...td, fontFamily: typography.mono, fontWeight: typography.semibold }}>
                        {fmtUsd(a.notional_usd)}
                      </td>
                      <td style={td}>
                        <HlAddrLink address={a.actor_address}>
                          {a.actor_short}
                        </HlAddrLink>
                      </td>
                      <td style={td}>
                        {a.is_fresh_wallet ? (
                          <span title={`${a.actor_history_count} prior fills`}
                                style={{
                                  ...pill('success'),
                                  background: colors.successSoft,
                                  color: colors.success,
                                }}>🆕 FRESH</span>
                        ) : (
                          <span style={{ color: colors.textMuted, fontSize: typography.small, fontFamily: typography.mono }}>
                            {a.actor_history_count != null ? `${a.actor_history_count} fills` : '—'}
                          </span>
                        )}
                      </td>
                      <td style={td}>
                        {a.is_alerted ? (
                          <span style={pill('accent')}>SENT</span>
                        ) : (
                          <span style={{ color: colors.textMuted, fontSize: typography.tiny }}>
                            (deduped)
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export default Anomalies;
