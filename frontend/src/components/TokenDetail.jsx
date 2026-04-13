import React from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import { formatUSD, formatPct, formatDate, shortAddress, scoreColor } from '../utils/formatters';
import ScoreGauge from './ScoreGauge';

const cardStyle = {
  background: '#111118',
  border: '1px solid #222',
  borderRadius: '8px',
  padding: '20px',
  marginBottom: '16px',
};

const labelStyle = {
  color: '#666',
  fontSize: '11px',
  textTransform: 'uppercase',
  letterSpacing: '0.5px',
  marginBottom: '4px',
};

const valueStyle = {
  fontSize: '16px',
  color: '#fff',
};

function TokenDetail() {
  const { address } = useParams();
  const navigate = useNavigate();
  const { data: token, loading, error } = useApi(`/tokens/${address}`, { refreshInterval: 60000 });
  const { data: history } = useApi(`/tokens/${address}/history`, { refreshInterval: 60000 });

  if (loading && !token) {
    return <div style={{ color: '#666', padding: '40px' }}>Loading token data...</div>;
  }

  if (error) {
    return <div style={{ color: '#ff4444', padding: '40px' }}>Error: {error}</div>;
  }

  if (!token) return null;

  const profile = token.profile || {};
  const watchlist = token.watchlist || {};
  const score = watchlist.current_score || 0;
  const breakdown = watchlist.score_breakdown || {};

  return (
    <div>
      {/* Back button */}
      <button
        onClick={() => navigate('/')}
        style={{
          background: 'none', border: '1px solid #333', color: '#888',
          padding: '6px 14px', borderRadius: '4px', cursor: 'pointer',
          marginBottom: '20px', fontSize: '13px',
        }}
      >
        Back to Dashboard
      </button>

      {/* Header */}
      <div style={{ ...cardStyle, display: 'flex', alignItems: 'center', gap: '24px' }}>
        <ScoreGauge score={score} size={80} />
        <div>
          <h1 style={{ fontSize: '24px', margin: '0 0 4px 0' }}>
            {token.token_symbol} <span style={{ color: '#555', fontSize: '16px' }}>{token.token_name}</span>
          </h1>
          <div style={{ color: '#555', fontSize: '12px' }}>
            {token.contract_address} | BSC
          </div>
          <div style={{ marginTop: '8px', display: 'flex', gap: '12px', fontSize: '13px' }}>
            {token.dex_url && (
              <a href={token.dex_url} target="_blank" rel="noopener noreferrer" style={{ color: '#44aaff' }}>
                DEX Screener
              </a>
            )}
            <a
              href={`https://bscscan.com/token/${token.contract_address}`}
              target="_blank" rel="noopener noreferrer"
              style={{ color: '#44aaff' }}
            >
              BscScan
            </a>
          </div>
        </div>
        <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
          <div style={{
            color: watchlist.confidence_level === 'HIGH' ? '#ff4444' : '#ffaa00',
            fontWeight: 'bold', fontSize: '14px',
          }}>
            {watchlist.confidence_level || 'N/A'} CONFIDENCE
          </div>
          <div style={{ color: '#888', fontSize: '12px' }}>
            Status: {token.status}
          </div>
        </div>
      </div>

      {/* Metrics grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '12px', marginBottom: '16px' }}>
        {[
          { label: 'Price', value: formatUSD(token.price_usd) },
          { label: '24h Volume', value: formatUSD(token.volume_24h) },
          { label: 'Market Cap', value: formatUSD(token.market_cap) },
          { label: 'FDV', value: formatUSD(token.fdv) },
          { label: 'Liquidity', value: formatUSD(token.liquidity_usd) },
          { label: 'Float', value: formatPct(profile.float_pct) },
          { label: 'Top 10 Holders', value: formatPct(profile.top_10_holder_pct) },
          { label: 'Top 1 Holder', value: formatPct(profile.top_1_holder_pct) },
          { label: 'Token Age', value: profile.token_age_days ? `${profile.token_age_days} days` : 'N/A' },
          { label: 'Deployer', value: shortAddress(profile.deployer_address) },
          { label: 'Verified', value: profile.is_contract_verified ? 'Yes' : 'No' },
          { label: 'Binance Alpha', value: profile.binance_alpha ? 'Yes' : 'No' },
        ].map((item, i) => (
          <div key={i} style={cardStyle}>
            <div style={labelStyle}>{item.label}</div>
            <div style={valueStyle}>{item.value}</div>
          </div>
        ))}
      </div>

      {/* Score breakdown */}
      {Object.keys(breakdown).length > 0 && (
        <div style={cardStyle}>
          <h3 style={{ fontSize: '14px', marginBottom: '12px', color: '#888' }}>Score Breakdown</h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))', gap: '8px' }}>
            {Object.entries(breakdown).map(([signal, points]) => (
              <div key={signal} style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '6px 10px', background: '#0a0a0f', borderRadius: '4px',
              }}>
                <span style={{ color: '#aaa', fontSize: '12px' }}>{signal.replace(/_/g, ' ')}</span>
                <span style={{ color: scoreColor(points * 4), fontWeight: 'bold', fontSize: '12px' }}>+{points}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Contract red flags */}
      <div style={cardStyle}>
        <h3 style={{ fontSize: '14px', marginBottom: '12px', color: '#888' }}>Contract Flags</h3>
        <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', fontSize: '13px' }}>
          <span style={{ color: profile.has_mint_function ? '#ff4444' : '#333' }}>
            Mint: {profile.has_mint_function ? 'YES' : 'No'}
          </span>
          <span style={{ color: profile.has_pause_function ? '#ff4444' : '#333' }}>
            Pause: {profile.has_pause_function ? 'YES' : 'No'}
          </span>
          <span style={{ color: profile.has_blacklist_function ? '#ff4444' : '#333' }}>
            Blacklist: {profile.has_blacklist_function ? 'YES' : 'No'}
          </span>
          <span style={{ color: profile.has_proxy ? '#ffaa00' : '#333' }}>
            Proxy: {profile.has_proxy ? 'YES' : 'No'}
          </span>
        </div>
        {profile.narrative_tags && profile.narrative_tags.length > 0 && (
          <div style={{ marginTop: '10px' }}>
            <span style={{ color: '#666', fontSize: '11px' }}>Narrative: </span>
            {profile.narrative_tags.map((tag, i) => (
              <span key={i} style={{
                background: '#1a1a2e', color: '#44aaff', padding: '2px 8px',
                borderRadius: '10px', fontSize: '11px', marginRight: '6px',
              }}>
                {tag}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Cluster & Exchange flow */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '16px' }}>
        <div style={cardStyle}>
          <h3 style={{ fontSize: '14px', marginBottom: '8px', color: '#888' }}>Wallet Clusters</h3>
          {watchlist.cluster_detected ? (
            <div>
              <div style={{ color: '#ff4444', fontWeight: 'bold' }}>CLUSTER DETECTED</div>
              <div style={{ color: '#aaa', fontSize: '13px', marginTop: '4px' }}>
                {watchlist.cluster_wallet_count} wallets identified
              </div>
              {watchlist.cluster_funding_sources && (
                <div style={{ color: '#666', fontSize: '12px', marginTop: '4px' }}>
                  Funding sources: {watchlist.cluster_funding_sources.map(shortAddress).join(', ')}
                </div>
              )}
            </div>
          ) : (
            <div style={{ color: '#444' }}>No clusters detected</div>
          )}
        </div>
        <div style={cardStyle}>
          <h3 style={{ fontSize: '14px', marginBottom: '8px', color: '#888' }}>Exchange Flows</h3>
          {watchlist.exchange_deposits_detected ? (
            <div>
              <div style={{ color: '#ff4444', fontWeight: 'bold' }}>DEPOSITS DETECTED</div>
              <div style={{ color: '#aaa', fontSize: '13px', marginTop: '4px' }}>
                Volume: {formatUSD(watchlist.exchange_deposit_volume)}
              </div>
            </div>
          ) : (
            <div style={{ color: '#444' }}>No exchange deposits detected</div>
          )}
        </div>
      </div>

      {/* AI Briefing */}
      {watchlist.ai_briefing && (
        <div style={cardStyle}>
          <h3 style={{ fontSize: '14px', marginBottom: '8px', color: '#888' }}>AI Briefing</h3>
          <div style={{
            color: '#ccc', fontSize: '13px', lineHeight: '1.6',
            whiteSpace: 'pre-wrap', background: '#0a0a0f',
            padding: '16px', borderRadius: '4px',
          }}>
            {watchlist.ai_briefing}
          </div>
        </div>
      )}

      {/* Score history */}
      {history && history.length > 0 && (
        <div style={cardStyle}>
          <h3 style={{ fontSize: '14px', marginBottom: '8px', color: '#888' }}>Score History</h3>
          <div style={{ maxHeight: '200px', overflowY: 'auto' }}>
            {history.map((h, i) => (
              <div key={i} style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '6px 10px', borderBottom: '1px solid #1a1a1a',
                fontSize: '12px',
              }}>
                <span style={{ color: '#555' }}>{formatDate(h.recorded_at)}</span>
                <span style={{ color: scoreColor(h.score), fontWeight: 'bold' }}>{h.score}/100</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default TokenDetail;
