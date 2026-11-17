import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ConversationMemory } from "@enzo/chat";

/** The terminal remembers its conversation in ~/.config/enzo (or ENZO_CONFIG_DIR / XDG_CONFIG_HOME). */
export function fileMemory(env: NodeJS.ProcessEnv = process.env): ConversationMemory {
  const dir =
    env.ENZO_CONFIG_DIR ?? join(env.XDG_CONFIG_HOME ?? join(homedir(), ".config"), "enzo");
  const file = join(dir, "conversation");
  return {
    get() {
      try {
        return readFileSync(file, "utf8").trim() || null;
      } catch {
        return null; // first run
      }
    },
    set(id) {
      mkdirSync(dir, { recursive: true });
      writeFileSync(file, `${id}\n`, { mode: 0o600 });
    },
  };
}
