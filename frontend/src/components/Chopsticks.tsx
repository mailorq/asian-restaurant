import { useId } from "react";

// a pair of lacquered chopsticks resting on the rim of the hero plate. the round look comes from a gradient
// across each stick and the soft shadow under them; the size is fixed by the view box, so the layout never moves
export function Chopsticks({ className = "" }: { className?: string }) {
  const id = useId();
  const lacquer = `${id}-lacquer`;
  const gold = `${id}-gold`;
  const shade = `${id}-shade`;

  // one stick, 300 long, 15 thick at the top end and 7 at the tip
  const stick = "M0 -7.5 L 300 -3.5 Q 306 -3.2 306 0 Q 306 3.2 300 3.5 L 0 7.5 Q -3 0 0 -7.5 Z";

  return (
    <svg viewBox="-20 -40 360 110" aria-hidden className={className}>
      <defs>
        <linearGradient id={lacquer} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#0e0a08" />
          <stop offset="0.22" stopColor="#4a2e20" />
          <stop offset="0.36" stopColor="#9a6a4b" />
          <stop offset="0.5" stopColor="#4d3022" />
          <stop offset="1" stopColor="#0b0806" />
        </linearGradient>
        <linearGradient id={gold} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#5c410f" />
          <stop offset="0.35" stopColor="#f0d08a" />
          <stop offset="0.6" stopColor="#c09234" />
          <stop offset="1" stopColor="#4f380c" />
        </linearGradient>
        <filter id={shade} x="-10%" y="-100%" width="120%" height="300%">
          <feGaussianBlur stdDeviation="6" />
        </filter>
      </defs>

      <g filter={`url(#${shade})`} opacity="0.55">
        <path d="M0 14 L 306 10 L 306 22 L 0 34 Z" fill="#000" />
      </g>

      <g transform="translate(0 -6) rotate(-2.5)">
        <path d={stick} fill={`url(#${lacquer})`} />
        <rect x="34" y="-7.2" width="30" height="14.4" rx="2" fill={`url(#${gold})`} />
      </g>
      <g transform="translate(8 16) rotate(1.5)">
        <path d={stick} fill={`url(#${lacquer})`} />
        <rect x="34" y="-7.2" width="30" height="14.4" rx="2" fill={`url(#${gold})`} />
      </g>
    </svg>
  );
}
