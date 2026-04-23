import React, { useState } from 'react';
import { useApi, apiPost } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';
import {
  colors, typography, spacing, radius,
  card, pageTitle, pageSubtitle, sectionTitle,
  buttonPrimary, input, th, td, pill,
} from '../theme';

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
      {children && <div style={{ padding: 0 }}>{children}</div>}
    </div>
  );
}

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

function CabalExtractor({ onSuccess }) {
  const [mint, setMint] = useState('');
  const [symbol, setSymbol] = useState('');
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);

  async function extract(e) {
    e.preventDefault();
    if (!mint.trim()) return;
    setLoading(true);
    setResult(null);
    try {
      const r = await apiPost('/wallets/solana/extract-from-runner', {
        mint: mint.trim(),
        symbol: symbol.trim() || '',
        auto_add: true,
      });
      setResult(r);
      if (r.wallets_added > 0) onSuccess?.();
    } catch (err) {
      setResult({ error: err.message });
    }
    setLoading(false);
  }

  return (
    <div style={{
      ...card,
      marginBottom: spacing.xl,
      background: `linear-gradient(135deg, ${colors.bgElev1} 0%, ${colors.bgElev2} 100%)`,
      border: `1px solid ${colors.accent}30`,
      boxShadow: '0 0 32px rgba(0,102,255,0.08)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: spacing.sm, marginBottom: spacing.sm }}>
        <span style={{ color: colors.accent, fontSize: 20 }}>◆</span>
        <div style={{
          fontSize: typography.h3,
          fontWeight: typography.semibold,
          color: colors.text,
          letterSpacing: '-0.01em',
        }}>
          Extract Cabal from a Runner
        </div>
      </div>
      <div style={{
        fontSize: typography.body,
        color: colors.textDim,
        marginBottom: spacing.lg,
        lineHeight: 1.5,
      }}>
        Paste the mint address of a Solana token that pumped. The extractor merges
        GMGN top traders/holders with Helius transaction history, identifies early
        buyers, and auto-adds them to the watchlist. The graph walk then follows
        their money as they rotate into fresh wallets.
      </div>

      <form onSubmit={extract} style={{
        display: 'flex',
        gap: spacing.sm,
        flexWrap: 'wrap',
        alignItems: 'stretch',
      }}>
        <input
          type="text"
          value={mint}
          onChange={(e) => setMint(e.target.value)}
          placeholder="Solana token mint address (CA)"
          style={{
            ...input,
            flex: '1 1 420px',
            minWidth: 240,
            fontFamily: typography.mono,
          }}
        />
        <input
          type="text"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="Symbol (optional)"
          style={{
            ...input,
            width: 160,
          }}
        />
        <button
          type="submit"
          disabled={loading || !mint.trim()}
          style={{
            ...buttonPrimary,
            minWidth: 140,
            opacity: loading || !mint.trim() ? 0.5 : 1,
            cursor: loading || !mint.trim() ? 'default' : 'pointer',
          }}
        >
          {loading ? 'Extracting…' : 'Extract Wallets'}
        </button>
      </form>

      {result && (
        <div style={{ marginTop: spacing.lg }}>
          {result.error ? (
            <div style={{ color: colors.danger, fontSize: typography.body }}>
              Error: {result.error}
            </div>
          ) : (
            <>
              <div style={{ display: 'flex', gap: spacing.lg, marginBottom: spacing.md, alignItems: 'baseline' }}>
                <div>
                  <span style={{ fontSize: typography.h1, fontWeight: typography.bold, color: colors.accent }}>
                    {result.wallets_found}
                  </span>
                  <span style={{ fontSize: typography.small, color: colors.textFaint, marginLeft: 6 }}>
                    wallets found
                  </span>
                </div>
                <div>
                  <span style={{ fontSize: typography.h1, fontWeight: typography.bold, color: colors.success }}>
                    {result.wallets_added}
                  </span>
                  <span style={{ fontSize: typography.small, color: colors.textFaint, marginLeft: 6 }}>
                    added to watchlist
                  </span>
                </div>
              </div>
              {result.wallets?.length > 0 && (
                <div style={{
                  maxHeight: 360,
                  overflowY: 'auto',
                  border: `1px solid ${colors.border}`,
                  borderRadius: radius.sm,
                }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                    <thead style={{ position: 'sticky', top: 0 }}>
                      <tr>
                        <th style={th}>#</th>
                        <th style={th}>Address</th>
                        <th style={th}>Profit</th>
                        <th style={th}>Multiple</th>
                        <th style={th}>Balance</th>
                        <th style={th}>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.wallets.map((w, i) => (
                        <tr key={w.address}>
                          <td style={{ ...td, color: colors.textFaint }}>{i + 1}</td>
                          <td style={td}>
                            <SolscanLink address={w.address}>
                              {short(w.address)}
                            </SolscanLink>
                          </td>
                          <td style={{ ...td, fontFamily: typography.mono, color: w.total_profit_usd > 0 ? colors.success : colors.textDim }}>
                            ${w.total_profit_usd?.toLocaleString(undefined, { maximumFractionDigits: 0 }) || '—'}
                          </td>
                          <td style={{ ...td, fontFamily: typography.mono, color: colors.text }}>
                            {w.profit_multiplier ? `${w.profit_multiplier}x` : '—'}
                          </td>
                          <td style={{ ...td, fontFamily: typography.mono, color: colors.textDim }}>
                            ${w.balance_usd?.toLocaleString(undefined, { maximumFractionDigits: 0 }) || '—'}
                          </td>
                          <td style={td}>
                            {w.fully_exited ? (
                              <span style={pill('accent')}>exited</span>
                            ) : w.still_holding ? (
                              <span style={pill('warning')}>holding</span>
                            ) : (
                              <span style={pill()}>active</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function TrackedRow({ w }) {
  const origin = (() => {
    if (w.associated_token) {
      return (
        <span style={{ color: colors.text, fontWeight: typography.medium }}>
          {w.associated_token}
        </span>
      );
    }
    if (w.associated_mint) {
      return (
        <span style={{ color: colors.textFaint }}>
          via{' '}
          <SolscanLink address={w.associated_mint}>
            {w.associated_mint_short}
          </SolscanLink>
        </span>
      );
    }
    if (w.funding_source) {
      return (
        <span style={{ color: colors.textFaint }}>
          funded by{' '}
          <SolscanLink address={w.funding_source}>
            {w.funding_source_short}
          </SolscanLink>
        </span>
      );
    }
    return <span style={{ color: colors.textMuted }}>—</span>;
  })();

  const conf = w.confidence_score;
  const confColor =
    conf == null ? colors.textMuted :
    conf >= 10 ? colors.success :
    conf >= 5 ? colors.accent :
    conf >= 2 ? colors.warning : colors.textFaint;

  return (
    <tr>
      <td style={td}>
        <SolscanLink address={w.wallet_address}>
          {w.wallet_short}
        </SolscanLink>
      </td>
      <td style={td}>
        <span style={pill(roleVariant(w.role))}>{w.role}</span>
      </td>
      <td style={{ ...td, fontFamily: typography.mono, color: confColor }}>
        {conf == null ? '—' : `★${Number(conf).toFixed(1)}`}
      </td>
      <td style={{ ...td, fontSize: typography.small }}>{origin}</td>
      <td style={{ ...td, fontFamily: typography.mono, color: (w.net_profit_usd ?? 0) >= 0 ? colors.success : colors.danger }}>
        {w.net_profit_usd != null
          ? `${w.net_profit_usd >= 0 ? '+' : ''}$${Number(w.net_profit_usd).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
          : (w.total_profit_est
              ? `$${Number(w.total_profit_est).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
              : '—')}
      </td>
      <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>
        {formatDate(w.added_at)}
      </td>
    </tr>
  );
}

function RoleFilter({ value, onChange, counts }) {
  const items = [
    ['all', 'All', counts.all],
    ['cabal_trader', 'Cabal Trader', counts.cabal_trader],
    ['cabal_linked', 'Cabal Linked', counts.cabal_linked],
    ['cabal_linked_2', 'Linked (Depth 2)', counts.cabal_linked_2],
  ];
  return (
    <div style={{
      display: 'flex',
      gap: spacing.xs,
      padding: `${spacing.sm} ${spacing.lg}`,
      borderBottom: `1px solid ${colors.border}`,
      background: colors.bgSubtle,
    }}>
      {items.map(([key, label, count]) => (
        <button
          key={key}
          onClick={() => onChange(key)}
          style={{
            background: value === key ? colors.accent : 'transparent',
            color: value === key ? '#fff' : colors.textDim,
            border: 'none',
            padding: '6px 12px',
            borderRadius: radius.sm,
            fontSize: typography.small,
            fontWeight: typography.medium,
            cursor: 'pointer',
          }}
        >
          {label}{' '}
          <span style={{ opacity: 0.65, fontSize: typography.tiny }}>
            {count}
          </span>
        </button>
      ))}
    </div>
  );
}

function WalletTracker() {
  const { data: solWallets, refetch: refetchSolWallets } = useApi(
    '/wallets/solana/known?per_page=200', { refreshInterval: 60000 }
  );
  const [roleFilter, setRoleFilter] = useState('all');

  const items = solWallets?.items || [];
  const counts = {
    all: items.length,
    cabal_trader: items.filter(w => w.role === 'cabal_trader').length,
    cabal_linked: items.filter(w => w.role === 'cabal_linked').length,
    cabal_linked_2: items.filter(w => w.role === 'cabal_linked_2').length,
  };
  const visible = roleFilter === 'all'
    ? items
    : items.filter(w => w.role === roleFilter);

  return (
    <div>
      <h1 style={pageTitle}>Cabal Tracker</h1>
      <div style={pageSubtitle}>
        Extract cabal wallets from runner CAs. Tracked wallets are then
        polled every few minutes — when multiple buy the same new mint,
        the convergence shows up in Live Activity.
      </div>

      <CabalExtractor onSuccess={refetchSolWallets} />

      <Section
        title="Tracked Solana Wallets"
        subtitle={`${solWallets?.total || 0} wallets across all runners you've extracted.`}
      >
        <RoleFilter value={roleFilter} onChange={setRoleFilter} counts={counts} />
        {visible.length ? (
          <div style={{ maxHeight: 560, overflowY: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead style={{ position: 'sticky', top: 0, background: colors.bgElev1 }}>
                <tr>
                  <th style={th}>Wallet</th>
                  <th style={th}>Role</th>
                  <th style={th}>Confidence</th>
                  <th style={th}>Origin</th>
                  <th style={th}>Net P/L</th>
                  <th style={th}>Added</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((w) => (
                  <TrackedRow key={w.wallet_address} w={w} />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            {items.length
              ? `No wallets match "${roleFilter}".`
              : 'No wallets tracked yet. Extract from a runner above to get started.'}
          </div>
        )}
      </Section>
    </div>
  );
}

export default WalletTracker;
