import React, { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';
import {
  colors, typography, spacing, radius,
  card, pageTitle, pageSubtitle, sectionTitle,
  th, td, pill,
} from '../theme';

function SolscanLink({ address, children }) {
  if (!address) return children;
  return (
    <a
      href={`https://solscan.io/account/${address}`}
      target="_blank" rel="noreferrer"
      style={{
        color: colors.accent,
        textDecoration: 'none',
        fontFamily: typography.mono,
        fontSize: typography.small,
      }}
      onMouseEnter={(e) => e.currentTarget.style.textDecoration = 'underline'}
      onMouseLeave={(e) => e.currentTarget.style.textDecoration = 'none'}
    >
      {children}
    </a>
  );
}

function roleVariant(role) {
  switch (role) {
    case 'cabal_trader': return 'accent';
    case 'cabal_linked': return 'warning';
    case 'cabal_linked_2': return 'purple';
    default: return 'default';
  }
}

function confidenceBadge(score) {
  let color, label;
  if (score >= 10) { color = colors.success; label = 'ELITE'; }
  else if (score >= 5) { color = colors.accent; label = 'STRONG'; }
  else if (score >= 2) { color = colors.warning; label = 'SOLID'; }
  else { color = colors.textFaint; label = 'UNPROVEN'; }
  return (
    <span style={{
      display: 'inline-block',
      padding: '4px 10px',
      borderRadius: radius.pill,
      fontSize: typography.tiny,
      fontWeight: typography.semibold,
      background: `${color}18`,
      color: color,
      letterSpacing: 0.3,
      textTransform: 'uppercase',
    }}>
      {label} · {score.toFixed(1)}
    </span>
  );
}

function fmtUsd(v) {
  const n = Number(v) || 0;
  if (Math.abs(n) >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (Math.abs(n) >= 1_000) return `$${(n / 1_000).toFixed(1)}k`;
  return `$${n.toFixed(0)}`;
}

function Leaderboard() {
  const [minConfidence, setMinConfidence] = useState(0);
  const [snipersOnly, setSnipersOnly] = useState(false);
  const { data, loading } = useApi(
    `/wallets/solana/leaderboard?limit=100&min_confidence=${minConfidence}&snipers_only=${snipersOnly}`,
    { refreshInterval: 60000 }
  );
  const items = data?.items || [];

  return (
    <div>
      <h1 style={pageTitle}>Leaderboard</h1>
      <div style={pageSubtitle}>
        Tracked wallets ranked by a composite confidence score —
        logarithmic lifetime profit × win-rate bonus × recency decay.
        Solo Telegram alerts fire only when a wallet from this list
        buys a mint it's never held before.
      </div>

      <div style={{
        display: 'flex', gap: spacing.sm, marginBottom: spacing.lg,
        alignItems: 'center',
      }}>
        <span style={{ color: colors.textFaint, fontSize: typography.small }}>
          Min confidence:
        </span>
        {[0, 2, 5, 10].map(n => (
          <button
            key={n}
            onClick={() => setMinConfidence(n)}
            style={{
              background: minConfidence === n ? colors.accent : 'transparent',
              color: minConfidence === n ? '#fff' : colors.textDim,
              border: `1px solid ${minConfidence === n ? colors.accent : colors.borderStrong}`,
              padding: '6px 12px',
              borderRadius: radius.sm,
              fontSize: typography.small,
              fontWeight: typography.medium,
              cursor: 'pointer',
            }}
          >{n === 0 ? 'All' : `${n}+`}</button>
        ))}
        <button
          onClick={() => setSnipersOnly(!snipersOnly)}
          style={{
            marginLeft: spacing.md,
            background: snipersOnly ? colors.success : 'transparent',
            color: snipersOnly ? '#fff' : colors.textDim,
            border: `1px solid ${snipersOnly ? colors.success : colors.borderStrong}`,
            padding: '6px 12px',
            borderRadius: radius.sm,
            fontSize: typography.small,
            fontWeight: typography.medium,
            cursor: 'pointer',
          }}
        >🎯 Snipers only</button>
        <span style={{
          marginLeft: 'auto',
          color: colors.textFaint,
          fontSize: typography.small,
        }}>
          Showing {items.length} wallets
        </span>
      </div>

      <div style={card}>
        {loading && !items.length ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            Loading…
          </div>
        ) : items.length === 0 ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            No wallets meet this threshold yet. The stats aggregator
            needs at least one completed tracker cycle before scores
            populate.
          </div>
        ) : (
          <div style={{ maxHeight: 700, overflowY: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead style={{ position: 'sticky', top: 0, background: colors.bgElev1 }}>
                <tr>
                  <th style={{ ...th, width: 40 }}>#</th>
                  <th style={th}>Wallet</th>
                  <th style={th}>Role</th>
                  <th style={th}>Confidence</th>
                  <th style={th}>Net P/L</th>
                  <th style={th}>Wins / Losses</th>
                  <th style={th}>Win rate</th>
                  <th style={th}>Best Mint</th>
                  <th style={th}>Mints</th>
                  <th style={th}>Cluster</th>
                  <th style={th}>Last Active</th>
                </tr>
              </thead>
              <tbody>
                {items.map((w, i) => (
                  <tr key={w.wallet_address}>
                    <td style={{ ...td, color: colors.textFaint }}>{i + 1}</td>
                    <td style={td}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <SolscanLink address={w.wallet_address}>
                          {w.wallet_short}
                        </SolscanLink>
                        {w.is_sniper && (
                          <span title={`Sniper — avg $${(w.avg_buy_size_usd || 0).toFixed(0)} entry, ${(w.avg_exit_multiplier || 0).toFixed(1)}x avg exit`}
                                style={{
                                  background: colors.successSoft,
                                  color: colors.success,
                                  borderRadius: radius.pill,
                                  padding: '2px 6px',
                                  fontSize: 10,
                                  fontWeight: typography.semibold,
                                }}>
                            🎯 SNIPER
                          </span>
                        )}
                      </div>
                    </td>
                    <td style={td}>
                      {w.role ? (
                        <span style={pill(roleVariant(w.role))}>{w.role}</span>
                      ) : <span style={{ color: colors.textMuted }}>—</span>}
                    </td>
                    <td style={td}>{confidenceBadge(w.confidence_score)}</td>
                    <td style={{ ...td, fontFamily: typography.mono, color: w.net_profit_usd >= 0 ? colors.success : colors.danger }}>
                      {w.net_profit_usd > 0 ? '+' : ''}{fmtUsd(w.net_profit_usd)}
                    </td>
                    <td style={{ ...td, fontFamily: typography.mono, fontSize: typography.small }}>
                      <span style={{ color: colors.success }}>{w.win_count}</span>
                      {' / '}
                      <span style={{ color: colors.danger }}>{w.loss_count}</span>
                    </td>
                    <td style={{ ...td, fontFamily: typography.mono, color: colors.textDim }}>
                      {w.win_rate == null ? '—' : `${(w.win_rate * 100).toFixed(0)}%`}
                    </td>
                    <td style={td}>
                      {w.best_mint ? (
                        <span>
                          <SolscanLink address={w.best_mint}>{w.best_mint_short}</SolscanLink>
                          <span style={{
                            marginLeft: 8, fontFamily: typography.mono,
                            color: colors.success, fontSize: typography.small,
                          }}>
                            +{fmtUsd(w.best_mint_profit_usd)}
                          </span>
                        </span>
                      ) : <span style={{ color: colors.textMuted }}>—</span>}
                    </td>
                    <td style={{ ...td, fontFamily: typography.mono, fontSize: typography.small, color: colors.textDim }}>
                      {w.mints_traded}
                    </td>
                    <td style={td}>
                      {w.entity_size > 1 ? (
                        <span style={{
                          ...pill('purple'),
                          fontSize: 9,
                          padding: '2px 8px',
                        }}>
                          {w.entity_size} linked
                        </span>
                      ) : <span style={{ color: colors.textMuted }}>solo</span>}
                    </td>
                    <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>
                      {formatDate(w.last_activity_at)}
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

export default Leaderboard;
