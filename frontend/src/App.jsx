import React from 'react';
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import Dashboard from './components/Dashboard';
import TokenDetail from './components/TokenDetail';
import WalletTracker from './components/WalletTracker';
import AlertLog from './components/AlertLog';
import ScanActivity from './components/ScanActivity';

const navStyle = {
  display: 'flex',
  gap: '20px',
  padding: '16px 24px',
  background: '#111118',
  borderBottom: '1px solid #222',
  alignItems: 'center',
};

const linkStyle = {
  color: '#888',
  textDecoration: 'none',
  fontSize: '14px',
  padding: '6px 12px',
  borderRadius: '4px',
  transition: 'all 0.2s',
};

const activeLinkStyle = {
  ...linkStyle,
  color: '#fff',
  background: '#1a1a2e',
};

function App() {
  return (
    <BrowserRouter>
      <div style={{ minHeight: '100vh', background: '#0a0a0f', color: '#e0e0e0' }}>
        <nav style={navStyle}>
          <span style={{ color: '#ff4444', fontWeight: 'bold', fontSize: '16px', marginRight: '20px' }}>
            BSC PUMP SCANNER
          </span>
          <NavLink to="/" style={({ isActive }) => isActive ? activeLinkStyle : linkStyle} end>
            Dashboard
          </NavLink>
          <NavLink to="/wallets" style={({ isActive }) => isActive ? activeLinkStyle : linkStyle}>
            Wallet Tracker
          </NavLink>
          <NavLink to="/alerts" style={({ isActive }) => isActive ? activeLinkStyle : linkStyle}>
            Alert Log
          </NavLink>
          <NavLink to="/activity" style={({ isActive }) => isActive ? activeLinkStyle : linkStyle}>
            Scanner Activity
          </NavLink>
        </nav>
        <div style={{ padding: '24px' }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/token/:address" element={<TokenDetail />} />
            <Route path="/wallets" element={<WalletTracker />} />
            <Route path="/alerts" element={<AlertLog />} />
            <Route path="/activity" element={<ScanActivity />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  );
}

export default App;
