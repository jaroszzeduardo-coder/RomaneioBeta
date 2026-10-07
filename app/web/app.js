/* Fretio web UI — núcleo: ponte pywebview, roteador, tema e dispatch de eventos.
   Páginas registram window.Pages[nome] = { title, render(view, app), onShow?, onEvent? }.
   Em teste headless (Playwright), window.__STUB_API__ substitui a ponte. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

function apiBridge() {
  if (window.__STUB_API__) return Promise.resolve(window.__STUB_API__);
  if (window.pywebview && window.pywebview.api) return Promise.resolve(window.pywebview.api);
  return new Promise((resolve) => {
    window.addEventListener("pywebviewready", () => resolve(window.pywebview.api), { once: true });
  });
}

const TITULOS = {
  romaneio: "Romaneio", cotacao: "Cotação",
  fornecedores: "Fornecedores", rastreio: "Rastreio", config: "Configurações",
};

window.App = {
  state: { empresa: "", versao: "", tema: "escuro", transportadoras: [], dashboard: {} },
  page: "romaneio",
  // Dono (página que iniciou) de cada operação longa, por família de evento.
  // Eventos de operação são roteados ao dono mesmo quando ele não está visível,
  // para o resultado/progresso sobreviver à navegação. null = nenhuma em curso.
  opOwners: { cotacao: null, rastreio: null },
  view: () => $("#view"),
  api: apiBridge,
  toast,
  fmt: () => window.Fmt,

  // Marca a página atual como dona da operação. Não rouba o dono de uma operação
  // já em andamento (o backend rejeita a 2ª) — preserva o roteamento da original.
  beginOp(family) {
    if (family in this.opOwners && !this.opOwners[family]) this.opOwners[family] = this.page;
  },

  navigate(name) {
    if (!TITULOS[name]) return;
    this.page = name;
    document.querySelectorAll(".nav-item[data-page]").forEach((b) =>
      b.classList.toggle("is-active", b.dataset.page === name));
    $("#pageTitle").textContent = TITULOS[name];
    $("#pageSub").textContent = window.App.state.subTitulo || "Nenhum arquivo carregado";
    $("#backBtn").hidden = name === "romaneio";

    const view = this.view();
    view.innerHTML = "";
    const mod = window.Pages[name];
    if (!mod) {
      view.innerHTML = `<div class="placeholder"><div class="ph-title">Tela "${TITULOS[name]}" em migração</div>
        <div class="ph-text">Esta página ainda está sendo portada para a nova interface.</div></div>`;
      return;
    }
    try {
      mod.render(view, window.App);
      if (mod.onShow) mod.onShow(window.App);
    } catch (e) {
      view.innerHTML = `<div class="placeholder"><div class="ph-title">Erro ao abrir a tela</div>
        <div class="ph-text">${window.Fmt.esc(e.message || e)}</div></div>`;
    }
  },
};

/* ── Tema / aparência ──────────────────────────────────────────────────── */
function applyTema(temaEfetivo) {
  const dark = temaEfetivo !== "claro";
  document.documentElement.setAttribute("data-theme", dark ? "escuro" : "claro");
  const toggle = $("#themeToggle");
  toggle.setAttribute("aria-checked", String(dark));
  $("#themeLabel").textContent = dark ? "Modo escuro" : "Modo claro";
}
function applyAparencia(raio, botao) {
  if (raio) document.documentElement.setAttribute("data-raio", String(raio).toLowerCase());
  if (botao) document.documentElement.setAttribute("data-botao", String(botao).toLowerCase());
}
window.App.applyTema = applyTema;
window.App.applyAparencia = applyAparencia;
async function toggleTema() {
  const dark = $("#themeToggle").getAttribute("aria-checked") === "true";
  const novo = dark ? "claro" : "escuro";
  applyTema(novo);
  const res = await (await apiBridge()).set_tema(novo);
  if (res && res.tema_efetivo) applyTema(res.tema_efetivo);
}

/* ── Toast ─────────────────────────────────────────────────────────────── */
let _toastTimer = null;
function toast(msg) {
  if (!msg) return;
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 2800);
}

/* ── Canal Python -> JS ────────────────────────────────────────────────── */
// Eventos de operação longa: mapeados à família cujo dono os recebe mesmo fora
// de vista. Sem isto, ao navegar antes do término, o resultado é entregue à
// página ativa (que o ignora) e some ao voltar à tela de origem.
const OP_FAMILY = {
  cotacao_progress: "cotacao", cotacao_result: "cotacao", cotacao_finished: "cotacao",
  rastreio_progress: "rastreio", rastreio_finished: "rastreio",
  // chrome_missing da cotação é assíncrono (coroutine) — roteia ao dono p/ o aviso
  // não sumir se o usuário já navegou. O do rastreio é síncrono no rastreio_iniciar
  // (usuário ainda na tela); sem dono de cotação, cai na página ativa, como antes.
  chrome_missing: "cotacao",
};

window.onBackendEvent = function (evt) {
  if (!evt || !evt.event) return;
  // Handlers globais
  if (evt.event === "status_update") {
    const txt = (evt.payload && evt.payload.texto) || "";
    window.App.state.subTitulo = txt;
    $("#pageSub").textContent = txt;
  } else if (evt.event === "toast") {
    toast((evt.payload && evt.payload.texto) || "");
  }
  // Roteia eventos de operação ao dono (mesmo fora de vista); os demais vão para
  // a página ativa, como antes.
  const fam = OP_FAMILY[evt.event];
  const alvo = (fam && window.App.opOwners[fam]) || window.App.page;
  const mod = window.Pages[alvo];
  if (mod && mod.onEvent) {
    try { mod.onEvent(evt, window.App); } catch (e) { /* isola erro de página */ }
  }
  // Operação terminou: libera o dono (próximo início recomeça limpo).
  if (evt.event === "cotacao_finished") window.App.opOwners.cotacao = null;
  else if (evt.event === "rastreio_finished") window.App.opOwners.rastreio = null;
};

/* ── Boot ──────────────────────────────────────────────────────────────── */
function bindShell() {
  document.querySelectorAll(".nav-item[data-page]").forEach((btn) =>
    btn.addEventListener("click", () => window.App.navigate(btn.dataset.page)));
  $("#backBtn").addEventListener("click", () => window.App.navigate("romaneio"));
  $("#empresaChip").addEventListener("click", async () => {
    const a = await apiBridge();
    if (!a.trocar_empresa) return;
    // Navega no JS após o await (callback de retorno já resolvido) — ver abrir_app
    // em startup.js: navegar no Python correria com a resolução do callback.
    const r = await a.trocar_empresa();
    if (r && r.ok) window.location.assign(r.navegar || "startup.html?fase=empresa");
  });
  $("#themeToggle").addEventListener("click", toggleTema);
  $("#cmdkBtn").addEventListener("click", () => toast("Command palette (Ctrl+K) — em breve"));
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault(); toast("Command palette (Ctrl+K) — em breve");
    }
  });
}

async function boot() {
  bindShell();
  const a = await apiBridge();
  const b = await a.get_bootstrap();
  Object.assign(window.App.state, b);
  window.App.state.subTitulo = "Nenhum arquivo carregado";

  $("#empresaName").textContent = b.empresa || "—";
  $("#empresaAvatar").textContent = (b.empresa || "?").trim().charAt(0).toUpperCase();
  $("#footerVersion").textContent = "Fretio " + (b.versao || "");
  applyTema(b.tema_efetivo || "escuro");
  applyAparencia(b.raio, b.botao);

  window.App.navigate("romaneio");
}

document.addEventListener("DOMContentLoaded", boot);
