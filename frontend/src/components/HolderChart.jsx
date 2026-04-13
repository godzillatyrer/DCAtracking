import React from 'react';
import { shortAddress } from '../utils/formatters';

/**
 * Simple horizontal bar chart showing top holder distribution.
 * No external charting library needed.
 */
function HolderChart({ holders = [] }) {
  if (!holders || holders.length === 0) {
    return <div style={{ color: '#444', fontSize: '13px' }}>No holder data available</div>;
  }

  const maxBalance = Math.max(...holders.map(h => Number(h.balance) || 0));

  return (
    <div>
      {holders.slice(0, 10).map((holder, i) => {
        const balance = Number(holder.balance) || 0;
        const pct = maxBalance > 0 ? (balance / maxBalance) * 100 : 0;
        const isCluster = holder.is_known_operator || holder.is_cluster;

        return (
          <div key={i} style={{
            display: 'flex', alignItems: 'center', gap: '10px',
            marginBottom: '6px', fontSize: '12px',
          }}>
            <span style={{
              width: '20px', textAlign: 'right', color: '#555',
            }}>
              #{i + 1}
            </span>
            <span style={{
              width: '100px', fontFamily: 'monospace',
              color: isCluster ? '#ff4444' : '#888',
            }}>
              {shortAddress(holder.address)}
            </span>
            <div style={{
              flex: 1, height: '16px', background: '#1a1a1a',
              borderRadius: '2px', overflow: 'hidden',
            }}>
              <div style={{
                width: `${pct}%`, height: '100%',
                background: isCluster ? '#ff4444' :
                            i === 0 ? '#ffaa00' : '#44aaff',
                borderRadius: '2px',
                transition: 'width 0.3s',
              }} />
            </div>
            {isCluster && (
              <span style={{ color: '#ff4444', fontSize: '10px', fontWeight: 'bold' }}>
                CLUSTER
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default HolderChart;
