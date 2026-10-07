"""Fretio — shell web (POC pywebview + WebView2).

Substitui a janela PySide6 por uma UI web (app/web/*) renderizada no WebView2,
mantendo TODO o backend Python intacto. Esta é a fatia vertical (Dashboard) que
valida a arquitetura antes de migrar as demais telas.

Pontes:
  JS  -> Python : métodos públicos de `Api` (pywebview.api.<metodo>)
  Python -> JS  : emit(window, evento, payload) -> window.onBackendEvent({...})

NÃO importa nem altera romaneio_app.py. Entrypoint paralelo, 100% reversível.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ── sys.path (dev) ─────────────────────────────────────────────────────────
_APP_DIR = Path(__file__).resolve().parent
_FRETIO_SRC = _APP_DIR / "fretio" / "src"
for _p in (_APP_DIR, _FRETIO_SRC):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import webview  # noqa: E402

import company_config as cc  # noqa: E402
from web_presenters import (  # noqa: E402
    chave_nota,
    montar_romaneio_fornecedor,
    nota_card,
    validar_local_entrega,
)

# Helpers/constantes compartilhados foram movidos para web_app_shared.py e são
# reimportados aqui para manter a superfície pública do módulo idêntica.
from web_app_shared import (  # noqa: E402
    _CARRIER_FIELDS,
    _ConfigUnsafeToWrite,
    _PROVIDER_SPECS,
    _carregar_versao,
    _load_config,
    _load_config_for_write,
    _resolver_tema_efetivo,
    _toml_reader,
)

# Mixins de domínio movidos para submódulos; reimportados para a herança do Api
# e para manter a superfície pública do módulo idêntica.
from web_app_config import ConfigMixin  # noqa: E402
from web_app_cotacao import CotacaoMixin  # noqa: E402
from web_app_rastreio import RastreioMixin  # noqa: E402
from web_app_romaneio import RomaneioMixin  # noqa: E402
from web_app_startup import StartupMixin  # noqa: E402


def _index_path() -> str:
    base = Path(getattr(sys, "_MEIPASS", "") or _APP_DIR)
    return str(base / "web" / "index.html")


def _startup_path() -> str:
    base = Path(getattr(sys, "_MEIPASS", "") or _APP_DIR)
    return str(base / "web" / "startup.html")


# ── Push Python -> JS (única fronteira para a GUI, espelha o postEvent) ─────
def emit(window: Any, evento: str, payload: dict | None = None) -> None:
    if window is None:
        return
    msg = json.dumps({"event": evento, "payload": payload or {}}, ensure_ascii=False)
    # Strings vêm de portais de transportadora; escapar os caracteres que podem
    # encerrar o literal JS (U+2028/U+2029) ou um <script> embutido antes de
    # injetar em evaluate_js. Equivalentes dentro de string JSON, então o
    # contrato {event,payload} permanece idêntico.
    msg = (
        msg.replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    try:
        window.evaluate_js(f"window.onBackendEvent({msg})")
    except Exception:
        pass


# Apresentação (nota_card / validar_local_entrega / montar_romaneio_fornecedor)
# foi movida para web_presenters.py e importada acima.


class Api(ConfigMixin, StartupMixin, RastreioMixin, CotacaoMixin, RomaneioMixin):
    def __init__(self, empresa: str | None = None, config_path: Path | None = None) -> None:
        self._empresa = empresa or ""
        self._config_path = config_path
        self._versao = _carregar_versao()
        self._window: Any = None
        self._update_info: Any = None
        # Controlador de backend (lazy — só criado quando cotação/rastreio são usados).
        self._sessao: Any = None
        self._loop: Any = None
        self._cotando = False
        self._rastreando = False
        self._runtime_config_dirty = False
        # Serializa o check-and-set de _cotando/_rastreando: chamadas do js_api
        # podem chegar concorrentes, então reservar a flag precisa ser atômico.
        # Os resets para False ficam fora do lock de propósito: são stores de um
        # único bool, atômicos sob o GIL.
        self._op_lock = threading.Lock()
        self._notas: list[Any] = []  # NotaFiscal carregadas (rastreio)
        self._romaneios: list[dict] = []  # romaneios processados na sessão (dashboard)
        self._last_cotacao: list[Any] = []  # últimos ResultadoCotacao (dashboard)
        self._romaneio_texto = ""  # último romaneio calculado (handoff p/ cotação)

    def attach_window(self, window: Any) -> None:
        self._window = window
        # Fechar a janela (X ou sair()) encerra worker + sessão Playwright.
        try:
            window.events.closed += self._teardown
        except Exception:
            pass

    def _teardown(self) -> None:
        # Encerra o worker assíncrono e a sessão Playwright (fecha o Chrome do
        # Rodonaves e libera o user-data-dir). Sem isso o processo do Chrome vaza
        # ao fechar a janela e o lock do perfil pode corromper a próxima abertura.
        # Idempotente: zera _loop/_sessao antes, então chamadas repetidas são no-op.
        loop, sessao = self._loop, self._sessao
        self._loop = None
        self._sessao = None
        if loop is not None:
            try:
                loop.shutdown(cleanup_coro_factory=(sessao.cleanup if sessao is not None else None))
            except Exception:
                pass

    def _refresh_runtime_config(self) -> None:
        if self._cotando or self._rastreando:
            self._runtime_config_dirty = True
            return
        self._runtime_config_dirty = False
        self._teardown()

    def _apply_pending_runtime_config(self) -> None:
        if self._runtime_config_dirty and not self._cotando and not self._rastreando:
            self._refresh_runtime_config()

    def _emit(self, evento: str, payload: dict | None = None) -> None:
        emit(self._window, evento, payload)


    def _gate(self, feature: str) -> dict | None:
        """Gating por licença/config remota (fail-open). Retorna erro se bloqueado."""
        try:
            from remote_permissions import feature_allowed_or_default, feature_message
            if not feature_allowed_or_default(feature):
                return {"erro": feature_message(feature)}
        except Exception:
            pass
        return None

    # --- leitura ---
    def get_bootstrap(self) -> dict[str, Any]:
        cfg = _load_config(self._config_path)
        fretio = cfg.get("fretio", {}) if isinstance(cfg.get("fretio"), dict) else {}
        tema = str(fretio.get("ui_tema", "sistema")).lower()

        transportadoras = []
        tcfg = cfg.get("transportadoras", {})
        if isinstance(tcfg, dict):
            for nome, sec in tcfg.items():
                if isinstance(sec, dict):
                    transportadoras.append({
                        "nome": nome,
                        "habilitado": bool(sec.get("habilitado", False)),
                        "status": "pending",
                    })

        # Dashboard é estado de sessão (em memória); no POC inicia vazio,
        # espelhando exatamente o app atual recém-aberto.
        dashboard = {
            "total_romaneios": 0,
            "total_volumes": 0,
            "melhor_frete": None,
            "sucesso_pct": None,
            "romaneios_recentes": [],
        }
        return {
            "empresa": self._empresa,
            "versao": self._versao,
            "tema": tema,
            "tema_efetivo": _resolver_tema_efetivo(tema),
            "raio": str(fretio.get("ui_raio", "Suave")),
            "botao": str(fretio.get("ui_botao", "Solido")),
            "transportadoras": transportadoras,
            "dashboard": dashboard,
        }

    def listar_empresas(self) -> list[str]:
        return cc._listar_empresas()

    # ── Cotação ────────────────────────────────────────────────────────────
    def _ensure_backend(self) -> None:
        if self._sessao is not None:
            return
        from cotacao_transportadoras import TransportadoraSession  # import pesado, lazy
        from async_worker import AsyncWorkerLoop

        self._sessao = TransportadoraSession(config_path=self._config_path)
        self._loop = AsyncWorkerLoop(name="WebAsyncLoop")

    # ── NF-e ────────────────────────────────────────────────────────────────
    def nfe_selecionar(self) -> dict:
        if self._window is None:
            return {"erro": "Janela indisponível"}
        bloqueio = self._gate("nfe")
        if bloqueio:
            return bloqueio
        paths = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Arquivos XML de NFe (*.xml)", "Todos os arquivos (*.*)"),
        )
        if not paths:
            return {"cancelado": True}
        from extrator_nfe import extrair_arquivo as _extrair

        erros: list[str] = []
        novas: list[Any] = []
        vistos = {getattr(n, "chave_acesso", "") for n in self._notas if getattr(n, "chave_acesso", "")}
        for arq in paths:
            nome = Path(arq).name
            try:
                notas = _extrair(arq)
                if not notas:
                    erros.append(f"{nome}: nenhuma NF-e encontrada")
                    continue
                for nf in notas:
                    if nf.chave_acesso and nf.chave_acesso in vistos:
                        continue
                    if nf.chave_acesso:
                        vistos.add(nf.chave_acesso)
                    novas.append(nf)
            except Exception as exc:
                erros.append(f"{nome}: {exc}")

        base = len(self._notas)
        self._notas.extend(novas)
        cards = [nota_card(base + i + 1, nf) for i, nf in enumerate(novas)]
        return {"cards": cards, "erros": erros, "total_notas": len(self._notas)}

    def nfe_cards(self) -> dict:
        """Reconstrói os cards das NF-e já carregadas (self._notas é a fonte da
        verdade). A tela Rastreio chama isto no render para reexibir as notas após
        navegar para outra página e voltar — sem isto os cards se perdiam e o
        re-import era barrado pelo filtro de duplicados de nfe_selecionar."""
        cards = [nota_card(i + 1, nf) for i, nf in enumerate(self._notas)]
        return {"cards": cards, "total_notas": len(self._notas)}

    def abrir_externo(self, alvo: str) -> dict:
        """Abre arquivo (screenshot) ou URL (rastreio) no app padrão do sistema.

        Só permite URLs http(s) e arquivos locais dentro da pasta de screenshots
        conhecida — o alvo chega do resultado do provider e não pode virar um
        caminho local arbitrário (ex.: um .exe) nem um esquema file://."""
        alvo = str(alvo or "").strip()
        if not alvo:
            return {"erro": "alvo vazio"}
        low = alvo.lower()
        if low.startswith(("http://", "https://")):
            try:
                import webbrowser
                webbrowser.open(alvo)
                return {"ok": True}
            except Exception as exc:
                return {"erro": str(exc)}
        if "://" in low:
            return {"erro": "destino não permitido"}
        try:
            from rastreamento import _download_dir
            base = Path(_download_dir()).resolve()
            target = Path(alvo).resolve()
            if os.path.commonpath([str(base), str(target)]) != str(base) or not target.exists():
                return {"erro": "destino não permitido"}
            if sys.platform == "win32":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                import webbrowser
                webbrowser.open(str(target))
            return {"ok": True}
        except Exception as exc:
            return {"erro": str(exc)}

    def abrir_screenshots(self) -> dict:
        try:
            from rastreamento import _download_dir
            d = str(_download_dir())
            if sys.platform == "win32":
                os.startfile(d)  # type: ignore[attr-defined]
            else:
                import webbrowser
                webbrowser.open(d)
            return {"ok": True}
        except Exception as exc:
            return {"erro": str(exc)}

    # ── Dashboard (estado de sessão) ────────────────────────────────────────
    def get_dashboard(self) -> dict:
        roms = self._romaneios
        total_rom = len(roms)
        total_vol = sum(int(r.get("volumes", 0) or 0) for r in roms)
        ok = [
            r for r in self._last_cotacao
            if getattr(r, "status", "") == "ok" and getattr(r, "valor_frete", None) is not None
        ]
        melhor = min((float(r.valor_frete) for r in ok), default=None) if ok else None
        total_cot = len(self._last_cotacao)
        ok_cot = len(ok)
        return {
            "total_romaneios": total_rom,
            "total_volumes": total_vol,
            "melhor_frete": melhor,
            "sucesso_pct": round(ok_cot / total_cot * 100) if total_cot else None,
            "sub_romaneios": "processado nesta sessão" if total_rom == 1 else "processados nesta sessão",
            "sub_volumes": "volume processado" if total_vol == 1 else "volumes processados",
            "sub_sucesso": f"{ok_cot} de {total_cot} transportadoras" if total_cot else "",
            "romaneios_recentes": list(reversed(roms))[:6],
        }

    def abrir_app(self) -> dict:
        # A navegação é feita no JS, DEPOIS de aguardar este retorno (ver
        # app/web/startup.js). Navegar aqui via load_url trocaria a página antes
        # do pywebview resolver o callback de retorno deste método, gerando
        # JavascriptException ('_returnValuesCallbacks.abrir_app.<id> is not a
        # function'). Devolvemos o destino relativo (mesma pasta web/) e o cliente
        # navega na sequência, já com o callback resolvido — sem corrida.
        return {"ok": True, "navegar": "index.html"}

    def trocar_empresa(self) -> dict:
        """Volta para o seletor de empresa. Navegação feita no JS — ver abrir_app."""
        return {"ok": True, "navegar": "startup.html?fase=empresa"}

    def sair(self) -> dict:
        try:
            self._teardown()
        except Exception:
            pass
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass
        return {"ok": True}


