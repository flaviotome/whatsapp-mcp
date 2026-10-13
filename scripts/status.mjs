// Prints the service status; with --open, also opens the QR pairing page in the browser.
import { exec, execSync } from "node:child_process";
import { readEnv } from "./env.mjs";

const env = readEnv();
const base = `http://localhost:${env.HOST_PORT || "8000"}`;

function token() {
  if (env.MCP_AUTH_TOKEN) return env.MCP_AUTH_TOKEN;
  try {
    // Auto-generated token persisted by the server in the data volume.
    return execSync("docker compose exec -T whatsapp-mcp cat /data/auth_token", { stdio: ["ignore", "pipe", "ignore"] })
      .toString()
      .trim();
  } catch {
    return "";
  }
}

const tok = token();
try {
  const res = await fetch(`${base}/status`, { headers: tok ? { Authorization: `Bearer ${tok}` } : {} });
  console.log(JSON.stringify(await res.json(), null, 2));
} catch {
  console.error(`Service not reachable at ${base}. Run "docker compose up -d" first.`);
  process.exitCode = 1;
}

if (process.argv.includes("--open")) {
  const url = tok ? `${base}/?token=${encodeURIComponent(tok)}` : `${base}/`;
  const opener = process.platform === "win32" ? 'start ""' : process.platform === "darwin" ? "open" : "xdg-open";
  console.log(`Opening ${base}/`);
  exec(`${opener} "${url}"`);
}
