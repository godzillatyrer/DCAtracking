import React from 'react';
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import Dashboard from './components/Dashboard';
import TokenDetail from './components/TokenDetail';
import WalletTracker from './components/WalletTracker';
import AlertLog from './components/AlertLog';
import ScanActivity from './components/ScanActivity';
import APIStatus from './components/APIStatus';

const LOGO = 'PUMP SCANNER';

const navOuter = {
  borderBottom: '1px solid #1d1f27',
  background: 'linear-gradient(180deg, #0e1014 0%, #0b0d12 100%)',
  backdropFilter: 'blur(8px)',
};

const navInner = {
  display: 'flex',
  gap: '32px',
  padding: '18px 32px',
  alignItems: 'center',
  maxWidth: '1400px',
  margin: '0 auto',
};

const linkStyle = {
  color: '#7a7f8c',
  textDecoration: 'none',
  fontSize: '14px',
  fontWeight: 500,
  padding: '8px 0',
  borderBottom: '2px solid transparent',
  transition: 'color 0.15s, border-color 0.15s',
};

const activeLinkStyle = {
  ...linkStyle,
  color: '#f5f7fa',
  borderBottomColor: '#3b82f6',
};

function App() {
  return (
    <BrowserRouter>
      <div style={{
        minHeight: '100vh',
        background: '#0b0d12',
        color: '#e6e8ec',
        fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
      }}>
        <nav style={navOuter}>
          <div style={navInner}>
            <span style={{
              color: '#f5f7fa',
              fontWeight: 700,
              fontSize: '15px',
              letterSpacing: '2px',
              marginRight: '24px',
            }}>
              <span style={{ color: '#3b82f6' }}>◆</span> {LOGO}
            </span>
            <NavLink to="/" style={({ isActive }) => isActive ? activeLinkStyle : linkStyle} end>
              Dashboard
            </NavLink>
            <NavLink to="/wallets" style={({ isActive }) => isActive ? activeLinkStyle : linkStyle}>
              Wallet Tracker
            </NavLink>
            <NavLink to="/alerts" style={({ isActive }) => isActive ? activeLinkStyle : linkStyle}>
              Alerts
            </NavLink>
            <NavLink to="/activity" style={({ isActive }) => isActive ? activeLinkStyle : linkStyle}>
              Activity
            </NavLink>
            <NavLink to="/api-status" style={({ isActive }) => isActive ? activeLinkStyle : linkStyle}>
              API Status
            </NavLink>
          </div>
        </nav>
        <div style={{ maxWidth: '1400px', margin: '0 auto', padding: '32px' }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/token/:address" element={<TokenDetail />} />
            <Route path="/wallets" element={<WalletTracker />} />
            <Route path="/alerts" element={<AlertLog />} />
            <Route path="/activity" element={<ScanActivity />} />
            <Route path="/api-status" element={<APIStatus />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  );
}

export default App;
