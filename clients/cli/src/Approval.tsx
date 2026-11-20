import { describeApproval, type PendingApproval } from "@enzo/chat";
import { COLOR } from "./theme.ts";

/** The exact action waiting for a yes or no, with its exact arguments (spec §12.3). */
export function ApprovalCard({ approval }: { approval: PendingApproval }) {
  return (
    <box
      border
      borderStyle="rounded"
      borderColor={COLOR.warn}
      title=" Approve this action? "
      paddingLeft={1}
      paddingRight={1}
      marginTop={1}
      flexDirection="column"
      flexShrink={0}
    >
      <text>
        <strong>{describeApproval(approval)}</strong>
      </text>
      <text fg={COLOR.dim}>Exact details: {JSON.stringify(approval.arguments, null, 2)}</text>
      {approval.detail ? <text fg={COLOR.dim}>{clip(approval.detail, 12)}</text> : null}
      <text>
        <span fg={COLOR.ok}>y</span> approve · <span fg={COLOR.error}>n</span> decline
      </text>
    </box>
  );
}

/** The first lines of a long detail (a diff): the exact action above it stays on screen. */
export function clip(text: string, lines: number): string {
  const all = text.split("\n");
  return all.length <= lines
    ? text
    : `${all.slice(0, lines).join("\n")}\n… ${all.length - lines} more lines`;
}
