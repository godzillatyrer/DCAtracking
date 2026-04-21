import React, { useState } from 'react';
import { useApi, apiPost } from '../hooks/useApi';
import { shortAddress, formatDate } from '../utils/formatters';
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
      boxShadow: accent ? '0 0 24px rgba(59,130,246,0.08)' : undefined,
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
      {children && <div style={{ padding: spacing.lg }}>{children}</div>}
    </div>
  );
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
      boxShadow: '0 0 32px rgba(59,130,246,0.08)',
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
        Paste the mint address of a Solana token that just pumped. The system parses
        on-chain transaction history to identify every early buyer and seller, computes
        their realized &amp; unrealized PnL, and auto-adds the profitable wallets to your
        watchlist. The graph walk then follows their money as they rotate into new wallets.
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
                        <th style={th}>Cost</th>
                        <th style={th}>Balance</th>
                        <th style={th}>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.wallets.map((w, i) => (
                        <tr key={w.address}>
                          <td style={{ ...td, color: colors.textFaint }}>{i + 1}</td>
                          <td style={{ ...td, fontFamily: typography.mono, fontSize: typography.small }}>
                            {w.address.slice(0, 8)}…{w.address.slice(-6)}
                          </td>
                          <td style={{ ...td, fontFamily: typography.mono, color: w.total_profit_usd > 0 ? colors.success : colors.textDim }}>
                            ${w.total_profit_usd?.toLocaleString(undefined, { maximumFractionDigits: 0 }) || '—'}
                          </td>
                          <td style={{ ...td, fontFamily: typography.mono, color: colors.text }}>
                            {w.profit_multiplier ? `${w.profit_multiplier}x` : '—'}
                          </td>
                          <td style={{ ...td, fontFamily: typography.mono, color: colors.textDim }}>
                            ${w.cost_usd?.toLocaleString(undefined, { maximumFractionDigits: 0 }) || '—'}
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

function WalletTracker() {
  const { data: wallets, refetch: refetchWallets } = useApi('/wallets/known?per_page=100', { refreshInterval: 60000 });
  const { data: newAccum } = useApi('/wallets/new-accumulations?limit=20', { refreshInterval: 30000 });
  const { data: solWallets, refetch: refetchSolWallets } = useApi('/wallets/solana/known?per_page=100', { refreshInterval: 60000 });
  const [selectedWallet, setSelectedWallet] = useState(null);
  const { data: activity } = useApi(
    `/wallets/${selectedWallet}/activity?per_page=50&flagged_only=true`,
    { enabled: !!selectedWallet, refreshInterval: 30000 }
  );

  return (
    <div>
      <h1 style={pageTitle}>Wallet Tracker</h1>
      <div style={pageSubtitle}>
        On-chain wallet monitoring for known pump-and-dump operators.
        When they move, you get alerted.
      </div>

      <CabalExtractor onSuccess={refetchSolWallets} />

      {newAccum && newAccum.length > 0 && (
        <Section
          title="New Token Accumulations"
          subtitle="When a known operator buys a token they didn't hold before — highest-priority signal."
          accent
        >
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={th}>Operator</th>
                <th style={th}>From Token</th>
                <th style={th}>New Token</th>
                <th style={th}>Amount</th>
                <th style={th}>Detected</th>
              </tr>
            </thead>
            <tbody>
              {newAccum.map((a, i) => (
                <tr key={i}>
                  <td style={td}>
                    <div style={{ color: colors.text, fontWeight: typography.medium }}>{a.wallet_label}</div>
                    <div style={{ color: colors.textFaint, fontSize: typography.tiny, fontFamily: typography.mono, marginTop: 2 }}>
                      {shortAddress(a.wallet_address)}
                    </div>
                  </td>
                  <td style={{ ...td, color: colors.textDim }}>{a.associated_token || '—'}</td>
                  <td style={{ ...td, color: colors.danger, fontWeight: typography.semibold }}>
                    {a.token_symbol || shortAddress(a.token_contract)}
                  </td>
                  <td style={{ ...td, fontFamily: typography.mono, color: colors.textDim }}>{a.amount || '—'}</td>
                  <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>{formatDate(a.detected_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}

      <Section
        title="Solana Cabal Wallets"
        subtitle={`${solWallets?.total || 0} tracked wallets across all runners you've extracted.`}
      >
        {solWallets?.items?.length ? (
          <div style={{ maxHeight: 480, overflowY: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead style={{ position: 'sticky', top: 0, background: colors.bgElev1 }}>
                <tr>
                  <th style={th}>Address</th>
                  <th style={th}>Role</th>
                  <th style={th}>Token</th>
                  <th style={th}>Profit (est.)</th>
                  <th style={th}>Added</th>
                </tr>
              </thead>
              <tbody>
                {solWallets.items.map((w) => (
                  <tr key={w.wallet_address}>
                    <td style={{ ...td, fontFamily: typography.mono, fontSize: typography.small }}>
                      {w.wallet_address.slice(0, 8)}…{w.wallet_address.slice(-6)}
                    </td>
                    <td style={td}>
                      <span style={pill(
                        w.role === 'cabal_trader' ? 'accent' :
                        w.role === 'cabal_linked' ? 'warning' : 'default'
                      )}>
                        {w.role}
                      </span>
                    </td>
                    <td style={{ ...td, color: colors.textDim }}>{w.associated_token || '—'}</td>
                    <td style={{ ...td, fontFamily: typography.mono, color: colors.success }}>
                      {w.total_profit_est ? `$${Number(w.total_profit_est).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : '—'}
                    </td>
                    <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>
                      {formatDate(w.added_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            No Solana wallets tracked yet. Extract from a runner above to get started.
          </div>
        )}
      </Section>

      <Section
        title="Known BSC Operators"
        subtitle={`${wallets?.total || 0} BSC wallets from historical confirmed pumps + auto-extracted cluster members.`}
      >
        {wallets?.items?.length ? (
          <div style={{ maxHeight: 480, overflowY: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead style={{ position: 'sticky', top: 0, background: colors.bgElev1 }}>
                <tr>
                  <th style={th}>Wallet</th>
                  <th style={th}>Role</th>
                  <th style={th}>Associated</th>
                  <th style={th}>Activity</th>
                  <th style={th}>Added</th>
                </tr>
              </thead>
              <tbody>
                {wallets.items.map((w) => (
                  <tr
                    key={w.wallet_address}
                    onClick={() => setSelectedWallet(w.wallet_address)}
                    style={{ cursor: 'pointer' }}
                    onMouseEnter={(e) => e.currentTarget.style.background = colors.bgHover}
                    onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
                  >
                    <td style={td}>
                      <div style={{ color: colors.text, fontWeight: typography.medium }}>
                        {w.label || shortAddress(w.wallet_address)}
                      </div>
                      <div style={{ color: colors.textFaint, fontSize: typography.tiny, fontFamily: typography.mono, marginTop: 2 }}>
                        {shortAddress(w.wallet_address)}
                      </div>
                    </td>
                    <td style={td}>
                      <span style={pill(w.role === 'golden_deployer' ? 'danger' : 'default')}>
                        {w.role}
                      </span>
                    </td>
                    <td style={{ ...td, color: colors.textDim }}>{w.associated_token || '—'}</td>
                    <td style={{ ...td, fontFamily: typography.mono }}>{w.token_count || 0} tokens</td>
                    <td style={{ ...td, color: colors.textFaint, fontSize: typography.small }}>
                      {formatDate(w.added_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: colors.textFaint }}>
            No BSC operators tracked yet.
          </div>
        )}
      </Section>

      {selectedWallet && activity && (
        <Section
          title="Activity Feed"
          subtitle={`Recent flagged activity for ${shortAddress(selectedWallet)}`}
        >
          <button
            onClick={() => setSelectedWallet(null)}
            style={{
              background: 'transparent',
              border: `1px solid ${colors.borderStrong}`,
              color: colors.textDim,
              padding: '6px 12px',
              borderRadius: radius.sm,
              fontSize: typography.small,
              cursor: 'pointer',
              marginBottom: spacing.md,
            }}
          >
            Close
          </button>
          {activity.items?.length ? (
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={th}>Type</th>
                  <th style={th}>Token</th>
                  <th style={th}>Amount</th>
                  <th style={th}>When</th>
                </tr>
              </thead>
              <tbody>
                {activity.items.map((a) => (
                  <tr key={a.id}>
                    <td style={td}>
                      <span style={pill(a.activity_type === 'new_token_accumulation' ? 'danger' : 'default')}>
                        {a.activity_type}
                      </span>
                    </td>
                    <td style={td}>{a.token_symbol || shortAddress(a.token_contract)}</td>
                    <td style={{ ...td, fontFamily: typography.mono }}>{a.amount}</td>
                    <td style={{ ...td, color: colors.textFaint }}>{formatDate(a.detected_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div style={{ color: colors.textFaint, padding: spacing.lg }}>No flagged activity.</div>
          )}
        </Section>
      )}
    </div>
  );
}

export default WalletTracker;
