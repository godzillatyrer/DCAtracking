/**
 * Design system for PumpScanner.
 *
 * Dark, professional, fintech-adjacent aesthetic. Big typography for
 * hero metrics, refined color palette with a single accent (blue).
 */

export const colors = {
  // Backgrounds
  bg:        '#0b0d12',
  bgElev1:   '#12151c',
  bgElev2:   '#191d26',
  bgHover:   '#1e222c',

  // Borders
  border:    '#1d1f27',
  borderStrong: '#2a2e3a',

  // Text
  text:      '#f5f7fa',
  textDim:   '#9099ab',
  textFaint: '#5e6675',
  textMuted: '#3f4553',

  // Accent
  accent:     '#3b82f6',   // blue
  accentDim:  '#1e3a6e',
  accentSoft: 'rgba(59, 130, 246, 0.12)',

  // Semantic
  success:   '#10b981',
  successSoft: 'rgba(16, 185, 129, 0.12)',
  warning:   '#f59e0b',
  warningSoft: 'rgba(245, 158, 11, 0.12)',
  danger:    '#ef4444',
  dangerSoft: 'rgba(239, 68, 68, 0.12)',
};

export const typography = {
  // Stacks
  sans: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  mono: "'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace",

  // Sizes
  display:   '40px',
  h1:        '30px',
  h2:        '22px',
  h3:        '17px',
  body:      '14px',
  small:     '13px',
  tiny:      '11px',

  // Weights
  regular:   400,
  medium:    500,
  semibold:  600,
  bold:      700,
};

export const spacing = {
  xs: '6px',
  sm: '10px',
  md: '16px',
  lg: '24px',
  xl: '32px',
  xxl: '48px',
};

export const radius = {
  sm: '6px',
  md: '10px',
  lg: '14px',
};

export const shadows = {
  sm: '0 1px 2px rgba(0,0,0,0.3)',
  md: '0 4px 12px rgba(0,0,0,0.25)',
  glow: '0 0 24px rgba(59, 130, 246, 0.15)',
};

// Composite style presets
export const card = {
  background: colors.bgElev1,
  border: `1px solid ${colors.border}`,
  borderRadius: radius.md,
  padding: spacing.lg,
};

export const cardHero = {
  ...card,
  background: `linear-gradient(135deg, ${colors.bgElev1} 0%, ${colors.bgElev2} 100%)`,
  padding: spacing.xl,
};

export const metricValue = {
  fontSize: typography.display,
  fontWeight: typography.bold,
  color: colors.text,
  letterSpacing: '-0.02em',
  lineHeight: 1,
};

export const metricLabel = {
  fontSize: typography.tiny,
  color: colors.textFaint,
  textTransform: 'uppercase',
  letterSpacing: '1.2px',
  fontWeight: typography.medium,
  marginBottom: spacing.sm,
};

export const sectionTitle = {
  fontSize: typography.h2,
  fontWeight: typography.semibold,
  color: colors.text,
  marginBottom: spacing.md,
  letterSpacing: '-0.01em',
};

export const pageTitle = {
  fontSize: typography.h1,
  fontWeight: typography.bold,
  color: colors.text,
  marginBottom: spacing.xs,
  letterSpacing: '-0.02em',
};

export const pageSubtitle = {
  fontSize: typography.body,
  color: colors.textDim,
  marginBottom: spacing.xl,
};

export const buttonPrimary = {
  background: colors.accent,
  color: '#fff',
  border: 'none',
  padding: '10px 20px',
  borderRadius: radius.sm,
  fontSize: typography.body,
  fontWeight: typography.semibold,
  cursor: 'pointer',
  transition: 'background 0.15s',
  letterSpacing: '-0.005em',
};

export const buttonSecondary = {
  background: 'transparent',
  color: colors.text,
  border: `1px solid ${colors.borderStrong}`,
  padding: '10px 20px',
  borderRadius: radius.sm,
  fontSize: typography.body,
  fontWeight: typography.medium,
  cursor: 'pointer',
  transition: 'all 0.15s',
};

export const input = {
  background: colors.bg,
  border: `1px solid ${colors.borderStrong}`,
  color: colors.text,
  padding: '11px 14px',
  borderRadius: radius.sm,
  fontSize: typography.body,
  outline: 'none',
  transition: 'border-color 0.15s',
};

export const th = {
  textAlign: 'left',
  padding: '12px 16px',
  color: colors.textFaint,
  fontSize: typography.tiny,
  textTransform: 'uppercase',
  letterSpacing: '1px',
  fontWeight: typography.semibold,
  borderBottom: `1px solid ${colors.border}`,
  background: colors.bg,
};

export const td = {
  padding: '14px 16px',
  borderBottom: `1px solid ${colors.border}`,
  fontSize: typography.body,
  color: colors.text,
};

export const pill = (variant = 'default') => {
  const variants = {
    default:  { bg: colors.bgElev2,      color: colors.textDim },
    accent:   { bg: colors.accentSoft,   color: colors.accent },
    success:  { bg: colors.successSoft,  color: colors.success },
    warning:  { bg: colors.warningSoft,  color: colors.warning },
    danger:   { bg: colors.dangerSoft,   color: colors.danger },
  };
  const v = variants[variant] || variants.default;
  return {
    display: 'inline-block',
    padding: '4px 10px',
    borderRadius: '999px',
    fontSize: typography.tiny,
    fontWeight: typography.semibold,
    background: v.bg,
    color: v.color,
    letterSpacing: '0.3px',
    textTransform: 'uppercase',
  };
};
