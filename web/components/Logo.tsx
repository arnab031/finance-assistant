/**
 * Product mark: two ledger rows and a check.
 *
 * The thesis of the whole system is "every figure traced back to a source row",
 * so the mark is a ledger that has been checked - not a generic chart or dollar
 * sign, which would say nothing this product does not share with every other
 * finance tool. Strokes are deliberately chunky so it survives 16px in a tab.
 */
export default function Logo({ size = 30 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      role="img"
      aria-label="Finsight"
      className="logo"
    >
      <defs>
        <linearGradient id="logo-bg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="var(--accent)" />
          <stop offset="100%" stopColor="var(--accent-ink)" />
        </linearGradient>
      </defs>
      <rect width="24" height="24" rx="6.5" fill="url(#logo-bg)" />
      <g fill="#fff">
        <rect x="5.5" y="6" width="11" height="1.9" rx="0.95" />
        <rect x="5.5" y="10" width="7.5" height="1.9" rx="0.95" />
      </g>
      <path
        d="M5.8 16.4 L8.6 19.1 L16.2 11.6"
        fill="none"
        stroke="#fff"
        strokeWidth="2.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
