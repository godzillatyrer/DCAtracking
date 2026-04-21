/**
 * Design system for PumpScanner — light, Apple-inspired.
 *
 * Soft grey canvas, white elevated surfaces, bold typography, subtle
 * shadows, vibrant accent. Keeps text sizes large + spacing generous
 * so the product looks polished and premium.
 */

export const colors = {
  // Backgrounds
  bg:        '#f5f5f7',          // soft Apple grey canvas
  bgElev1:   '#ffffff',          // white card surface
  bgElev2:   '#fbfbfd',
  bgHover:   '#f0f0f3',
  bgSubtle:  '#fafafa',

  // Borders
  border:    '#e5e5e7',
  borderStrong: '#d2d2d7',

  // Text
  text:      '#1d1d1f',          // Apple black-ish
  textDim:   '#424245',
  textFaint: '#6e6e73',
  textMuted: '#86868b',

  // Accent — vibrant punchy blue (slightly more poppy than Apple's #007aff)
  accent:     '#0066ff',
  accentHover:'#0052cc',
  accentDim:  '#b3d0ff',
  accentSoft: 'rgba(0, 102, 255, 0.10)',

  // Secondary accent — purple for variety / premium accents
  purple:     '#5e5ce6',
  purpleSoft: 'rgba(94, 92, 230, 0.10)',

  // Semantic (Apple palette)
  success:     '#30b54a',
  successSoft: 'rgba(48, 181, 74, 0.10)',
  warning:     '#ff9500',
  warningSoft: 'rgba(255, 149, 0, 0.12)',
  danger:      '#ff3b30',
  dangerSoft:  'rgba(255, 59, 48, 0.10)',
};

export const typography = {
  sans: "'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif",
  mono: "'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace",

  display:   '44px',
  h1:        '32px',
  h2:        '22px',
  h3:        '17px',
  body:      '15px',
  small:     '13px',
  tiny:      '11px',

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
  sm: '8px',
  md: '12px',
  lg: '16px',
  pill: '999px',
};

export const shadows = {
  sm: '0 1px 2px rgba(0,0,0,0.04), 0 1px 3px rgba(0,0,0,0.06)',
  md: '0 4px 12px rgba(0,0,0,0.06), 0 2px 4px rgba(0,0,0,0.04)',
  lg: '0 10px 32px rgba(0,0,0,0.08), 0 4px 12px rgba(0,0,0,0.05)',
  glow: '0 8px 32px rgba(0, 102, 255, 0.15)',
};

export const card = {
  background: colors.bgElev1,
  border: `1px solid ${colors.border}`,
  borderRadius: radius.md,
  padding: spacing.lg,
  boxShadow: shadows.sm,
};

export const cardHero = {
  background: `linear-gradient(135deg, #ffffff 0%, ${colors.bgElev2} 100%)`,
  border: `1px solid ${colors.border}`,
  borderRadius: radius.lg,
  padding: spacing.xl,
  boxShadow: shadows.md,
};

export const metricValue = {
  fontSize: typography.display,
  fontWeight: typography.bold,
  color: colors.text,
  letterSpacing: '-0.03em',
  lineHeight: 1,
};

export const metricLabel = {
  fontSize: typography.tiny,
  color: colors.textFaint,
  textTransform: 'uppercase',
  letterSpacing: '1.2px',
  fontWeight: typography.semibold,
  marginBottom: spacing.sm,
};

export const sectionTitle = {
  fontSize: typography.h2,
  fontWeight: typography.semibold,
  color: colors.text,
  marginBottom: spacing.sm,
  letterSpacing: '-0.015em',
};

export const pageTitle = {
  fontSize: typography.h1,
  fontWeight: typography.bold,
  color: colors.text,
  marginBottom: spacing.xs,
  letterSpacing: '-0.025em',
};

export const pageSubtitle = {
  fontSize: typography.body,
  color: colors.textFaint,
  marginBottom: spacing.xl,
  lineHeight: 1.5,
};

export const buttonPrimary = {
  background: colors.accent,
  color: '#ffffff',
  border: 'none',
  padding: '11px 22px',
  borderRadius: radius.sm,
  fontSize: typography.body,
  fontWeight: typography.semibold,
  cursor: 'pointer',
  transition: 'background 0.15s, transform 0.08s',
  letterSpacing: '-0.005em',
  boxShadow: '0 1px 2px rgba(0, 102, 255, 0.3)',
};

export const buttonSecondary = {
  background: '#ffffff',
  color: colors.text,
  border: `1px solid ${colors.borderStrong}`,
  padding: '11px 22px',
  borderRadius: radius.sm,
  fontSize: typography.body,
  fontWeight: typography.medium,
  cursor: 'pointer',
  transition: 'all 0.15s',
};

export const buttonGhost = {
  background: 'transparent',
  color: colors.textDim,
  border: 'none',
  padding: '8px 14px',
  borderRadius: radius.sm,
  fontSize: typography.small,
  fontWeight: typography.medium,
  cursor: 'pointer',
  transition: 'background 0.15s',
};

export const input = {
  background: '#ffffff',
  border: `1px solid ${colors.borderStrong}`,
  color: colors.text,
  padding: '12px 14px',
  borderRadius: radius.sm,
  fontSize: typography.body,
  outline: 'none',
  transition: 'border-color 0.15s, box-shadow 0.15s',
};

export const th = {
  textAlign: 'left',
  padding: '12px 18px',
  color: colors.textFaint,
  fontSize: typography.tiny,
  textTransform: 'uppercase',
  letterSpacing: '0.8px',
  fontWeight: typography.semibold,
  borderBottom: `1px solid ${colors.border}`,
  background: colors.bgSubtle,
};

export const td = {
  padding: '14px 18px',
  borderBottom: `1px solid ${colors.border}`,
  fontSize: typography.body,
  color: colors.text,
};

export const pill = (variant = 'default') => {
  const variants = {
    default:  { bg: '#efeff2',           color: colors.textDim },
    accent:   { bg: colors.accentSoft,   color: colors.accent },
    purple:   { bg: colors.purpleSoft,   color: colors.purple },
    success:  { bg: colors.successSoft,  color: colors.success },
    warning:  { bg: colors.warningSoft,  color: colors.warning },
    danger:   { bg: colors.dangerSoft,   color: colors.danger },
  };
  const v = variants[variant] || variants.default;
  return {
    display: 'inline-block',
    padding: '4px 10px',
    borderRadius: radius.pill,
    fontSize: typography.tiny,
    fontWeight: typography.semibold,
    background: v.bg,
    color: v.color,
    letterSpacing: '0.3px',
    textTransform: 'uppercase',
  };
};
