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
          {api.error && (
            <div style={{ color: '#ff8888', marginBottom: '6px' }}>
              <b>Live error:</b> {api.error}
            </div>
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
