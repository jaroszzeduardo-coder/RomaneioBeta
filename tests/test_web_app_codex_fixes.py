"""Testes dos fixes do review do Codex na PR #85.

Cobrem regressões que a migração PySide6→web introduziu (não o refactor desta
sessão), corrigidas em web_app.py: UF como string, gate de romaneio, troca de
empresa vazando estado, migração de config não chamada, sanitização de nome de
empresa e a checagem fail-open do runtime WebView2.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

# pywebview não é instalado no CI (ubuntu); stuba a superfície usada por web_app.
if "webview" not in sys.modules:
    sys.modules["webview"] = types.SimpleNamespace(
        OPEN_DIALOG=10,
        create_window=lambda *a, **k: None,
        start=lambda *a, **k: None,
    )

import web_app


def _api():
    return web_app.Api(empresa="teste", config_path=None)


def _nao_cria(_nome):
    raise AssertionError("não deveria criar a empresa")


# --- Fix: ufs_atendidas como string separada por vírgula -----------------------

def test_norm_ufs_splits_comma_string_and_normalizes():
    n = web_app.ConfigMixin._norm_ufs
    assert n("SP,RS") == ["SP", "RS"]
    assert n("sp, rs ,") == ["SP", "RS"]   # trim + descarta token vazio
    assert n(["sp", "rs"]) == ["SP", "RS"]
    assert n([]) == []
    assert n(None) == []


def test_config_get_splits_comma_string_uf(tmp_path):
    cfg = tmp_path / "CONFIG.toml"
    cfg.write_text(
        '[transportadoras.braspress]\nhabilitado = true\nufs_atendidas = "SP,RS"\n',
        encoding="utf-8",
    )
    out = web_app.Api(empresa="x", config_path=cfg).config_get()
    bp = next(c for c in out["transportadoras"] if c["nome"] == "braspress")
    assert bp["ufs_atendidas"] == ["SP", "RS"]


def test_config_salvar_transportadora_normalizes_uf_string(monkeypatch):
    gravado = {}
    monkeypatch.setattr(web_app.ConfigMixin, "_write_config",
                        lambda self, mutate: (mutate(gravado) or True))
    api = _api()
    api.config_salvar_transportadora("braspress", {"ufs_atendidas": "sp,rs"})
    assert gravado["transportadoras"]["braspress"]["ufs_atendidas"] == ["SP", "RS"]


# --- Fix: gate de romaneio para colado, não para FOB ---------------------------

def test_romaneio_gate_blocks_pasted_quote():
    api = _api()
    api._gate = lambda feature: {"erro": "romaneio off"} if feature == "romaneio" else None
    r = api.cotacao_iniciar("um romaneio colado")  # sem cnpj_remetente = colado
    assert r == {"erro": "romaneio off"}


def test_romaneio_gate_does_not_block_fob():
    api = _api()
    api._gate = lambda feature: {"erro": "romaneio off"} if feature == "romaneio" else None

    def boom():
        raise RuntimeError("sem backend")

    api._ensure_backend = boom  # FOB deve passar dos gates e parar no backend
    r = api.cotacao_iniciar("texto", cnpj_remetente="12345678000190")
    assert "Falha ao preparar" in (r.get("erro") or "")  # passou do gate romaneio


# --- Fix: troca de empresa encerra sessão e zera estado ------------------------

def test_startup_entrar_tears_down_and_clears_state(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "_load_config", lambda p: {})
    monkeypatch.setattr(web_app.cc, "_empresa_config_path", lambda n: tmp_path / "CONFIG.toml")
    monkeypatch.setattr(web_app.cc, "_salvar_ultima_empresa", lambda n: None)
    monkeypatch.setattr(web_app.cc, "_garantir_defaults_fretio", lambda c: False)
    monkeypatch.setattr(web_app.cc, "_garantir_defaults_empresa", lambda c: False)
    api = _api()
    api._sessao = object()  # sessão "viva" da empresa anterior
    api._loop = None        # None p/ _teardown não tentar shutdown
    api._notas = [1, 2]
    api._romaneios = [{"x": 1}]
    api._last_cotacao = [object()]
    api._romaneio_texto = "antigo"
    r = api.startup_entrar("OutraEmpresa")
    assert r.get("ok") is True
    assert api._sessao is None  # backend encerrado
    assert api._notas == []
    assert api._romaneios == []
    assert api._last_cotacao == []
    assert api._romaneio_texto == ""


# --- Fix: migração de config legada antes de listar empresas -------------------

def test_startup_empresas_runs_migration_before_listing(monkeypatch):
    chamadas = []
    monkeypatch.setattr(web_app.cc, "_migrar_config_se_necessario", lambda: chamadas.append("migrou"))
    monkeypatch.setattr(web_app.cc, "_listar_empresas", lambda: (chamadas.append("listou"), ["darlu"])[1])
    out = _api().startup_empresas()
    assert out == ["darlu"]
    assert chamadas == ["migrou", "listou"]  # migração ANTES de listar


# --- Fix: sanitização de nome de empresa ---------------------------------------

def test_startup_criar_empresa_sanitizes_separators(monkeypatch):
    nomes = []
    monkeypatch.setattr(web_app.cc, "_listar_empresas", lambda: [])
    monkeypatch.setattr(web_app.cc, "_criar_config_empresa_vazia", lambda n: nomes.append(n))
    r = _api().startup_criar_empresa("foo/bar")
    assert r.get("ok") is True
    assert nomes == ["foo_bar"]  # separador vira "_"


def test_startup_criar_empresa_rejects_dotdot(monkeypatch):
    monkeypatch.setattr(web_app.cc, "_listar_empresas", lambda: [])
    monkeypatch.setattr(web_app.cc, "_criar_config_empresa_vazia", _nao_cria)
    r = _api().startup_criar_empresa("..")
    assert r.get("ok") is False


def test_startup_criar_empresa_case_insensitive_duplicate(monkeypatch):
    monkeypatch.setattr(web_app.cc, "_listar_empresas", lambda: ["darlu"])
    monkeypatch.setattr(web_app.cc, "_criar_config_empresa_vazia", _nao_cria)
    r = _api().startup_criar_empresa("DARLU")
    assert r.get("ok") is False
    assert "Já existe" in (r.get("erro") or "")


# --- Fix: checagem fail-open do runtime WebView2 -------------------------------

def test_webview2_runtime_present_returns_bool_without_raising():
    assert isinstance(web_app._webview2_runtime_present(), bool)


# --- Fix [P1]: encerrar o processo após disparar o updater ---------------------

class _ExitSentinel(Exception):
    pass


def test_startup_aplicar_update_force_exits_whole_process(monkeypatch):
    import updater
    chamadas = []
    monkeypatch.setattr(updater, "apply_update", lambda info, callback=None: True)
    monkeypatch.setattr(updater, "restart_app", lambda: chamadas.append("restart"))
    monkeypatch.setattr(web_app.os, "_exit", lambda code: (_ for _ in ()).throw(_ExitSentinel(code)))
    api = _api()
    api._update_info = object()
    api._teardown = lambda: chamadas.append("teardown")
    import pytest
    with pytest.raises(_ExitSentinel):
        api.startup_aplicar_update()
    assert "restart" in chamadas    # disparou o _apply_update.bat
    assert "teardown" in chamadas   # fechou Chrome/Playwright antes de sair
    # os._exit foi chamado (sentinela) -> o processo inteiro encerraria


def test_startup_aplicar_update_no_update_returns_error_without_exit():
    api = _api()
    api._update_info = None
    r = api.startup_aplicar_update()
    assert r.get("ok") is False


def test_startup_aplicar_update_reports_restart_failure_without_closing(monkeypatch):
    import updater

    monkeypatch.setattr(updater, "apply_update", lambda info, callback=None: True)
    monkeypatch.setattr(
        updater,
        "restart_app",
        lambda: (_ for _ in ()).throw(PermissionError("autorizacao negada")),
    )
    monkeypatch.setattr(
        web_app.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(AssertionError(f"nao deveria sair: {code}")),
    )
    api = _api()
    api._update_info = object()

    result = api.startup_aplicar_update()

    assert result["ok"] is False
    assert "autorizacao negada" in result["erro"]


# --- Fix [P2]: cards de NF-e sobrevivem à navegação (reload do backend) --------

def test_nfe_cards_rebuilds_from_notas(monkeypatch):
    # nfe_cards() reconstrói os cards a partir de _notas (fonte da verdade), com
    # índice 1-based na ordem de _notas — o frontend recarrega isto no render.
    monkeypatch.setattr(web_app, "nota_card", lambda i, nf: {"indice": i, "nf": nf})
    api = _api()
    api._notas = ["A", "B", "C"]
    out = api.nfe_cards()
    assert out["total_notas"] == 3
    assert [c["indice"] for c in out["cards"]] == [1, 2, 3]
    assert [c["nf"] for c in out["cards"]] == ["A", "B", "C"]


def test_nfe_cards_empty_when_no_notas():
    api = _api()
    api._notas = []
    assert api.nfe_cards() == {"cards": [], "total_notas": 0}
