import React, { useState } from 'react';
import { useApi, apiPost } from '../hooks/useApi';
import { shortAddress, formatDate } from '../utils/formatters';

const cardStyle = {
  background: '#111118',
  border: '1px solid #222',
  borderRadius: '8px',
  padding: '20px',
  marginBottom: '16px',
};

const thStyle = {
  textAlign: 'left',
  padding: '10px 12px',
  color: '#666',
  fontSize: '11px',
  textTransform: 'uppercase',
  borderBottom: '1px solid #222',
};

const tdStyle = {
  padding: '10px 12px',
  borderBottom: '1px solid #1a1a1a',
  fontSize: '13px',
};

function WalletTracker() {
  const { data: wallets, refetch: refetchWallets } = useApi('/wallets/known?per_page=100', { refreshInterval: 60000 });
  const { data: newAccum } = useApi('/wallets/new-accumulations?limit=20', { refreshInterval: 30000 });
  const { data: balances } = useApi('/wallets/balances?per_page=100', { refreshInterval: 120000 });
  const [selectedWallet, setSelectedWallet] = useState(null);
  const { data: activity } = useApi(
    `/wallets/${selectedWallet}/activity?per_page=50&flagged_only=true`,
    { enabled: !!selectedWallet, refreshInterval: 30000 }
  );

  const [addForm, setAddForm] = useState({ wallet_address: '', label: '', role: 'accumulator' });
  const [addError, setAddError] = useState(null);

  // Cabal extraction form state
  const [cabalMint, setCabalMint] = useState('');
  const [cabalSymbol, setCabalSymbol] = useState('');
  const [cabalResult, setCabalResult] = useState(null);
  const [cabalLoading, setCabalLoading] = useState(false);

  async function handleExtractCabal(e) {
    e.preventDefault();
    if (!cabalMint.trim()) return;
    setCabalLoading(true);
    setCabalResult(null);
    try {
      const result = await apiPost('/wallets/solana/extract-from-runner', {
        mint: cabalMint.trim(),
        symbol: cabalSymbol.trim() || '',
        auto_add: true,
      });
      setCabalResult(result);
      if (result.wallets_added > 0) {
        refetchWallets();
      }
    } catch (err) {
      setCabalResult({ error: err.message });
    }
    setCabalLoading(false);
  }

  async function handleAddWallet(e) {
    e.preventDefault();
    try {
      setAddError(null);
      await apiPost('/wallets/add', addForm);
      setAddForm({ wallet_address: '', label: '', role: 'accumulator' });
      refetchWallets();
    } catch (err) {
      setAddError(err.message);
    }
  }

  return (
    <div>
      <h2 style={{ fontSize: '18px', marginBottom: '20px' }}>Known Wallet Tracker</h2>

      {/* Cabal extraction — paste a runner CA */}
      <div style={{ ...cardStyle, borderColor: '#44aaff' }}>
        <h3 style={{ fontSize: '14px', color: '#44aaff', marginBottom: '8px' }}>
          Extract Cabal Wallets from a Runner
        </h3>
        <div style={{ color: '#666', fontSize: '12px', marginBottom: '12px' }}>
          Paste a Solana token CA that pumped big. The system extracts top holders/traders, adds them to the watchlist, and automatically tracks where they send money next.
        </div>
        <form onSubmit={handleExtractCabal} style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            type="text"
            value={cabalMint}
            onChange={(e) => setCabalMint(e.target.value)}
            placeholder="Solana token CA (e.g. Hon2rH...pump)"
            style={{
              flex: 1, minWidth: '300px',
              background: '#0a0a0f', border: '1px solid #333', color: '#fff',
              padding: '10px 12px', borderRadius: '4px', fontSize: '13px',
              fontFamily: 'monospace',
            }}
          />
          <input
            type="text"
            value={cabalSymbol}
            onChange={(e) => setCabalSymbol(e.target.value)}
            placeholder="Symbol (optional)"
            style={{
              width: '140px',
              background: '#0a0a0f', border: '1px solid #333', color: '#fff',
              padding: '10px 12px', borderRadius: '4px', fontSize: '13px',
            }}
          />
          <button
            type="submit"
            disabled={cabalLoading || !cabalMint.trim()}
            style={{
              background: cabalLoading ? '#222' : '#1a1a2e',
              border: '1px solid #44aaff',
              color: cabalLoading ? '#555' : '#44aaff',
              padding: '10px 24px', borderRadius: '4px',
              cursor: cabalLoading || !cabalMint.trim() ? 'default' : 'pointer',
              fontSize: '13px', fontWeight: 'bold', whiteSpace: 'nowrap',
            }}
          >
            {cabalLoading ? 'Extracting...' : 'Extract Wallets'}
          </button>
        </form>

        {cabalResult && (
          <div style={{ marginTop: '12px' }}>
            {cabalResult.error ? (
              <div style={{ color: '#ff4444', fontSize: '12px' }}>Error: {cabalResult.error}</div>
            ) : (
              <>
                <div style={{
                  color: cabalResult.wallets_found > 0 ? '#44ff88' : '#ffaa00',
                  fontSize: '13px', fontWeight: 'bold', marginBottom: '8px',
                }}>
                  Found {cabalResult.wallets_found} wallets, added {cabalResult.wallets_added} new to watchlist
                </div>
                {cabalResult.wallets && cabalResult.wallets.length > 0 && (
                  <div style={{ maxHeight: '300px', overflowY: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                      <thead>
                        <tr>
                          <th style={thStyle}>#</th>
                          <th style={thStyle}>Address</th>
                          <th style={thStyle}>Balance USD</th>
                          <th style={thStyle}>Profit</th>
                          <th style={thStyle}>Mult</th>
                          <th style={thStyle}>Tags</th>
                        </tr>
                      </thead>
                      <tbody>
                        {cabalResult.wallets.map((w, i) => (
                          <tr key={w.address}>
                            <td style={tdStyle}>{i + 1}</td>
                            <td style={{ ...tdStyle, fontFamily: 'monospace', fontSize: '11px' }}>
                              {w.address.slice(0, 8)}…{w.address.slice(-6)}
                            </td>
                            <td style={tdStyle}>${w.balance_usd?.toLocaleString(undefined, { maximumFractionDigits: 0 }) || '—'}</td>
                            <td style={{ ...tdStyle, color: w.total_profit_usd > 0 ? '#44ff88' : '#888' }}>
                              ${w.total_profit_usd?.toLocaleString(undefined, { maximumFractionDigits: 0 }) || '—'}
                            </td>
                            <td style={tdStyle}>{w.profit_multiplier ? `${w.profit_multiplier}x` : '—'}</td>
                            <td style={{ ...tdStyle, color: '#888' }}>{(w.tags || []).join(', ')}</td>
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

      {/* New accumulations — HIGH PRIORITY */}
      <div style={{ ...cardStyle, borderColor: '#ff4444' }}>
        <h3 style={{ fontSize: '14px', color: '#ff4444', marginBottom: '12px' }}>
          New Token Accumulations (High Priority)
        </h3>
        {newAccum && newAccum.length > 0 ? (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={thStyle}>Operator</th>
                <th style={thStyle}>Role</th>
                <th style={thStyle}>From Token</th>
                <th style={thStyle}>New Token</th>
                <th style={thStyle}>Amount</th>
                <th style={thStyle}>Detected</th>
              </tr>
            </thead>
            <tbody>
              {newAccum.map((a, i) => (
                <tr key={i}>
                  <td style={tdStyle}>
                    <div style={{ color: '#fff' }}>{a.wallet_label}</div>
                    <div style={{ color: '#555', fontSize: '11px' }}>{shortAddress(a.wallet_address)}</div>
                  </td>
                  <td style={tdStyle}>{a.wallet_role || '-'}</td>
                  <td style={tdStyle}>{a.associated_token || '-'}</td>
                  <td style={{ ...tdStyle, color: '#ff4444', fontWeight: 'bold' }}>{a.token_symbol || shortAddress(a.token_contract)}</td>
                  <td style={tdStyle}>{a.amount || '-'}</td>
                  <td style={tdStyle}>{formatDate(a.detected_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div style={{ color: '#444', fontSize: '13px' }}>No new accumulations detected</div>
        )}
      </div>

      {/* Known wallets table */}
      <div style={cardStyle}>
        <h3 style={{ fontSize: '14px', marginBottom: '12px', color: '#888' }}>
          Tracked Wallets ({wallets?.total || 0})
        </h3>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={thStyle}>Label</th>
                <th style={thStyle}>Address</th>
                <th style={thStyle}>Token</th>
                <th style={thStyle}>Role</th>
                <th style={thStyle}>Tokens Held</th>
                <th style={thStyle}>BNB Balance</th>
                <th style={thStyle}>Funding</th>
                <th style={thStyle}>Added</th>
              </tr>
            </thead>
            <tbody>
              {(wallets?.items || []).map((w, i) => {
                const bnb = balances ? balances[w.wallet_address] : null;
                return (
                <tr
                  key={i}
                  onClick={() => setSelectedWallet(w.wallet_address)}
                  style={{
                    cursor: 'pointer',
                    background: selectedWallet === w.wallet_address ? 'rgba(68, 170, 255, 0.08)' : 'transparent',
                  }}
                >
                  <td style={{ ...tdStyle, color: '#fff' }}>{w.label || '-'}</td>
                  <td style={{ ...tdStyle, fontFamily: 'monospace', fontSize: '12px' }}>{shortAddress(w.wallet_address)}</td>
                  <td style={tdStyle}>{w.associated_token || '-'}</td>
                  <td style={tdStyle}>
                    <span style={{
                      color: w.role === 'deployer' ? '#ff4444' :
                             w.role === 'distributor' ? '#ff8800' :
                             w.role === 'cluster_member' ? '#ffaa00' : '#888',
                    }}>
                      {w.role || '-'}
                    </span>
                  </td>
                  <td style={tdStyle}>
                    <span style={{ color: w.token_count > 0 ? '#44aaff' : '#444' }}>
                      {w.token_count || 0}
                    </span>
                  </td>
                  <td style={tdStyle}>
                    {bnb !== null && bnb !== undefined ? (
                      <span style={{ color: bnb > 1 ? '#44ff44' : bnb > 0 ? '#888' : '#444' }}>
                        {bnb.toFixed(4)} BNB
                      </span>
                    ) : (
                      <span style={{ color: '#333' }}>...</span>
                    )}
                  </td>
                  <td style={{ ...tdStyle, fontSize: '12px' }}>{shortAddress(w.funding_source)}</td>
                  <td style={tdStyle}>{formatDate(w.added_at)}</td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Activity feed for selected wallet */}
      {selectedWallet && (
        <div style={cardStyle}>
          <h3 style={{ fontSize: '14px', marginBottom: '12px', color: '#888' }}>
            Activity: {shortAddress(selectedWallet)}
          </h3>
          {activity && activity.items && activity.items.length > 0 ? (
            <div style={{ maxHeight: '400px', overflowY: 'auto' }}>
              {activity.items.map((a, i) => (
                <div key={i} style={{
                  display: 'flex', gap: '12px', alignItems: 'center',
                  padding: '10px', borderBottom: '1px solid #1a1a1a',
                }}>
                  <span style={{
                    padding: '2px 8px', borderRadius: '4px', fontSize: '11px',
                    background: a.activity_type.includes('deposit') ? '#331111' :
                                a.activity_type === 'new_token_accumulation' ? '#113311' :
                                a.activity_type === 'gas_funding' ? '#333311' : '#111133',
                    color: a.activity_type.includes('deposit') ? '#ff4444' :
                           a.activity_type === 'new_token_accumulation' ? '#44ff44' :
                           a.activity_type === 'gas_funding' ? '#ffaa00' : '#44aaff',
                  }}>
                    {a.activity_type}
                  </span>
                  <span style={{ color: '#fff', fontSize: '13px' }}>{a.token_symbol || 'BNB'}</span>
                  <span style={{ color: '#666', fontSize: '12px' }}>{a.amount || ''}</span>
                  {a.counterparty_label && (
                    <span style={{ color: '#555', fontSize: '12px' }}>{a.counterparty_label}</span>
                  )}
                  <span style={{ color: '#444', fontSize: '11px', marginLeft: 'auto' }}>
                    {formatDate(a.detected_at)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: '#444', fontSize: '13px' }}>No flagged activity for this wallet</div>
          )}
        </div>
      )}

      {/* Add wallet form */}
      <div style={cardStyle}>
        <h3 style={{ fontSize: '14px', marginBottom: '12px', color: '#888' }}>Add Wallet to Track</h3>
        <form onSubmit={handleAddWallet} style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
          <input
            type="text"
            placeholder="Wallet address (0x...)"
            value={addForm.wallet_address}
            onChange={(e) => setAddForm(prev => ({ ...prev, wallet_address: e.target.value }))}
            style={{
              background: '#0a0a0f', border: '1px solid #333', color: '#fff',
              padding: '8px 12px', borderRadius: '4px', fontSize: '13px', flex: '1', minWidth: '300px',
            }}
            required
          />
          <input
            type="text"
            placeholder="Label"
            value={addForm.label}
            onChange={(e) => setAddForm(prev => ({ ...prev, label: e.target.value }))}
            style={{
              background: '#0a0a0f', border: '1px solid #333', color: '#fff',
              padding: '8px 12px', borderRadius: '4px', fontSize: '13px', width: '200px',
            }}
          />
          <select
            value={addForm.role}
            onChange={(e) => setAddForm(prev => ({ ...prev, role: e.target.value }))}
            style={{
              background: '#0a0a0f', border: '1px solid #333', color: '#fff',
              padding: '8px 12px', borderRadius: '4px', fontSize: '13px',
            }}
          >
            <option value="accumulator">Accumulator</option>
            <option value="deployer">Deployer</option>
            <option value="distributor">Distributor</option>
            <option value="cluster_member">Cluster Member</option>
          </select>
          <button
            type="submit"
            style={{
              background: '#1a1a2e', border: '1px solid #44aaff', color: '#44aaff',
              padding: '8px 20px', borderRadius: '4px', cursor: 'pointer', fontSize: '13px',
            }}
          >
            Add
          </button>
        </form>
        {addError && <div style={{ color: '#ff4444', fontSize: '12px', marginTop: '8px' }}>{addError}</div>}
      </div>
    </div>
  );
}

export default WalletTracker;
