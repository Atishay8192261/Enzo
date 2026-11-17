import { Text } from "ink";

/** Shows the line being typed. Keys are handled once, at the app level, so none can be dropped. */
export function LineInput({ value, placeholder }: { value: string; placeholder: string }) {
  return (
    <Text>
      <Text color="cyan">› </Text>
      {value ? <Text>{value}</Text> : <Text dimColor>{placeholder}</Text>}
    </Text>
  );
}
