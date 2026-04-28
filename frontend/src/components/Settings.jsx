import React, { useState } from 'react';
import { useApi, apiPost } from '../hooks/useApi';
import {
  colors, typography, spacing, radius,
  card, pageTitle, pageSubtitle, sectionTitle,
  input, buttonPrimary, buttonSecondary,
} from '../theme';

function SettingRow({ setting, onSaved }) {
  const [value, setValue] = useState(setting.value);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const changed = String(value) !== String(setting.value);
  const isDefault = String(setting.value) === String(setting.default);

  async function save() {
    setSaving(true); setError(null);
    try {
      let v = value;
      if (setting.type === 'bool') v = Boolean(value);
      await apiPost('/settings', { key: setting.key, value: v });
      onSaved?.();
    } catch (e) {
      setError(e.message);
    }
    setSaving(false);
  }

  async function reset() {
    setSaving(true); setError(null);
    try {
      await apiPost('/settings/reset', { key: setting.key, value: null });
      setValue(setting.default);
      onSaved?.();
    } catch (e) {
      setError(e.message);
    }
    setSaving(false);
  }

  const renderField = () => {
    if (setting.type === 'bool') {
      return (
        <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <input
            type="checkbox"
            checked={Boolean(value)}
            onChange={(e) => setValue(e.target.checked)}
            style={{ width: 18, height: 18, cursor: 'pointer' }}
          />
          <span style={{
            color: Boolean(value) ? colors.success : colors.textFaint,
            fontSize: typography.small, fontWeight: typography.medium,
          }}>{Boolean(value) ? 'Enabled' : 'Disabled'}</span>
        </label>
      );
    }
    return (
      <input
        type={setting.type === 'string' ? 'text' : 'number'}
        step={setting.type === 'float' ? 'any' : 1}
        value={value ?? ''}
        onChange={(e) => setValue(
          setting.type === 'int' ? Number(e.target.value) :
          setting.type === 'float' ? Number(e.target.value) :
          e.target.value
        )}
        style={{ ...input, width: 220, fontFamily: typography.mono }}
      />
    );
  };

  return (
    <div style={{
      padding: `${spacing.md} ${spacing.lg}`,
      borderBottom: `1px solid ${colors.border}`,
      display: 'grid',
      gridTemplateColumns: '320px 1fr auto',
      gap: spacing.lg,
      alignItems: 'start',
    }}>
      <div>
        <div style={{
          fontFamily: typography.mono, fontSize: typography.small,
          color: colors.text, fontWeight: typography.medium,
        }}>{setting.key}</div>
        <div style={{
          fontSize: typography.tiny, color: colors.textMuted,
          marginTop: 4,
        }}>
          default: {String(setting.default)}
          {' · '}type: {setting.type}
          {!isDefault && <span style={{ color: colors.warning }}> · modified</span>}
        </div>
      </div>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: spacing.sm, marginBottom: 6 }}>
          {renderField()}
          {error && <span style={{ color: colors.danger, fontSize: typography.tiny }}>{error}</span>}
        </div>
        {setting.description && (
          <div style={{ fontSize: typography.small, color: colors.textFaint, lineHeight: 1.4 }}>
            {setting.description}
          </div>
        )}
      </div>
      <div style={{ display: 'flex', gap: 6 }}>
        <button
          onClick={save}
          disabled={!changed || saving}
          style={{
            ...buttonPrimary,
            padding: '6px 14px',
            fontSize: typography.small,
            opacity: (!changed || saving) ? 0.4 : 1,
            cursor: (!changed || saving) ? 'default' : 'pointer',
          }}
        >{saving ? '…' : 'Save'}</button>
        {!isDefault && (
          <button
            onClick={reset}
            disabled={saving}
            style={{ ...buttonSecondary, padding: '6px 14px', fontSize: typography.small }}
          >Reset</button>
        )}
      </div>
    </div>
  );
}

function CategorySection({ category, settings, onSaved }) {
  return (
    <div style={{ ...card, padding: 0, marginBottom: spacing.xl }}>
      <div style={{
        padding: `${spacing.md} ${spacing.lg}`,
        borderBottom: `1px solid ${colors.border}`,
      }}>
        <div style={sectionTitle}>{category.toUpperCase()}</div>
      </div>
      {settings.map((s) => (
        <SettingRow key={s.key} setting={s} onSaved={onSaved} />
      ))}
    </div>
  );
}

