"""Fase 1 da refatoração — correções de segurança e bugs confirmados.

Cada teste falha no código ANTES da Fase 1 e passa depois, servindo de trava
de regressão para os vazamentos/brechas/bugs identificados na auditoria.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))


# ── Bugs confirmados de NameError (star-import escondia a falha) ────────────
def test_config_digits_resolvivel_no_modulo():
    from cotacao import config
    assert hasattr(config, "_digits"), "config._digits não importado (NameError em _dados_envio)"
    assert config._digits("12.345.678/0001-90") == "12345678000190"


def test_error_reporter_redige_chave_nfe_44_digitos():
    from error_reporter import sanitize_error_payload
    chave = "1" * 44
    out = sanitize_error_payload(f"falha lendo nota {chave} no portal")
    assert chave not in out
    assert "[NFE_KEY_REDACTED]" in out


def test_sanitize_quote_details_mascara_cnpj_cpf_formatado_e_cru():
    from fretio.providers.base import ProviderBase
    san = ProviderBase._sanitize_quote_details
    assert "12.345.678/0001-90" not in san("frete para 12.345.678/0001-90 ok")
    assert "123.456.789-09" not in san("pagador 123.456.789-09")
    assert "12345678000190" not in san("doc x12345678000190x")
    assert "98765432100" not in san("cpf 98765432100 fim")
    # DOM com tags viradas em espaço: "12.345.678 / 0001-90" também deve mascarar.
    out = san("destino <span>12.345.678</span> / <span>0001-90</span> ok")
    assert "12.345.678" not in out and "0001-90" not in out


# ── Updater: tag não assinada do GitHub interpolada no .bat ──────────────────
def test_safe_bat_version_whitelist():
    from updater import _safe_bat_version
    assert _safe_bat_version("2.49") == "2.49"
    assert _safe_bat_version("2.49.1-rc.2") == "2.49.1-rc.2"
    assert _safe_bat_version("") == "desconhecida"
    for perigoso in ("2.49 & del x", "1>2", "a|b", "50%%foo"):
        out = _safe_bat_version(perigoso)
        for meta in "&><|% ":
            assert meta not in out


# ── Ponte web: emit() não pode permitir quebra do contexto JS ────────────────
def test_emit_escapa_separadores_e_preserva_envelope():
    import web_app

    capturado = {}

    class FakeWindow:
        def evaluate_js(self, code):
            capturado["code"] = code

    sep28, sep29 = "\u2028", "\u2029"  # quebram literal JS em engines antigas
    payload = {"x": f"linha1{sep28}linha2{sep29}fim", "y": "</script>", "z": "a&b"}
    web_app.emit(FakeWindow(), "teste", payload)
    code = capturado["code"]

    assert "window.onBackendEvent(" in code
    # caracteres perigosos não podem aparecer crus no JS injetado
    assert sep28 not in code and sep29 not in code
    assert "<" not in code and ">" not in code and "&" not in code
    assert "\\u2028" in code and "\\u003c" in code
    # envelope e dados preservados (JSON volta a decodificar tudo).
    inner = code[len("window.onBackendEvent("):-1]
    obj = json.loads(inner)
    assert obj["event"] == "teste"
    assert obj["payload"]["x"] == f"linha1{sep28}linha2{sep29}fim"
    assert obj["payload"]["y"] == "</script>"
    assert obj["payload"]["z"] == "a&b"


def test_emit_window_none_nao_quebra():
    import web_app
    web_app.emit(None, "x", {})  # não deve lançar


# ── Senhas não trafegam mais em texto puro pela ponte ───────────────────────
def _config_toml(senha="segredo123", cnpj="12345678000190"):
    return (
        "[transportadoras.braspress]\n"
        "habilitado = true\n"
        f'cnpj = "{cnpj}"\n'
        f'senha = "{senha}"\n'
    )


def _braspress_campos(api):
    carriers = api.config_get()["transportadoras"]
    bras = next(c for c in carriers if c["nome"] == "braspress")
    return {c["key"]: c for c in bras["campos"]}


def test_config_get_nao_envia_senha_em_texto_puro(tmp_path):
    import web_app
    p = tmp_path / "CONFIG.toml"
    p.write_text(_config_toml(), encoding="utf-8")
    api = web_app.Api(empresa="TESTE", config_path=p)

    campos = _braspress_campos(api)
    assert campos["senha"]["tipo"] == "password"
    assert campos["senha"]["valor"] == ""              # senha NÃO vai para o DOM
    assert campos["senha"]["tem_valor"] is True         # mas a UI sabe que existe
    assert campos["cnpj"]["valor"] == "12345678000190"  # campo comum continua


def test_config_salvar_credenciais_senha_nao_grava_em_claro(tmp_path, monkeypatch):
    import web_app
    import secure_credentials
    p = tmp_path / "CONFIG.toml"
    p.write_text(_config_toml(senha="segredo123"), encoding="utf-8")
    api = web_app.Api(empresa="TESTE", config_path=p)

    creds: list[tuple] = []
    monkeypatch.setattr(
        secure_credentials, "set_credential",
        lambda emp, transp, campo, val: (creds.append((transp, campo, val)) or True),
    )

    # Senha em branco + CNPJ novo: a senha legada em claro é migrada para o
    # Credential Manager e REMOVIDA do TOML (nunca fica em texto claro).
    assert api.config_salvar_credenciais("braspress", {"cnpj": "99999999000199", "senha": ""})["ok"]
    raw = web_app._load_config(p)["transportadoras"]["braspress"]
    assert "senha" not in raw                              # plaintext removido do arquivo
    assert raw["cnpj"] == "99999999000199"
    assert ("braspress", "senha", "segredo123") in creds   # legada preservada no CredMgr

    # Senha preenchida: vai para o Credential Manager, nunca para o TOML.
    creds.clear()
    assert api.config_salvar_credenciais("braspress", {"senha": "novaSenha"})["ok"]
    raw = web_app._load_config(p)["transportadoras"]["braspress"]
    assert raw.get("senha") != "novaSenha"
    assert ("braspress", "senha", "novaSenha") in creds


# ── Bug de UI: status em andamento rotulado como "erro" (cot_status.js) ──────
def test_cot_status_js_corrigido_para_rs_erro():
    # Sem runner JS no projeto; valida a correção textual da precedência:
    # antes:  raw === "erro" || rs ? "erro" : ...
    # depois: (raw === "erro" || rs === "erro") ? "erro" : ...
    src = (ROOT / "app" / "web" / "pages" / "cot_status.js").read_text(encoding="utf-8")
    assert '(raw === "erro" || rs === "erro")' in src
    assert "|| rs ?" not in src
