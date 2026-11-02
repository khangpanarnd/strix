import { memo, type CSSProperties } from "react";

/**
 * The landing page mascot (public/penguin.png, supplied as assets/penguin.png).
 *
 * It is a single illustrated pose, so the old per-role limb animations do not apply;
 * a penguin moves as a whole instead. `motion` picks how:
 *   bob     a slow float (default)
 *   waddle  a quick side-to-side tilt, for walking and climbing
 *   hop     a repeated little jump, for working
 *   wiggle  a cheerful rock, for waving
 *
 * Decorative: empty alt text and aria-hidden, like the characters it replaces. The
 * motion itself is defined in lp.base.css and switched off by prefers-reduced-motion.
 */

export type PenguinMotion = "bob" | "waddle" | "hop" | "wiggle";

interface PenguinProps {
  motion?: PenguinMotion;
  style?: CSSProperties;
  className?: string;
}

const SRC = "./penguin.png";
const WIDTH = 400;
const HEIGHT = 389;

export const Penguin = memo(function Penguin({ motion = "bob", style, className }: PenguinProps) {
  return (
    <span className={className ? `ch-host ${className}` : "ch-host"} style={style} aria-hidden="true">
      <img
        className={`ch motion-${motion}`}
        src={SRC}
        width={WIDTH}
        height={HEIGHT}
        alt=""
        decoding="async"
        draggable={false}
      />
    </span>
  );
});
