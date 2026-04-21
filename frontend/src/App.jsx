import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import WalletTracker from './components/WalletTracker';
import { colors, typography } from './theme';

const navOuter = {
  background: 'rgba(255, 255, 255, 0.72)',
  backdropFilter: 'saturate(180%) blur(20px)',
  WebkitBackdropFilter: 'saturate(180%) blur(20px)',
  borderBottom: `1px solid ${colors.border}`,
  position: 'sticky',
  top: 0,
  zIndex: 100,
};

const navInner = {
  display: 'flex',
  gap: '28px',
  padding: '16px 32px',
  alignItems: 'center',
  maxWidth: '1400px',
  margin: '0 auto',
};

function App() {
  return (
    <BrowserRouter>
      <div style={{
        minHeight: '100vh',
        background: colors.bg,
        color: colors.text,
        fontFamily: typography.sans,
      }}>
        <nav style={navOuter}>
          <div style={navInner}>
            <span style={{
              color: colors.text,
              fontWeight: typography.bold,
              fontSize: '17px',
              letterSpacing: '-0.02em',
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
            }}>
              <span style={{
                width: '22px', height: '22px',
                borderRadius: '6px',
                background: `linear-gradient(135deg, ${colors.accent} 0%, ${colors.purple} 100%)`,
                boxShadow: `0 2px 8px ${colors.accentSoft}`,
              }} />
              Solana Cabal Tracker
            </span>
          </div>
        </nav>
        <div style={{ maxWidth: '1400px', margin: '0 auto', padding: '40px 32px' }}>
          <Routes>
            <Route path="/" element={<WalletTracker />} />
            <Route path="*" element={<WalletTracker />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  );
}

export default App;
