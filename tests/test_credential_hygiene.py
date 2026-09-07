"""Regressões de segurança de credenciais:
- CWE-312: config_salvar_credenciais roteia senhas para o Credential Manager e
  nunca as grava em texto claro no CONFIG.toml.
- CWE-532: nome da empresa é mascarado nos logs de aviso.
"""
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

if "webview" not in sys.modules:
    sys.modules["webview"] = types.SimpleNamespace(
        OPEN_DIALOG=10, create_window=lambda *a, **k: None, start=lambda *a, **k: None,
    )

import secure_credentials  # noqa: E402
import web_app  # noqa: E402


def _find_password_field():
    for nome, fields in web_app._CARRIER_FIELDS.items():
        pwd = next((k for k, _, tp in fields if tp == "password"), None)
        other = next((k for k, _, tp in fields if tp != "password"), None)
        if pwd:
            return nome, pwd, other
    raise AssertionError("nenhuma transportadora com campo de senha em _CARRIER_FIELDS")


def test_config_save_routes_password_to_credmgr_not_toml(monkeypatch):
    carrier, pwd, other = _find_password_field()
    gravado = {}
    monkeypatch.setattr(
        web_app.ConfigMixin, "_write_config",
        lambda self, mutate: (mutate(gravado) or True),
    )
    calls = []
    monkeypatch.setattr(
        secure_credentials, "set_credential",
        lambda emp, transp, campo, val: (calls.append((emp, transp, campo, val)) or True),
    )

    api = web_app.Api(empresa="acme_ltda", config_path=None)
    campos = {pwd: "s3cr3t"}
    if other:
        campos[other] = "valor_publico"
    api.config_salvar_credenciais(carrier, campos)

    # Senha foi para o Credential Manager...
    assert ("acme_ltda", carrier, pwd, "s3cr3t") in calls
    # ...e NÃO foi escrita no CONFIG.toml.
    secao = gravado.get("transportadoras", {}).get(carrier, {})
    assert pwd not in secao
    if other:
        assert secao.get(other) == "valor_publico"  # campos não-senha continuam no TOML


def test_blank_password_not_written_anywhere(monkeypatch):
    carrier, pwd, _ = _find_password_field()
    gravado = {}
    monkeypatch.setattr(
        web_app.ConfigMixin, "_write_config",
        lambda self, mutate: (mutate(gravado) or True),
    )
    calls = []
    monkeypatch.setattr(
        secure_credentials, "set_credential",
        lambda *a: (calls.append(a) or True),
    )
    api = web_app.Api(empresa="acme", config_path=None)
    api.config_salvar_credenciais(carrier, {pwd: ""})  # branco = manter a já salva

    assert calls == []  # não chama set_credential com senha vazia
    assert pwd not in gravado.get("transportadoras", {}).get(carrier, {})


def test_redact_target_masks_empresa():
    redacted = secure_credentials._redact_target("Fretio:cliente_secreto:braspress:senha")
    assert redacted == "Fretio:***:braspress:senha"


def test_set_credential_warning_redacts_empresa(monkeypatch, caplog):
    # Força o caminho de fallback em memória (independente de SO) para disparar o warning.
    monkeypatch.setattr(secure_credentials, "_write_windows_credential", lambda t, v: False)
    with caplog.at_level(logging.WARNING, logger="secure_credentials"):
        secure_credentials.set_credential("cliente_secreto", "braspress", "senha", "x")
    assert "***" in caplog.text
    assert "cliente_secreto" not in caplog.text
