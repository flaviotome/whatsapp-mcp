"use strict";

const $ = (id) => document.getElementById(id);
const STATES = {
  starting: ["Iniciando conexão…", "warn", "Iniciando"],
  waiting_qr: ["Escaneie o QR code", "warn", "Aguardando pareamento"],
  waiting_pair_code: ["Digite o código no celular", "warn", "Aguardando pareamento"],
  connected: ["Conectado", "ok", "Online"],
  reconnecting: ["Reconectando…", "warn", "Reconectando"],
  logged_out: ["Desconectado do WhatsApp", "err", "Desconectado"],
  error: ["Erro na conexão", "err", "Erro"],
};
const TOOLS = {
  whatsapp_status: "Consultou o status",
  whatsapp_get_qr_code: "Pediu o QR code",
  list_chats: "Listou conversas",
  list_messages: "Leu mensagens",
  search_messages: "Buscou mensagens",
  search_contacts: "Buscou contatos",
  list_images: "Listou imagens",
  get_message_image: "Abriu uma imagem",
  send_message: "Enviou mensagem",
  react_to_message: "Reagiu a mensagem",
};

let saved = null; // settings as loaded from the server
let allowed = []; // chats in the allowlist being edited: {jid, name}

// ---- helpers --------------------------------------------------------------------
async function call(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("sessão expirada");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `erro ${res.status}`);
  return data;
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 2500);
}

function when(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  const mins = Math.round((Date.now() - d) / 60000);
  if (mins < 1) return "agora";
  if (mins < 60) return `há ${mins} min`;
  if (mins < 1440) return `há ${Math.round(mins / 60)} h`;
  return d.toLocaleDateString("pt-BR");
}

function show(el, visible) {
  el.hidden = !visible;
}

async function waitForRestart() {
  window.scrollTo(0, 0);
  show($("restarting"), true);
  await new Promise((r) => setTimeout(r, 3000));
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch("/api/overview", { cache: "no-store" });
      if (res.ok || res.status === 401) return location.reload();
    } catch (e) {
      /* still restarting */
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  $("restarting").textContent = "O serviço está demorando para voltar. Recarregue a página em instantes.";
}

// ---- tabs ------------------------------------------------------------------------
function route() {
  const tab = (location.hash || "#painel").slice(1);
  for (const name of ["painel", "conectar", "config"]) {
    show($(`tab-${name}`), name === tab);
    document.querySelector(`nav a[data-tab=${name}]`).classList.toggle("on", name === tab);
  }
  if (tab === "conectar") loadConnection();
  if (tab === "config") loadSettings();
}

// ---- dashboard ---------------------------------------------------------------------
async function refresh() {
  let data;
  try {
    data = await call("GET", "/api/overview");
  } catch (e) {
    $("state-title").textContent = "Serviço indisponível";
    return;
  }
  const s = data.status;
  const [title, cls, pill] = STATES[s.state] || [s.state, "warn", s.state];
  $("state-title").textContent = s.state === "connected" && s.phone ? `${title} como +${s.phone}` : title;
  $("dot").className = `dot ${cls}`;
  $("state-pill").className = `pill ${cls}`;
  $("state-pill").textContent = pill;
  show($("state-pill"), true);
  $("state-sub").textContent = s.state === "connected" ? `Última mensagem recebida: ${when(data.stats.last_message_at)}` : "";

  const pairing = !s.authenticated && s.state !== "connected";
  show($("pairing"), pairing);
  show($("qr"), pairing && s.qr_available);
  if (pairing && s.qr_available) $("qr").src = `/qr.png?t=${Date.now()}`;
  show($("pair-code"), Boolean(s.pair_code));
  $("pair-code").textContent = s.pair_code || "";
  $("pair-step").textContent = s.pair_code
    ? 'Toque em "Conectar com número de telefone" e digite o código acima.'
    : "Aponte a câmera para o QR code. Ele se renova a cada ~20 segundos.";
  show($("unlink"), Boolean(s.authenticated));
  show($("last-error"), Boolean(s.last_error));
  $("last-error").textContent = s.last_error ? `Último erro: ${s.last_error}` : "";

  const n = (v) => (typeof v === "number" ? v.toLocaleString("pt-BR") : "–");
  $("m-chats").textContent = n(data.stats.chats);
  $("m-messages").textContent = n(data.stats.messages);
  $("m-last").textContent = when(data.stats.last_message_at);
  $("m-sends").textContent = data.send_enabled ? n(data.sends) : "desligado";

  const list = $("activity");
  list.replaceChildren(
    ...data.activity.map((a) => {
      const li = document.createElement("li");
      const what = document.createElement("span");
      what.textContent = `${TOOLS[a.tool] || a.tool}${a.chat ? ` · ${a.chat}` : ""}${a.ok ? "" : " (falhou)"}`;
      const at = document.createElement("span");
      at.className = "muted small";
      at.textContent = when(a.at);
      li.append(what, at);
      return li;
    })
  );
  show($("activity-empty"), data.activity.length === 0);
}

// ---- connection --------------------------------------------------------------------
let revealed = false;
let connection = null;

function snippets(token) {
  const c = connection;
  return {
    code: `claude mcp add --transport http --scope user whatsapp ${c.mcp_url} --header "Authorization: Bearer ${token}"`,
    desktop: JSON.stringify(
      { mcpServers: { whatsapp: { command: "npx", args: ["-y", "mcp-remote", c.mcp_url, "--header", `Authorization: Bearer ${token}`] } } },
      null,
      2
    ),
  };
}