function ReplayPanel() {
  const [overridesText, setOverridesText] = useState(
    '{\n  "SAME_ENTITY_MIN_WALLETS": 2,\n  "FRESH_MAX_MC_USD": 100000\n}'
  );
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);

  async function run() {
    setLoading(true); setResult(null);
    try {
      const overrides = JSON.parse(overridesText || '{}');
      const r = await apiPost('/settings/replay', { overrides });
      setResult(r);
    } catch (e) {
      setResult({ error: e.message });
    }
    setLoading(false);
  }

  return (
    <div style={{ ...card, marginBottom: spacing.xl }}>
      <div style={sectionTitle}>Replay (backtest)</div>
      <div style={{
        fontSize: typography.small, color: colors.textFaint,
        marginBottom: spacing.md,
      }}>
        Simulate the dispatcher against current activity with override
        thresholds. No alerts actually fire — just a count of what would.
      </div>
      <textarea
        value={overridesText}
        onChange={(e) => setOverridesText(e.target.value)}
        rows={6}
        style={{
          ...input, width: '100%',
          fontFamily: typography.mono, fontSize: typography.small,
          minHeight: 140,
        }}
      />
      <div style={{ marginTop: spacing.sm }}>
        <button
          onClick={run}
          disabled={loading}
          style={{ ...buttonPrimary, opacity: loading ? 0.5 : 1 }}
        >{loading ? 'Running…' : 'Run replay'}</button>
      </div>
      {result && (
        <pre style={{
          marginTop: spacing.md,
          background: colors.bgSubtle,
          border: `1px solid ${colors.border}`,
          borderRadius: radius.sm,
          padding: spacing.md,
          fontSize: typography.small,
          color: colors.textDim,
          overflow: 'auto',
        }}>{JSON.stringify(result, null, 2)}</pre>
      )}
    </div>
  );
}

