import { memo, useMemo, type CSSProperties } from "react";
import { characterSvg, type CharacterAnim, type CharacterName } from "./pixel";

interface PixelCharProps {
  name: CharacterName;
  anim?: CharacterAnim;
  style?: CSSProperties;
  className?: string;
}

/**
 * A decorative pixel character. The SVG markup comes from `characterSvg`, which is
 * assembled from constants in pixel.ts and never from user input.
 */
export const PixelChar = memo(function PixelChar({ name, anim, style, className }: PixelCharProps) {
  const html = useMemo(() => characterSvg(name, anim), [name, anim]);
  return (
    <span
      className={className ? `ch-host ${className}` : "ch-host"}
      style={style}
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
});