# ── Demo do canal de push (prova Python->JS a partir de background) ─────────
def _demo_pushes(api: Api) -> None:
    win = api._window
    time.sleep(0.9)
    emit(win, "status_update", {"texto": "Conectando às transportadoras…"})
    cfg = _load_config(api._config_path)
    tcfg = cfg.get("transportadoras", {}) if isinstance(cfg.get("transportadoras"), dict) else {}
    habilitadas = [n for n, s in tcfg.items() if isinstance(s, dict) and s.get("habilitado")]
    for nome in habilitadas:
        time.sleep(0.5)
        emit(win, "login_status", {"nome": nome, "status": "ok"})
    time.sleep(0.6)
    emit(win, "status_update", {"texto": "Nenhum arquivo carregado"})
    emit(win, "toast", {"texto": "Ponte Python→JS funcionando ✓"})


# ── Runner de dev ──────────────────────────────────────────────────────────
def _resolver_empresa() -> str:
    ultima = cc._ler_ultima_empresa()
    if ultima and cc._empresa_config_path(ultima).exists():
        return ultima
    existentes = cc._listar_empresas()
    if existentes:
        return existentes[0]
    # cria 'dev' mínima
    nome = "dev"
    path = cc._empresa_config_path(nome)
    path.parent.mkdir(parents=True, exist_ok=True)
    cc._escrever_config_toml({
        "fretio": {"ui_tema": "escuro"},
        "romaneio": {"cep_origem": "01001000"},
        "transportadoras": {
            "braspress": {"habilitado": True},
            "rodonaves": {"habilitado": True},
        },
    }, path)
    return nome


