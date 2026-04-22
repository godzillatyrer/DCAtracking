import React, { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';
import {
  colors, typography, spacing, radius,
  card, pageTitle, pageSubtitle, sectionTitle,
  th, td, pill,
} from '../theme';

function short(addr) {
  if (!addr) return null;
  return addr.length > 12 ? `${addr.slice(0, 6)}…${addr.slice(-4)}` : addr;
}

function SolscanLink({ address, children }) {
  if (!address) return children;
  return (
    <a
      href={`https://solscan.io/account/${address}`}
      target="_blank"
      rel="noreferrer"
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

function Section({ title, subtitle, children, accent = false }) {
  return (
    <div style={{
      ...card,
      padding: 0,
      marginBottom: spacing.xl,
      borderColor: accent ? colors.accent : colors.border,
      boxShadow: accent ? '0 0 24px rgba(0,102,255,0.08)' : undefined,
    }}>
      <div style={{
        padding: `${spacing.md} ${spacing.lg}`,
        borderBottom: children ? `1px solid ${colors.border}` : 'none',
      }}>
        <div style={{
          ...sectionTitle,
          marginBottom: subtitle ? spacing.xs : 0,
        }}>
          {title}
        </div>
        {subtitle && (
          <div style={{ fontSize: typography.small, color: colors.textFaint }}>
            {subtitle}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

function confidenceLabel(score) {
  if (score >= 3.0) return { text: 'HIGH', color: colors.success };
  if (score >= 2.0) return { text: 'MEDIUM', color: colors.warning };
  return { text: 'LOW', color: colors.textFaint };
}

function ConvergenceCard({ cluster }) {
  const { text: confText, color: confColor } = confidenceLabel(cluster.score);
  return (
    <div style={{
      padding: `${spacing.md} ${spacing.lg}`,
      borderBottom: `1px solid ${colors.border}`,
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'baseline',
        gap: spacing.md,
        marginBottom: spacing.sm,
      }}>
        <div style={{
          fontSize: typography.h3,
          fontWeight: typography.semibold,
          fontFamily: typography.mono,
        }}>
          <SolscanLink address={cluster.mint}>{cluster.mint_short}</SolscanLink>
        </div>
        <div style={{
          ...pill('accent'),
          background: `${confColor}20`,
          color: confColor,
        }}>
          {confText} · score {cluster.score}
        </div>
        <div style={{ color: colors.textFaint, fontSize: typography.small }}>
          {cluster.wallet_count} wallets · ${cluster.total_value_usd?.toLocaleString(undefined, { maximumFractionDigits: 0 })}
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: spacing.sm }}>
          <a
            href={`https://dexscreener.com/solana/${cluster.mint}`}
            target="_blank" rel="noreferrer"
            style={{
              color: colors.accent,
              fontSize: typography.small,
              textDecoration: 'none',
              fontWeight: typography.medium,
            }}
          >DEX Screener</a>
          <a
            href={`https://gmgn.ai/sol/token/${cluster.mint}`}
            target="_blank" rel="noreferrer"
            style={{
              color: colors.accent,
              fontSize: typography.small,
              textDecoration: 'none',
              fontWeight: typography.medium,
            }}
          >GMGN</a>
        </div>
      </div>
      <div style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: spacing.xs,
        marginTop: spacing.sm,
      }}>
        {cluster.wallets.map(w => (
          <div key={w.wallet_address} style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            background: colors.bgSubtle,
            border: `1px solid ${colors.border}`,
            borderRadius: radius.pill,
            padding: '4px 10px',
            fontSize: typography.tiny,
          }}>
            <span style={{ ...pill(roleVariant(w.role)), padding: '2px 6px', fontSize: 9 }}>
              {w.role?.replace('cabal_', '') || 'unknown'}
            </span>
            <SolscanLink address={w.wallet_address}>{w.wallet_short}</SolscanLink>
            {w.value_usd > 0 && (
              <span style={{ color: colors.textFaint, fontFamily: typography.mono }}>
                ${w.value_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}
              </span>
            )}
          </div>
        ))}
      </div>
      <div style={{ marginTop: spacing.xs, color: colors.textMuted, fontSize: typography.tiny }}>
        First buy {formatDate(cluster.first_buy)} · Last buy {formatDate(cluster.last_buy)}
      </div>
    </div>
  );
}

function WindowSelector({ value, onChange }) {
  const options = [
    [30, '30m'],
    [60, '1h'],
    [180, '3h'],
    [360, '6h'],
    [1440, '24h'],
  ];
  return (
    <div style={{ display: 'flex', gap: spacing.xs }}>
      {options.map(([v, label]) => (
        <button
          key={v}
          onClick={() => onChange(v)}
          style={{
            background: value === v ? colors.accent : 'transparent',
            color: value === v ? '#fff' : colors.textDim,
            border: `1px solid ${value === v ? colors.accent : colors.borderStrong}`,
            padding: '6px 12px',
            borderRadius: radius.sm,
            fontSize: typography.small,
            fontWeight: typography.medium,
            cursor: 'pointer',
          }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function LiveActivity() {
  const [window, setWindow] = useState(60);
  const [minWallets, setMinWallets] = useState(2);

  const { data: conv, loading: convLoading } = useApi(
    `/wallets/solana/convergence?window_minutes=${window}&min_wallets=${minWallets}`,
    { refreshInterval: 30000 }
  );
  const { data: feed } = useApi(
    `/wallets/solana/activity?limit=50&since_minutes=${window}&only_buys=true`,
    { refreshInterval: 30000 }
  );

  const clusters = conv?.clusters || [];
  const activity = feed?.items || [];

  return (
    <div>
      <h1 style={pageTitle}>Live Activity</h1>
      <div style={pageSubtitle}>
        Real-time view of what tracked cabal wallets are buying. When
        multiple wallets converge on the same new mint, the cluster
        score rises — those are your highest-conviction signals.
      </div>

      <div style={{
        display: 'flex',
        gap: spacing.lg,
        alignItems: 'center',
        marginBottom: spacing.lg,
        flexWrap: 'wrap',
      }}>
        <div>
          <div style={{ fontSize: typography.tiny, color: colors.textFaint, marginBottom: 6, textTransform: 'uppercase', letterSpacing: 1 }}>
            Window
          </div>
          <WindowSelector value={window} onChange={setWindow} />
        </div>
        <div>
          <div style={{ fontSize: typography.tiny, color: colors.textFaint, marginBottom: 6, textTransform: 'uppercase', letterSpacing: 1 }}>
            Min wallets
          </div>
          <div style={{ display: 'flex', gap: spacing.xs }}>
            {[2, 3, 5].map(n => (
              <button
                key={n}
                onClick={() => setMinWallets(n)}
                style={{
                  background: minWallets === n ? colors.accent : 'transparent',
                  color: minWallets === n ? '#fff' : colors.textDim,
                  border: `1px solid ${minWallets === n ? colors.accent : colors.borderStrong}`,
                  padding: '6px 12px',
                  borderRadius: radius.sm,
                  fontSize: typography.small,
                  fontWeight: typography.medium,
                  cursor: 'pointer',
                  minWidth: 40,
                }}
              >{n}+</button>
            ))}
          </div>
        </div>
      </div>

      <Section
        title="Convergence"
        subtitle={`Mints bought by ${minWallets}+ tracked wallets in the last ${window < 60 ? `${window}m` : `${Math.round(window / 60)}h`}. Score weights by role (trader 1.0, linked 0.7, depth-2 0.5).`}
        accent={clusters.length > 0}
      >
        {convLoading && clusters.length === 0 ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            Loading…
          </div>
        ) : clusters.length === 0 ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            No convergence yet. Tracker polls every 5 min — either no
            tracked wallets are buying anything, or not enough have
            converged on the same mint in this window.
          </div>
        ) : (
          clusters.map(c => <ConvergenceCard key={c.mint} cluster={c} />)
        )}
      </Section>

      <Section
        title="Recent Buys"
        subtitle={`All SPL buys by tracked wallets in the last ${window < 60 ? `${window}m` : `${Math.round(window / 60)}h`} · ${activity.length} shown`}
      >
        {activity.length === 0 ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            No activity in this window yet. Wait for the next tracker run.
          </div>
        ) : (
          <div style={{ maxHeight: 560, overflowY: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead style={{ position: 'sticky', top: 0, background: colors.bgElev1 }}>
                <tr>
                  <th style={th}>Wallet</th>
                  <th style={th}>Role</th>
                  <th style={th}>Mint</th>
                  <th style={th}>Value</th>
                  <th style={th}>New?</th>
                  <th style={th}>When</th>
                </tr>
              </thead>
              <tbody>
                {activity.map(a => (
                  <tr key={a.signature + ':' + a.wallet_address}>
                    <td style={td}>
                      <SolscanLink address={a.wallet_address}>
                        {a.wallet_short}
                      </SolscanLink>
                    </td>
                    <td style={td}>
                      {a.wallet_role ? (
                        <span style={pill(roleVariant(a.wallet_role))}>
                          {a.wallet_role}
                        </span>
                      ) : '—'}
                    </td>
                    <td style={td}>
                      <SolscanLink address={a.token_mint}>
                        {a.token_mint_short}
                      </SolscanLink>
                    </td>
                    <td style={{ ...td, fontFamily: typography.mono, color: colors.textDim }}>
                      {a.value_usd && Number(a.value_usd) > 0
                        ? `$${Number(a.value_usd).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
                        : '—'}
                    </td>
                    <td style={td}>
                      {a.is_new_token ? (
                        <span style={pill('success')}>NEW</span>
                      ) : null}
                    </td>
                    <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>
                      {formatDate(a.detected_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}

export default LiveActivity;