function CexPanel() {
  const { data, refetch } = useApi('/cex-addresses', { refreshInterval: 60000 });
  const [newAddr, setNewAddr] = useState('');
  const [newName, setNewName] = useState('');
  const [newExch, setNewExch] = useState('');
  const items = data?.items || [];

  async function add() {
    if (!newAddr.trim() || !newName.trim()) return;
    try {
      await apiPost('/cex-addresses', {
        address: newAddr.trim(), name: newName.trim(), exchange: newExch.trim(),
      });
      setNewAddr(''); setNewName(''); setNewExch('');
      refetch();
    } catch (e) { alert(e.message); }
  }
  async function toggle(addr) {
    try {
      await apiPost(`/cex-addresses/${addr}/toggle`, {});
      refetch();
    } catch (e) { alert(e.message); }
  }

  return (
    <div style={{ ...card, padding: 0, marginBottom: spacing.xl }}>
      <div style={{
        padding: `${spacing.md} ${spacing.lg}`,
        borderBottom: `1px solid ${colors.border}`,
      }}>
        <div style={sectionTitle}>CEX deposit addresses</div>
        <div style={{ fontSize: typography.small, color: colors.textFaint }}>
          SOL transfers from tracked wallets to these addresses are tagged as
          CEX outflows (cash-out signal).
        </div>
      </div>
      <div style={{ padding: spacing.md, display: 'flex', gap: spacing.sm, flexWrap: 'wrap' }}>
        <input value={newAddr} onChange={(e) => setNewAddr(e.target.value)} placeholder="Solana address" style={{ ...input, flex: '1 1 300px', fontFamily: typography.mono }} />
        <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Label (e.g. Binance 4)" style={{ ...input, width: 180 }} />
        <input value={newExch} onChange={(e) => setNewExch(e.target.value)} placeholder="Exchange" style={{ ...input, width: 140 }} />
        <button onClick={add} style={buttonPrimary}>Add</button>
      </div>
      <div style={{ maxHeight: 320, overflowY: 'auto', padding: `0 ${spacing.lg} ${spacing.md}` }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: typography.small }}>
          <tbody>
            {items.map(r => (
              <tr key={r.address} style={{ borderBottom: `1px solid ${colors.border}` }}>
                <td style={{ padding: 8, fontFamily: typography.mono, color: colors.textDim }}>
                  {r.address.slice(0,6)}…{r.address.slice(-6)}
                </td>
                <td style={{ padding: 8, color: colors.text, fontWeight: typography.medium }}>{r.name}</td>
                <td style={{ padding: 8, color: colors.textFaint }}>{r.exchange || '—'}</td>
                <td style={{ padding: 8 }}>
                  <button
                    onClick={() => toggle(r.address)}
                    style={{
                      background: r.is_active ? colors.successSoft : 'transparent',
                      color: r.is_active ? colors.success : colors.textFaint,
                      border: `1px solid ${r.is_active ? colors.success : colors.borderStrong}`,
                      borderRadius: radius.pill, padding: '3px 10px',
                      fontSize: typography.tiny, fontWeight: typography.semibold,
                      cursor: 'pointer',
                    }}
                  >{r.is_active ? 'ACTIVE' : 'INACTIVE'}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function MajorCoinPanel() {
  const { data, refetch } = useApi('/major-coins', { refreshInterval: 60000 });
  const [newSym, setNewSym] = useState('');
  const [newLabel, setNewLabel] = useState('');
  const items = data?.items || [];

  async function add() {
    if (!newSym.trim()) return;
    try {
      await apiPost('/major-coins', {
        symbol: newSym.trim(), label: newLabel.trim(),
      });
      setNewSym(''); setNewLabel('');
      refetch();
    } catch (e) { alert(e.message); }
  }
  async function toggle(sym) {
    try {
      await apiPost(`/major-coins/${sym}/toggle`, {});
      refetch();
    } catch (e) { alert(e.message); }
  }

  return (
    <div style={{ ...card, padding: 0, marginBottom: spacing.xl }}>
      <div style={{
        padding: `${spacing.md} ${spacing.lg}`,
        borderBottom: `1px solid ${colors.border}`,
      }}>
        <div style={sectionTitle}>Major coins (anomaly exclusion)</div>
        <div style={{ fontSize: typography.small, color: colors.textFaint }}>
          The Hyperliquid whale watcher (and future watchers) ignore
          these symbols. Add or toggle to tune what counts as a "major."
        </div>
      </div>
      <div style={{ padding: spacing.md, display: 'flex', gap: spacing.sm, flexWrap: 'wrap' }}>
        <input value={newSym} onChange={(e) => setNewSym(e.target.value)} placeholder="Symbol (e.g. PEPE)" style={{ ...input, width: 180, fontFamily: typography.mono, textTransform: 'uppercase' }} />
        <input value={newLabel} onChange={(e) => setNewLabel(e.target.value)} placeholder="Label (optional)" style={{ ...input, flex: '1 1 200px' }} />
        <button onClick={add} style={buttonPrimary}>Add</button>
      </div>
      <div style={{ maxHeight: 320, overflowY: 'auto', padding: `0 ${spacing.lg} ${spacing.md}` }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: typography.small }}>
          <tbody>
            {items.map(r => (
              <tr key={r.symbol} style={{ borderBottom: `1px solid ${colors.border}` }}>
                <td style={{ padding: 8, fontFamily: typography.mono, fontWeight: 600, color: colors.text }}>
                  {r.symbol}
                </td>
                <td style={{ padding: 8, color: colors.textFaint }}>{r.label || '—'}</td>
                <td style={{ padding: 8, textAlign: 'right' }}>
                  <button
                    onClick={() => toggle(r.symbol)}
                    style={{
                      background: r.excluded ? colors.warningSoft : 'transparent',
                      color: r.excluded ? colors.warning : colors.textFaint,
                      border: `1px solid ${r.excluded ? colors.warning : colors.borderStrong}`,
                      borderRadius: radius.pill, padding: '3px 10px',
                      fontSize: typography.tiny, fontWeight: 600,
                      cursor: 'pointer',
                    }}
                  >{r.excluded ? 'EXCLUDED' : 'WATCHED'}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}


function Settings() {
  const { data, refetch } = useApi('/settings', { refreshInterval: 0 });
  const allItems = data?.items || [];
  // Hide internal-only categories (prefix _) from the UI
  const items = allItems.filter(s => !(s.category || '').startsWith('_'));
  const byCategory = items.reduce((acc, s) => {
    (acc[s.category] ||= []).push(s);
    return acc;
  }, {});

  return (
    <div>
      <h1 style={pageTitle}>Settings</h1>
      <div style={pageSubtitle}>
        All alert thresholds live here. Changes persist to the DB and
        take effect within 30 seconds — no redeploy.
      </div>
      {Object.keys(byCategory).sort().map(cat => (
        <CategorySection
          key={cat}
          category={cat}
          settings={byCategory[cat]}
          onSaved={refetch}
        />
      ))}
      <MajorCoinPanel />
      <CexPanel />
      <ReplayPanel />
    </div>
  );
}

export default Settings;