def _run_dev(args) -> None:
    """Modo dev: carrega o app direto numa empresa, sem partida/licença."""
    empresa = args.empresa or _resolver_empresa()
    api = Api(empresa, cc._empresa_config_path(empresa))
    window = webview.create_window(
        title=f"Fretio {api._versao} — {empresa}",
        url=_index_path(), js_api=api,
        width=1040, height=680, min_size=(900, 600), background_color="#1a1917",
    )
    api.attach_window(window)

    if args.smoke:
        def _smoke() -> None:
            for _ in range(50):
                time.sleep(0.1)
                try:
                    if window.evaluate_js("document.querySelector('#empresaName').textContent") != "—":
                        break
                except Exception:
                    pass
            try:
                result = {
                    "ok": True,
                    "empresa_render": window.evaluate_js("document.querySelector('#empresaName').textContent"),
                    "versao_render": window.evaluate_js("document.querySelector('#footerVersion').textContent"),
                    "kpis": window.evaluate_js("document.querySelectorAll('#kpiGrid .kpi').length"),
                    "carriers": window.evaluate_js("document.querySelectorAll('.carrier').length"),
                }
            except Exception as exc:
                result = {"ok": False, "erro": str(exc)}
            Path(args.smoke).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            window.destroy()
        threading.Thread(target=_smoke, daemon=True).start()
    elif args.demo:
        window.events.loaded += lambda: threading.Thread(target=_demo_pushes, args=(api,), daemon=True).start()

    webview.start()


