/* Página Cotação — cola/revisa romaneio, dispara cotação e mostra progresso
   ao vivo por transportadora (porta _rotulos_status_cotacao / a tabela de status). */
"use strict";

(function () {
  let running = false;
  const RESUMO_ANDAMENTO = "Cotação em andamento. As respostas aparecem conforme cada transportadora finaliza.";
  const RUNSTATUS_ANDAMENTO = "Cotação em andamento. Aguarde as respostas das transportadoras.";

  // Estado da cotação por sessão (sobrevive à navegação; re-hidratado no render).
  // A tabela de status mora no store do CotStatus (chave "cotStatusBody").
  function st(app) {
    return app.state.cotacao || (app.state.cotacao = { running: false, started: false, resumo: "", runStatus: "" });
  }

  function autoResize(el) {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }

  async function iniciar(app) {
    const ta = document.getElementById("cotInput");
    const texto = (ta.value || "").trim();
    if (!texto) { app.toast("Cole um romaneio antes de cotar"); return; }
    // Antes da confirmação do backend, só o feedback de carregamento no botão —
    // não zera resultado/tabela ainda, senão uma cotação REJEITADA (gate/licença,
    // op presa) apagaria o resultado anterior já concluído nesta sessão.
    const btn = document.getElementById("cotStart");
    btn.disabled = true; btn.classList.add("loading");
    let res;
    try {
      res = await (await app.api()).cotacao_iniciar(texto);
    } catch (e) {
      finalizar(app);
      app.toast("Falha ao iniciar a cotação");
      return;
    }
    if (res && res.erro) { app.toast(res.erro); btn.disabled = false; btn.classList.remove("loading"); return; }
    // Backend aceitou: agora sim marca em andamento, vira dono e zera o anterior.
    running = true;
    const s = st(app);
    s.running = true; s.started = true; s.resumo = RESUMO_ANDAMENTO; s.runStatus = RUNSTATUS_ANDAMENTO;
    app.beginOp("cotacao");
    window.CotStatus.reset("cotStatusBody");
    document.getElementById("cotStatusWrap").hidden = false;
    document.getElementById("cotResult").textContent = RESUMO_ANDAMENTO;
    document.getElementById("cotRunStatus").textContent = RUNSTATUS_ANDAMENTO;
  }

  function finalizar(app) {
    running = false;
    if (app) st(app).running = false;
    const btn = document.getElementById("cotStart");
    if (btn) { btn.disabled = false; btn.classList.remove("loading"); }
  }

  window.Pages = window.Pages || {};
  window.Pages.cotacao = {
    title: "Cotação",
    render(view, app) {
      const s = st(app);
      running = !!s.running;
      view.innerHTML = `
        <div class="cot-cols">
          <section class="card cot-card">
            <div class="card-pad">
              <h2 class="card-title">1. Cole o romaneio</h2>
              <p class="card-hint">Cole aqui o romaneio que deseja cotar. Não é preciso importar um PDF antes — se você processou um na tela Romaneio, o texto já vem preenchido.</p>
              <textarea id="cotInput" class="input-area mono" spellcheck="false" placeholder="Exemplo:&#10;CNPJ/CPF: ...&#10;- VOL: ...&#10;- CUBAGEM: ... m3&#10;- PESO: ... kg&#10;- TOTAL: R$ ..."></textarea>
              <div class="cot-hintbox" id="cotHint">Cole um romaneio no campo acima para liberar a cotação.</div>
              <button class="btn btn-primary" id="cotStart" type="button" disabled>
                <span class="spin" aria-hidden="true"></span>Iniciar cotação
              </button>
              <div class="cot-runstatus" id="cotRunStatus">Cole um romaneio e clique em Iniciar cotação.</div>
            </div>
          </section>

          <section class="card cot-card">
            <div class="card-pad">
              <div class="card-title-row">
                <h2 class="card-title">Resultado da cotação</h2>
                <button class="btn btn-soft btn-sm" id="cotCopy" type="button">Copiar</button>
              </div>
              <div class="cot-status-wrap" id="cotStatusWrap" hidden>
                <table class="cot-table">
                  <thead><tr><th>Transp.</th><th>Situação</th><th>Etapa</th><th>Mensagem</th><th>Tempo</th></tr></thead>
                  <tbody id="cotStatusBody"></tbody>
                </table>
              </div>
              <pre class="result-text" id="cotResult">O resultado calculado pelas transportadoras aparecerá aqui.</pre>
            </div>
          </section>
        </div>`;

      const ta = $("#cotInput", view);
      const updateHint = () => {
        const txt = (ta.value || "").trim();
        const pronto = !!txt;
        $("#cotStart", view).disabled = pronto ? running : true;
        const linhas = txt ? txt.split("\n").filter((l) => l.trim()).length : 0;
        $("#cotHint", view).textContent = pronto
          ? `Romaneio preenchido com ${linhas} linha(s). Confira os dados e clique em Iniciar cotação.`
          : "Cole um romaneio no campo acima para liberar a cotação.";
        if (!running) {
          $("#cotRunStatus", view).textContent = pronto ? "Pronto para iniciar." : "Cole um romaneio para iniciar.";
        }
      };
      ta.addEventListener("input", () => { updateHint(); autoResize(ta); });
      $("#cotStart", view).addEventListener("click", () => iniciar(app));
      $("#cotCopy", view).addEventListener("click", async () => {
        const txt = $("#cotResult", view).textContent || "";
        try { await navigator.clipboard.writeText(txt); app.toast("Resultado copiado"); }
        catch { app.toast("Não foi possível copiar"); }
      });

      // Pré-carrega romaneio vindo do PDF/processamento, se houver
      if (app.state.romaneioTexto) { ta.value = app.state.romaneioTexto; }
      updateHint();
      autoResize(ta);

      // Re-hidrata o resultado/status da cotação em curso ou concluída nesta
      // sessão — sobrevive à navegação para outra tela e volta (Codex P2).
      if (s.started) {
        $("#cotStatusWrap", view).hidden = false;
        window.CotStatus.replay("cotStatusBody");
        if (s.resumo) $("#cotResult", view).textContent = s.resumo;
        if (s.runStatus) $("#cotRunStatus", view).textContent = s.runStatus;
        if (s.running) {
          const btn = $("#cotStart", view);
          btn.disabled = true; btn.classList.add("loading");
        }
      }
    },

    onEvent(evt, app) {
      const s = st(app);
      switch (evt.event) {
        case "cotacao_progress":
          window.CotStatus.upsert("cotStatusBody", evt.payload || {});
          break;
        case "cotacao_result": {
          s.resumo = (evt.payload && evt.payload.resumo) || "";
          const el = document.getElementById("cotResult");
          if (el) el.textContent = s.resumo;
          break;
        }
        case "cotacao_finished": {
          finalizar(app);
          s.runStatus = "Cotação finalizada.";
          const rs = document.getElementById("cotRunStatus");
          if (rs) rs.textContent = s.runStatus;
          break;
        }
        case "status_update":
          if (s.running && evt.payload && evt.payload.texto) {
            s.runStatus = evt.payload.texto;
            const rs = document.getElementById("cotRunStatus");
            if (rs) rs.textContent = s.runStatus;
          }
          break;
        case "chrome_missing":
          app.toast((evt.payload && evt.payload.texto) || "Google Chrome não encontrado");
          finalizar(app);
          break;
      }
    },
  };
})();
