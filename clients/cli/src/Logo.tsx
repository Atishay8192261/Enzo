import { COLOR, WORDMARK } from "./theme.ts";

/** Below this width or height the big wordmark does not fit: a plain title instead (#35). */
export const WORDMARK_MIN_WIDTH = 60;
export const WORDMARK_MIN_HEIGHT = 20;

export function Logo({ width, height }: { width: number; height: number }) {
  if (width < WORDMARK_MIN_WIDTH || height < WORDMARK_MIN_HEIGHT) {
    return (
      <text fg={COLOR.accent}>
        <strong>Enzo</strong>
      </text>
    );
  }
  return <ascii-font text="ENZO" font="block" color={WORDMARK} selectable={false} />;
}