function renderConnection() {
  const real = snippets(connection.token);
  const shown = revealed ? real : snippets("••••••••");
  $("cmd-code").textContent = shown.code;
  $("cmd-desktop").textContent = shown.desktop;
  // What the copy buttons put on the clipboard: always the real values.
  $("cmd-code").dataset.value = real.code;
  $("cmd-desktop").dataset.value = real.desktop;
  for (const id of ["token", "header-value"]) $(id).type = revealed ? "text" : "password";
  $("token-show").textContent = revealed ? "Ocultar" : "Mostrar";
}

async function loadConnection() {
  connection = await call("GET", "/api/connection");
  $("mcp-url").value = connection.mcp_url;
  $("token").value = connection.token;
  $("header-value").value = `Bearer ${connection.token}`;
  show($("rotate"), !connection.token_from_env);
  show($("token-env"), connection.token_from_env);
  renderConnection();
}

// ---- settings ----------------------------------------------------------------------
function ensureOption(select, value, label) {
  if (![...select.options].some((o) => o.value === String(value))) select.add(new Option(label, value));
  select.value = String(value);
}

function renderChips() {
  $("chips").replaceChildren(
    ...allowed.map((c, i) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = c.name || c.jid;
      chip.title = c.jid;
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = "×";
      x.setAttribute("aria-label", `Remover ${c.name || c.jid}`);
      x.onclick = () => {
        allowed.splice(i, 1);
        renderChips();
      };
      chip.append(x);
      return chip;
    })
  );
}

async function loadSettings() {
  saved = (await call("GET", "/api/settings")).values;
  $("s-send").checked = saved.send_enabled;
  ensureOption($("s-spacing"), saved.send_spacing, saved.send_spacing);
  $("s-presence").checked = saved.presence_mode === "normal";
  ensureOption($("s-prefetch"), saved.media_prefetch_days, `Últimos ${saved.media_prefetch_days} dias`);
  ensureOption($("s-cache"), saved.media_cache_max_mb, `${saved.media_cache_max_mb} MB`);
  allowed = saved.send_allowed_chats.map((jid) => ({ jid, name: jid }));
  renderChips();
  // Show names instead of JIDs where the chat list knows them.
  for (const c of allowed) {
    call("GET", `/api/chats?q=${encodeURIComponent(c.jid.split("@")[0])}`)
      .then((rows) => {
        const hit = rows.find((r) => r.jid === c.jid);
        if (hit) {
          c.name = hit.name;
          renderChips();
        }
      })
      .catch(() => {});
  }
  show($("settings-error"), false);
}

let searchTimer;
function searchChats() {
  clearTimeout(searchTimer);
  const q = $("chat-search").value.trim();
  if (!q) return $("chat-results").replaceChildren();
  searchTimer = setTimeout(async () => {
    const rows = await call("GET", `/api/chats?q=${encodeURIComponent(q)}`).catch(() => []);
    $("chat-results").replaceChildren(
      ...rows
        .filter((r) => !allowed.some((a) => a.jid === r.jid))
        .map((r) => {
          const b = document.createElement("button");
          b.type = "button";
          b.textContent = `${r.name} · ${r.jid}`;
          b.onclick = () => {
            allowed.push(r);
            renderChips();
            $("chat-search").value = "";
            $("chat-results").replaceChildren();
          };
          return b;
        })
    );
  }, 250);
}

async function saveSettings(event) {
  event.preventDefault();
  const values = {
    send_enabled: $("s-send").checked,
    send_allowed_chats: allowed.map((c) => c.jid),
    send_spacing: $("s-spacing").value,
    presence_mode: $("s-presence").checked ? "normal" : "quiet",
    media_prefetch_days: Number($("s-prefetch").value),
    media_cache_max_mb: Number($("s-cache").value),
  };
  try {
    await call("POST", "/api/settings", values);
  } catch (e) {
    $("settings-error").textContent = `Não foi possível salvar: ${e.message}`;
    show($("settings-error"), true);
    return;
  }
  waitForRestart();
}

// ---- wiring --------------------------------------------------------------------------
document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-copy]");
  if (!target) return;
  const el = $(target.dataset.copy);
  await navigator.clipboard.writeText(el.dataset.value ?? el.value ?? el.textContent);
  toast("Copiado");
});

$("token-show").onclick = () => {
  revealed = !revealed;
  renderConnection();
};
$("rotate").onclick = async () => {
  if (!confirm("Gerar um novo token? Todos os clientes que usam o atual (Claude Code, claude.ai…) param de funcionar até você atualizar o token neles.")) return;
  await call("POST", "/api/token/rotate");
  waitForRestart();
};
$("reconnect").onclick = async () => {
  await call("POST", "/api/whatsapp/reconnect");
  toast("Reconectando…");
  setTimeout(refresh, 1500);
};
$("unlink").onclick = async () => {
  if (!confirm("Desconectar este servidor do seu WhatsApp? Será preciso escanear o QR code de novo. As mensagens já salvas continuam aqui.")) return;
  await call("POST", "/api/whatsapp/logout");
  toast("Desconectando…");
  setTimeout(refresh, 3000);
};
$("pair-phone-btn").onclick = async () => {
  show($("pair-error"), false);
  try {
    await call("POST", "/api/whatsapp/pair-phone", { phone: $("pair-phone").value });
    toast("Gerando código…");
  } catch (e) {
    $("pair-error").textContent = e.message;
    show($("pair-error"), true);
  }
};
$("chat-search").addEventListener("input", searchChats);
$("chat-search").addEventListener("keyup", searchChats);
$("settings").onsubmit = saveSettings;
$("settings-reset").onclick = loadSettings;
window.addEventListener("hashchange", route);

route();
refresh();
setInterval(refresh, 3000);
