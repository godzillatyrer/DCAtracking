import React, { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { formatDate } from '../utils/formatters';

const cardStyle = {
  background: '#111118',
  border: '1px solid #222',
  borderRadius: '8px',
  padding: '16px 20px',
  marginBottom: '16px',
};

const statusColors = {
  ok:              { bg: '#113311', color: '#44ff44', label: 'OK' },
  error:           { bg: '#331111', color: '#ff4444', label: 'ERROR' },
  not_configured:  { bg: '#222',     color: '#888',   label: 'NOT CONFIGURED' },
  unknown:         { bg: '#222',     color: '#888',   label: 'UNKNOWN' },
};

function NansenCustomProbe() {
  const [url, setUrl] = useState('');
  const [header, setHeader] = useState('apiKey');
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  async function run(e) {
    e?.stopPropagation?.();
    if (!url.trim()) return;
    setBusy(true);
    try {
      const qs = new URLSearchParams({ url: url.trim(), header }).toString();
      const resp = await fetch(`/api/diagnostics/nansen-probe?${qs}`);
      const data = await resp.json();
      setResult(data);
    } catch (err) {
      setResult({ error: err.message });
    }
    setBusy(false);
  }

  return (
    <div
      onClick={(e) => e.stopPropagation()}
      style={{
        marginTop: '10px',
        padding: '10px',
        background: '#0a0a0f',
        borderRadius: '4px',
        border: '1px solid #223',
      }}
    >
      <div style={{ color: '#44aaff', fontSize: '11px', marginBottom: '6px', fontWeight: 'bold' }}>
        TEST A CUSTOM URL (paste any endpoint from your Nansen dashboard)
      </div>
      <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://api.nansen.ai/api/beta/profile/0x..."
          onClick={(e) => e.stopPropagation()}
          style={{
            flex: 1,
            background: '#111',
            border: '1px solid #333',
            color: '#fff',
            padding: '6px 8px',
            borderRadius: '3px',
            fontSize: '11px',
            fontFamily: 'monospace',
          }}
        />
        <select
          value={header}
          onChange={(e) => setHeader(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          style={{
            background: '#111',
            border: '1px solid #333',
            color: '#fff',
            padding: '6px',
            borderRadius: '3px',
            fontSize: '11px',
          }}
        >
          <option value="apiKey">apiKey header</option>
          <option value="authorization-bearer">Bearer</option>
        </select>
        <button
          disabled={busy || !url.trim()}
          onClick={run}
          style={{
            background: busy ? '#222' : '#1a1a2e',
            border: '1px solid #44aaff',
            color: busy ? '#555' : '#44aaff',
            padding: '6px 14px',
            borderRadius: '3px',
            cursor: busy || !url.trim() ? 'default' : 'pointer',
            fontSize: '11px',
            fontWeight: 'bold',
          }}
        >
          {busy ? '...' : 'Test'}
        </button>
      </div>
      {result && (
        <div style={{ marginTop: '8px', fontSize: '11px', fontFamily: 'monospace' }}>
          <span style={{
            color: result.status === 200 ? '#44ff88' : '#ff8888',
            fontWeight: 'bold',
          }}>
            [{result.status ?? 'err'}]
          </span>{' '}
          <span style={{ color: '#888' }}>{result.latency_ms}ms</span>
          {result.error && <span style={{ color: '#ff8888' }}> — {result.error}</span>}
          {result.body_preview && (
            <div style={{ color: '#aaa', marginTop: '4px', whiteSpace: 'pre-wrap' }}>
              {result.body_preview}
            </div>
          )}
        </div>
      )}
    </div>
  );
}


function StatusBadge({ status }) {
  const c = statusColors[status] || statusColors.unknown;
  return (
    <span style={{
      padding: '3px 10px', borderRadius: '4px',
      background: c.bg, color: c.color,
      fontSize: '11px', fontWeight: 'bold',
      whiteSpace: 'nowrap',
    }}>
      {c.label}
    </span>
  );
}

function SummaryBar({ summary }) {
  if (!summary) return null;
  return (
    <div style={{ display: 'flex', gap: '16px', marginBottom: '20px', flexWrap: 'wrap' }}>
      <div style={{ ...cardStyle, marginBottom: 0, minWidth: '140px' }}>
        <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Healthy</div>
        <div style={{ fontSize: '28px', fontWeight: 'bold', color: '#44ff44', marginTop: '4px' }}>
          {summary.ok}
        </div>
      </div>
      <div style={{ ...cardStyle, marginBottom: 0, minWidth: '140px' }}>
        <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Errors</div>
        <div style={{ fontSize: '28px', fontWeight: 'bold', color: '#ff4444', marginTop: '4px' }}>
          {summary.errors}
        </div>
      </div>
      <div style={{ ...cardStyle, marginBottom: 0, minWidth: '140px' }}>
        <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Not Configured</div>
        <div style={{ fontSize: '28px', fontWeight: 'bold', color: '#888', marginTop: '4px' }}>
          {summary.not_configured}
        </div>
      </div>
      <div style={{ ...cardStyle, marginBottom: 0, minWidth: '140px' }}>
        <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Total</div>
        <div style={{ fontSize: '28px', fontWeight: 'bold', marginTop: '4px' }}>
          {summary.total}
        </div>
      </div>
    </div>
  );
}

function APIRow({ api }) {
  const [expanded, setExpanded] = useState(false);
  const hasError = api.status === 'error' || !!api.last_job_error;

  return (
    <div
      style={{
        ...cardStyle,
        cursor: 'pointer',
        borderColor: hasError ? '#553333' : '#222',
      }}
      onClick={() => setExpanded(!expanded)}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
        <StatusBadge status={api.status} />
        <div style={{ minWidth: '140px' }}>
          <div style={{ fontWeight: 'bold', fontSize: '14px' }}>{api.name}</div>
          <div style={{ fontSize: '11px', color: '#666' }}>{api.env_key || '(no env key)'}</div>
        </div>
        <div style={{ flex: 1, color: '#aaa', fontSize: '12px' }}>
          {api.detail || api.error || '—'}
        </div>
        {api.latency_ms != null && (
          <div style={{
            fontSize: '11px',
            color: api.latency_ms > 2000 ? '#ffaa00' : '#666',
          }}>
            {api.latency_ms}ms
          </div>
        )}
      </div>

      {expanded && (
        <div style={{
          marginTop: '12px',
          paddingTop: '12px',
          borderTop: '1px solid #1a1a1a',
          fontSize: '12px',
          color: '#aaa',
        }}>
          <div style={{ marginBottom: '6px' }}>
            <span style={{ color: '#666' }}>Role: </span>{api.role}
          </div>
          {api.calls_today != null && (
            <div style={{
              color: '#ffcc44',
              fontSize: '11px',
              marginBottom: '6px',
              fontFamily: 'monospace',
            }}>
              Credits: {api.calls_today} calls today
              {api.last_success_at && (
                <> · last 200: {formatDate(api.last_success_at)}</>
              )}
            </div>
          )}
          {api.error && (
            <div style={{ color: '#ff8888', marginBottom: '6px' }}>
              <b>Live error:</b> {api.error}
            </div>
          )}
          {api.hint && (
            <div style={{
              color: '#ffaa00',
              marginBottom: '6px',
              fontSize: '11px',
              fontStyle: 'italic',
            }}>
              Hint: {api.hint}
            </div>
          )}
          {api.attempts && api.attempts.length > 0 && (
            <div style={{
              padding: '8px',
              background: '#0a0a0f',
              borderRadius: '4px',
              marginBottom: '8px',
              fontFamily: 'monospace',
              fontSize: '11px',
            }}>
              <div style={{ color: '#666', marginBottom: '4px' }}>PROBE ATTEMPTS:</div>
              {api.attempts.map((a, i) => (
                <div key={i} style={{ color: '#aaa', marginBottom: '6px' }}>
                  <span style={{
                    color: a.status === 200 ? '#44ff88' : '#ff8888',
                  }}>
                    [{a.status || '—'}]
                  </span>{' '}
                  <span style={{ color: '#888' }}>{a.endpoint}</span>
                  <div style={{ color: '#555', fontSize: '10px', marginLeft: '32px' }}>
                    {a.url}
                  </div>
                  {a.preview && (
                    <div style={{ color: '#666', fontSize: '10px', marginLeft: '32px' }}>
                      {a.preview}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {api.working_url && (
            <div style={{ color: '#44ff88', fontSize: '11px', marginBottom: '6px' }}>
              Working URL: <code>{api.working_url}</code>
            </div>
          )}
          {api.name === 'Nansen' && (
            <NansenCustomProbe />
          )}
          {api.last_job_error && (
            <div style={{
              padding: '8px',
              background: '#1a0a0a',
              borderRadius: '4px',
              marginTop: '8px',
            }}>
              <div style={{ color: '#ffaa00', fontSize: '11px', marginBottom: '4px' }}>
                LAST JOB ERROR — {api.last_job_error.job}
                <span style={{ color: '#555', marginLeft: '10px' }}>
                  {formatDate(api.last_job_error.at)}
                </span>
              </div>
              <div style={{ color: '#ff8888', fontFamily: 'monospace', fontSize: '11px' }}>
                {api.last_job_error.error}
              </div>
            </div>
          )}
          <div style={{ marginTop: '6px', color: '#555' }}>
            Checked: {formatDate(api.checked_at)}
          </div>
        </div>
      )}
    </div>
  );
}

function SeedActions() {
  const [msg, setMsg] = useState({});
  const [busy, setBusy] = useState({});

  async function run(name, url) {
    setBusy((b) => ({ ...b, [name]: true }));
    setMsg((m) => ({ ...m, [name]: null }));
    try {
      const res = await fetch(url, { method: 'POST' });
      const data = await res.json();
      setMsg((m) => ({ ...m, [name]: data.message || JSON.stringify(data) }));
    } catch (err) {
      setMsg((m) => ({ ...m, [name]: 'Error: ' + err.message }));
    }
    setBusy((b) => ({ ...b, [name]: false }));
  }

  const actions = [
    {
      key: 'known_wallets',
      title: 'Seed Known Wallets',
      desc: 'Extracts top holders / clusters from RAVE, SIREN, RIVER, ARIA, STO. Idempotent.',
      url: '/api/dashboard/seed-wallets',
    },
    {
      key: 'bridges',
      title: 'Seed BSC Bridges',
      desc: 'Seeds Wormhole, Stargate, cBridge, Synapse, etc. for Module 14 (exploit detection).',
      url: '/api/dashboard/seed-bridges',
    },
    {
      key: 'golden_deployers',
      title: 'Seed Golden Deployers',
      desc: 'Resolves deployer addresses for historical $50M+ BSC launches and tags them as golden_deployer in known_wallets. Takes 5-15 min.',
      url: '/api/dashboard/seed-golden-deployers',
    },
    {
      key: 'solana_wallets',
      title: 'Seed Solana Infrastructure',
      desc: 'Bootstraps solana_known_wallets with Raydium/Pump.fun/Jupiter so Solana detection has a baseline.',
      url: '/api/dashboard/seed-solana-wallets',
    },
  ];

  return (
    <div style={{ ...cardStyle, marginBottom: '24px' }}>
      <h3 style={{ fontSize: '14px', marginBottom: '12px', color: '#fff' }}>
        Seed Actions <span style={{ color: '#666', fontWeight: 'normal' }}>(idempotent, 24h cooldown)</span>
      </h3>
      {actions.map((a) => (
        <div
          key={a.key}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '16px',
            padding: '10px 0',
            borderBottom: '1px solid #1a1a1a',
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 'bold', fontSize: '13px' }}>{a.title}</div>
            <div style={{ color: '#666', fontSize: '12px', marginTop: '2px' }}>{a.desc}</div>
            {msg[a.key] && (
              <div style={{
                color: msg[a.key].startsWith('Error') ? '#ff8888' : '#44ff88',
                fontSize: '11px',
                marginTop: '4px',
              }}>
                {msg[a.key]}
              </div>
            )}
          </div>
          <button
            disabled={busy[a.key]}
            onClick={() => run(a.key, a.url)}
            style={{
              background: busy[a.key] ? '#222' : '#1a1a2e',
              border: '1px solid #44aaff',
              color: busy[a.key] ? '#555' : '#44aaff',
              padding: '8px 20px',
              borderRadius: '4px',
              cursor: busy[a.key] ? 'default' : 'pointer',
              fontSize: '12px',
              fontWeight: 'bold',
              whiteSpace: 'nowrap',
            }}
          >
            {busy[a.key] ? 'Running…' : 'Run'}
          </button>
        </div>
      ))}
    </div>
  );
}

const severityColors = {
  critical: { bg: '#331111', color: '#ff5555', icon: '\u274c' },
  warning:  { bg: '#332211', color: '#ffaa00', icon: '\u26a0' },
  info:     { bg: '#111a33', color: '#88aaff', icon: '\u2139' },
};

function HealthAuditPanel() {
  const { data: audit, loading, refetch } = useApi('/diagnostics/health-audit', {
    refreshInterval: 120000,
  });
  const [healing, setHealing] = useState(false);
  const [lastHealResult, setLastHealResult] = useState(null);

  async function autoHeal() {
    if (!audit?.issues) return;
    setHealing(true);
    setLastHealResult(null);
    const results = [];
    try {
      for (const issue of audit.issues) {
        if (!issue.auto_fix_url) continue;
        try {
          const resp = await fetch(issue.auto_fix_url, { method: 'POST' });
          const payload = await resp.json();
          results.push({ id: issue.id, ok: resp.ok, payload });
        } catch (e) {
          results.push({ id: issue.id, ok: false, error: e.message });
        }
      }
      setLastHealResult(results);
      if (refetch) await refetch();
    } finally {
      setHealing(false);
    }
  }

  const issues = audit?.issues || [];
  const summary = audit?.summary;
  const healthy = audit?.healthy;
  const autoFixable = issues.filter((i) => i.auto_fix_url).length;

  return (
    <div style={{ ...cardStyle, marginBottom: '24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <div>
          <h3 style={{ fontSize: '14px', margin: 0, color: '#fff' }}>
            Routine Health Audit{' '}
            <span style={{ color: '#666', fontWeight: 'normal' }}>
              — same data a Claude routine would see
            </span>
          </h3>
          {summary && (
            <div style={{ fontSize: '12px', color: '#888', marginTop: '6px' }}>
              {healthy ? (
                <span style={{ color: '#44ff88' }}>HEALTHY</span>
              ) : (
                <span style={{ color: '#ff5555' }}>UNHEALTHY</span>
              )}
              {' · '}
              Critical: <b style={{ color: '#ff5555' }}>{summary.critical}</b>{' · '}
              Warning: <b style={{ color: '#ffaa00' }}>{summary.warning}</b>{' · '}
              Info: <b style={{ color: '#88aaff' }}>{summary.info}</b>
            </div>
          )}
        </div>
        {autoFixable > 0 && (
          <button
            onClick={autoHeal}
            disabled={healing}
            style={{
              background: healing ? '#222' : '#1a2e1a',
              border: '1px solid #44ff88',
              color: healing ? '#555' : '#44ff88',
              padding: '8px 16px',
              borderRadius: '4px',
              cursor: healing ? 'default' : 'pointer',
              fontSize: '12px',
              fontWeight: 'bold',
            }}
          >
            {healing ? 'Healing…' : `Auto-heal (${autoFixable})`}
          </button>
        )}
      </div>

      {loading && !audit ? (
        <div style={{ color: '#666', padding: '16px 0' }}>Running audit…</div>
      ) : issues.length === 0 ? (
        <div style={{ color: '#44ff88', padding: '12px 0', fontSize: '13px' }}>
          {'\u2705'} All checks passing. Next audit in 2 minutes.
        </div>
      ) : (
        <div>
          {issues.map((issue) => {
            const s = severityColors[issue.severity] || severityColors.info;
            return (
              <div
                key={issue.id}
                style={{
                  padding: '10px 12px',
                  marginBottom: '8px',
                  background: s.bg,
                  borderRadius: '4px',
                  borderLeft: `3px solid ${s.color}`,
                  fontSize: '12px',
                }}
              >
                <div style={{ color: s.color, fontWeight: 'bold', marginBottom: '4px' }}>
                  {s.icon} {issue.title}
                </div>
                <div style={{ color: '#aaa', marginBottom: '4px' }}>
                  {issue.likely_fix}
                </div>
                {issue.files_to_check && issue.files_to_check.length > 0 && (
                  <div style={{ color: '#666', fontFamily: 'monospace', fontSize: '11px' }}>
                    files: {issue.files_to_check.join(', ')}
                  </div>
                )}
                {issue.auto_fix_url && (
                  <div style={{ color: '#44ff88', fontSize: '11px', marginTop: '4px' }}>
                    auto-fixable via {issue.auto_fix_url}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {lastHealResult && (
        <div style={{ marginTop: '12px', padding: '10px', background: '#0a0a0f', borderRadius: '4px' }}>
          <div style={{ color: '#888', fontSize: '11px', marginBottom: '4px' }}>LAST HEAL RESULT</div>
          {lastHealResult.map((r, i) => (
            <div key={i} style={{ fontSize: '11px', color: r.ok ? '#44ff88' : '#ff8888' }}>
              {r.id}: {r.ok ? JSON.stringify(r.payload) : r.error}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function APIStatus() {
  const { data, loading, refetch } = useApi('/diagnostics/apis', { refreshInterval: 60000 });
  const [refreshing, setRefreshing] = useState(false);

  async function forceRefresh() {
    setRefreshing(true);
    try {
      await fetch('/api/diagnostics/apis?force=1');
      if (refetch) await refetch();
    } finally {
      setRefreshing(false);
    }
  }

  const apis = data?.apis || [];
  const summary = data?.summary;

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
        <h2 style={{ fontSize: '18px', margin: 0 }}>API Status</h2>
        <button
          onClick={forceRefresh}
          disabled={refreshing}
          style={{
            background: '#1a1a2e',
            border: '1px solid #333',
            color: '#fff',
            padding: '6px 14px',
            borderRadius: '4px',
            cursor: refreshing ? 'default' : 'pointer',
            fontSize: '12px',
          }}
        >
          {refreshing ? 'Checking…' : 'Refresh now'}
        </button>
      </div>

      <SummaryBar summary={summary} />

      <HealthAuditPanel />

      <SeedActions />

      {loading && !data && (
        <div style={{ color: '#666', padding: '24px' }}>Running health checks…</div>
      )}

      <div>
        {apis.map((api) => (
          <APIRow key={api.name} api={api} />
        ))}
      </div>

      {data?.checked_at && (
        <div style={{ color: '#444', fontSize: '11px', textAlign: 'right', marginTop: '12px' }}>
          Results cached 60s · Last full check: {formatDate(data.checked_at)}
        </div>
      )}
    </div>
  );
}

export default APIStatus;
