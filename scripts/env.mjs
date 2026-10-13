// Minimal .env reader shared by the npm helper scripts (process env wins over the file).
import { existsSync, readFileSync } from "node:fs";

export function readEnv(path = ".env") {
  const values = {};
  if (existsSync(path)) {
    for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
      const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
      if (match) values[match[1]] = match[2].replace(/^["']|["']$/g, "");
    }
  }
  return { ...values, ...process.env };
}