def _webview2_runtime_present() -> bool:
    """True se o runtime do Edge WebView2 está instalado (mesma checagem do
    instalador Inno: EdgeUpdate Clients\\{F3017226-...} 'pv' em HKLM/HKCU).
    Fail-open: qualquer erro assume PRESENTE, para nunca bloquear o boot por engano."""
    if os.name != "nt":
        return True
    try:
        import winreg
        client = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        candidatos = (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, client),
            (winreg.HKEY_CURRENT_USER, client),
        )
        for hive, sub in candidatos:
            try:
                with winreg.OpenKey(hive, sub) as key:
                    pv, _ = winreg.QueryValueEx(key, "pv")
                    if pv:
                        return True
            except OSError:
                continue
        return False
    except Exception:
        return True  # fail-open: nunca bloquear o boot por erro na checagem


def _aviso_webview2_ausente() -> None:
    msg = (
        "O Fretio precisa do componente Microsoft Edge WebView2 Runtime, que não "
        "está instalado neste computador.\n\nInstale o WebView2 Runtime (Evergreen) "
        "e abra o Fretio novamente:\nhttps://go.microsoft.com/fwlink/p/?LinkId=2124703"
    )
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "Fretio — WebView2 necessário", 0x10)
    except Exception:
        print(msg)


def _run_producao() -> None:
    """Produção: partida de processo + tela de partida (licença/versão/update/empresa)."""
    from app_bootstrap import run_process_startup

    cont, _logger = run_process_startup()
    if not cont:
        return

    # Cliente que atualizou in-app (Qt->WebView2) sem o runtime WebView2: abrir a
    # janela daria tela em branco. Avisa com instruções e não inicia, em vez de
    # falhar mudo. Só em build empacotado (frozen); em dev nunca bloqueia.
    if getattr(sys, "frozen", False) and not _webview2_runtime_present():
        _aviso_webview2_ausente()
        return

    api = Api()
    window = webview.create_window(
        title="Fretio", url=_startup_path(), js_api=api,
        width=960, height=660, min_size=(840, 580), background_color="#1a1917",
    )
    api.attach_window(window)
    webview.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fretio (UI web, pywebview/WebView2)")
    parser.add_argument("--empresa", default=None, help="[dev] abre direto nesta empresa, sem licença")
    parser.add_argument("--demo", action="store_true", help="[dev] dispara pushes de demonstração")
    parser.add_argument("--smoke", metavar="OUT", help="[dev] verificação headless e sai")
    args = parser.parse_args()

    if args.empresa or args.demo or args.smoke:
        _run_dev(args)
    else:
        _run_producao()


if __name__ == "__main__":
    main()
