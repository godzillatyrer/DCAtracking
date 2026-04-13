import React, { useState } from 'react';
import { useApi, apiPost } from '../hooks/useApi';
import { formatUSD, formatDate, shortAddress } from '../utils/formatters';
import ScoreGauge from './ScoreGauge';

const cardStyle = {
  background: '#111118',
  border: '1px solid #222',
  borderRadius: '8px',
  padding: '20px',
  marginBottom: '16px',
};

const statStyle = {
  textAlign: 'center',
  padding: '12px',
};

function AlertLog() {
  const { data: alerts, refetch } = useApi('/alerts?per_page=100', { refreshInterval: 30000 });
  const { data: accuracy } = useApi('/alerts/accuracy/summary', { refreshInterval: 60000 });
  const [editing, setEditing] = useState(null);
  const [outcomeForm, setOutcomeForm] = useState({ outcome: 'pumped', review_notes: '' });

  async function submitOutcome(alertId) {
    try {
      await apiPost(`/alerts/${alertId}/outcome`, outcomeForm);
      setEditing(null);
      refetch();
    } catch (err) {
      alert('Error: ' + err.message);
    }
  }

  const stats = accuracy || {};

  return (
    <div>
      <h2 style={{ fontSize: '18px', marginBottom: '20px' }}>Alert History & Accuracy</h2>

      {/* Accuracy stats */}
      <div style={{ ...cardStyle, display: 'flex', gap: '24px', justifyContent: 'center', flexWrap: 'wrap' }}>
        <div style={statStyle}>
          <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Total Alerts</div>
          <div style={{ fontSize: '28px', fontWeight: 'bold' }}>{stats.total_alerts || 0}</div>
        </div>
        <div style={statStyle}>
          <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Reviewed</div>
          <div style={{ fontSize: '28px', fontWeight: 'bold' }}>{stats.total_reviewed || 0}</div>
        </div>
        <div style={statStyle}>
          <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Wins</div>
          <div style={{ fontSize: '28px', fontWeight: 'bold', color: '#44ff44' }}>{stats.wins || 0}</div>
        </div>
        <div style={statStyle}>
          <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Fizzles</div>
          <div style={{ fontSize: '28px', fontWeight: 'bold', color: '#ff4444' }}>{stats.fizzles || 0}</div>
        </div>
        <div style={statStyle}>
          <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Win Rate</div>
          <div style={{ fontSize: '28px', fontWeight: 'bold', color: stats.win_rate > 50 ? '#44ff44' : '#ffaa00' }}>
            {stats.win_rate || 0}%
          </div>
        </div>
        <div style={statStyle}>
          <div style={{ color: '#666', fontSize: '11px', textTransform: 'uppercase' }}>Pending Review</div>
          <div style={{ fontSize: '28px', fontWeight: 'bold', color: '#ffaa00' }}>{stats.pending_review || 0}</div>
        </div>
      </div>

      {/* Alert list */}
      <div style={cardStyle}>
        {(alerts?.items || []).map((a) => (
          <div key={a.id} style={{
            padding: '16px',
            borderBottom: '1px solid #1a1a1a',
            display: 'flex',
            gap: '16px',
            alignItems: 'flex-start',
          }}>
            <ScoreGauge score={a.score_at_alert || 0} size={50} />
            <div style={{ flex: 1 }}>
              <div style={{ display: 'flex', gap: '12px', alignItems: 'center', marginBottom: '6px' }}>
                <span style={{ color: '#fff', fontWeight: 'bold', fontSize: '15px' }}>
                  {a.token_symbol || shortAddress(a.contract_address)}
                </span>
                <span style={{
                  padding: '2px 8px', borderRadius: '4px', fontSize: '10px',
                  background: a.alert_type === 'score_threshold' ? '#1a1a2e' : '#2e1a1a',
                  color: a.alert_type === 'score_threshold' ? '#44aaff' : '#ff4444',
                }}>
                  {a.alert_type}
                </span>
                {a.outcome && (
                  <span style={{
                    padding: '2px 8px', borderRadius: '4px', fontSize: '10px',
                    background: a.outcome === 'pumped' ? '#113311' : '#331111',
                    color: a.outcome === 'pumped' ? '#44ff44' : '#ff4444',
                  }}>
                    {a.outcome.toUpperCase()}
                  </span>
                )}
                {a.telegram_sent && (
                  <span style={{ color: '#44aaff', fontSize: '11px' }}>TG Sent</span>
                )}
              </div>
              <div style={{ color: '#888', fontSize: '12px', marginBottom: '4px' }}>
                {a.trigger_reason}
              </div>
              {a.ai_briefing && (
                <div style={{
                  color: '#aaa', fontSize: '12px', lineHeight: '1.5',
                  background: '#0a0a0f', padding: '8px 12px', borderRadius: '4px',
                  marginTop: '8px', maxHeight: '100px', overflow: 'hidden',
                }}>
                  {a.ai_briefing}
                </div>
              )}
              <div style={{ color: '#444', fontSize: '11px', marginTop: '6px' }}>
                Fired: {formatDate(a.fired_at)} | Price: {formatUSD(a.price_at_alert)} | MCap: {formatUSD(a.market_cap_at_alert)}
                {a.peak_pct_from_alert && ` | Peak: +${a.peak_pct_from_alert}%`}
              </div>

              {/* Outcome editing */}
              {!a.reviewed && (
                <div style={{ marginTop: '8px' }}>
                  {editing === a.id ? (
                    <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                      <select
                        value={outcomeForm.outcome}
                        onChange={(e) => setOutcomeForm(prev => ({ ...prev, outcome: e.target.value }))}
                        style={{
                          background: '#0a0a0f', border: '1px solid #333', color: '#fff',
                          padding: '4px 8px', borderRadius: '4px', fontSize: '12px',
                        }}
                      >
                        <option value="pumped">Pumped</option>
                        <option value="fizzled">Fizzled</option>
                        <option value="still_active">Still Active</option>
                      </select>
                      <input
                        type="text"
                        placeholder="Notes..."
                        value={outcomeForm.review_notes}
                        onChange={(e) => setOutcomeForm(prev => ({ ...prev, review_notes: e.target.value }))}
                        style={{
                          background: '#0a0a0f', border: '1px solid #333', color: '#fff',
                          padding: '4px 8px', borderRadius: '4px', fontSize: '12px', flex: 1,
                        }}
                      />
                      <button
                        onClick={() => submitOutcome(a.id)}
                        style={{
                          background: '#113311', border: '1px solid #44ff44', color: '#44ff44',
                          padding: '4px 12px', borderRadius: '4px', cursor: 'pointer', fontSize: '12px',
                        }}
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditing(null)}
                        style={{
                          background: 'none', border: '1px solid #333', color: '#666',
                          padding: '4px 12px', borderRadius: '4px', cursor: 'pointer', fontSize: '12px',
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => setEditing(a.id)}
                      style={{
                        background: 'none', border: '1px solid #333', color: '#888',
                        padding: '4px 12px', borderRadius: '4px', cursor: 'pointer', fontSize: '11px',
                      }}
                    >
                      Log Outcome
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}
        {(!alerts?.items || alerts.items.length === 0) && (
          <div style={{ padding: '40px', textAlign: 'center', color: '#444' }}>
            No alerts fired yet. Scanner will generate alerts when tokens cross the score threshold.
          </div>
        )}
      </div>
    </div>
  );
}

export default AlertLog;
