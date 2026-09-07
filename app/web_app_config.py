"""Fretio — ConfigMixin (leitura/escrita de configuração e aparência).

Extraído de web_app.py; opera sobre o estado do Api via self. Importa helpers
compartilhados de web_app_shared (nunca de web_app) para evitar ciclo.
"""
from __future__ import annotations

from typing import Any

import company_config as cc
from web_app_shared import (
    _CARRIER_FIELDS,
    _ConfigUnsafeToWrite,
    _load_config,
    _load_config_for_write,
    _resolver_tema_efetivo,
)


# ── Ponte JS -> Python ─────────────────────────────────────────────────────
class ConfigMixin:
    """Leitura/escrita de configuração e aparência.

    Delegate de domínio do god-object Api. Os métodos operam sobre o estado do
    Api (self._config_path / self._empresa) — a superfície pública pywebview
    permanece idêntica (ver test_char_web_app_api_surface.py)."""

    def set_tema(self, modo: str) -> dict[str, Any]:
        modo = (modo or "sistema").lower()
        if modo not in ("claro", "escuro", "sistema"):
            modo = "sistema"
        try:
            cfg = _load_config_for_write(self._config_path)
        except _ConfigUnsafeToWrite:
            return {"ok": False, "tema": modo, "tema_efetivo": _resolver_tema_efetivo(modo)}
        if not isinstance(cfg.get("fretio"), dict):
            cfg["fretio"] = {}
        cfg["fretio"]["ui_tema"] = modo
        try:
            cc._escrever_config_toml(cfg, self._config_path)
            ok = True
        except Exception:
            ok = False
        return {"ok": ok, "tema": modo, "tema_efetivo": _resolver_tema_efetivo(modo)}

    @staticmethod
    def _norm_ufs(value) -> list:
        """Normaliza ufs_atendidas: aceita lista OU string separada por vírgula
        (configs antigas guardam "SP,RS"). Retorna UFs em maiúsculas, sem vazios."""
        items = value.split(",") if isinstance(value, str) else (value or [])
        return [str(x).strip().upper() for x in items if str(x).strip()]

    def config_get(self) -> dict:
        cfg = _load_config(self._config_path)
        fb = cfg.get("fretio", {}) or {}
        rom = cfg.get("romaneio", {}) or {}
        transp = cfg.get("transportadoras", {}) or {}

        carriers = []
        for nome, fields in _CARRIER_FIELDS.items():
            sec = transp.get(nome, {}) or {}
            carriers.append({
                "nome": nome,
                "habilitado": bool(sec.get("habilitado", False)),
                "ufs_atendidas": self._norm_ufs(sec.get("ufs_atendidas")),
                "campos": [
                    {
                        "key": k, "label": lbl, "tipo": tp,
                        "valor": "" if tp == "password" else str(sec.get(k, "") or ""),
                        "tem_valor": bool(str(sec.get(k, "") or "")),
                    }
                    for k, lbl, tp in fields
                ],
            })

        try:
            paralelas = int(fb.get("max_paralelo", 3) or 3)
        except (TypeError, ValueError):
            paralelas = 3
        return {
            "empresa": {
                "nome": self._empresa,
                "cep_origem": str(rom.get("cep_origem", "") or ""),
                "cnpj_pagador": str(rom.get("cnpj_pagador_padrao", "") or ""),
                "paralelas": paralelas,
            },
            "aparencia": {
                "tema": str(fb.get("ui_tema", "sistema")).lower(),
                "raio": str(fb.get("ui_raio", "Suave")),
                "botao": str(fb.get("ui_botao", "Solido")),
                "accent": str(fb.get("ui_accent", "Claude")),
                "temas": ["claro", "escuro", "sistema"],
                "raios": ["Reto", "Suave", "Arredondado"],
                "botoes": ["Solido", "Suave"],
            },
            "transportadoras": carriers,
            "ufs": list(cc.TODAS_UFS),
        }

    def _write_config(self, mutate) -> bool:
        try:
            cfg = _load_config_for_write(self._config_path)
        except _ConfigUnsafeToWrite:
            return False
        try:
            mutate(cfg)
            cc._escrever_config_toml(cfg, self._config_path)
            return True
        except Exception:
            return False

    def config_salvar_empresa(self, data: dict) -> dict:
        import re
        data = data or {}

        def mut(cfg):
            rom = cfg.setdefault("romaneio", {})
            cep = re.sub(r"\D", "", str(data.get("cep_origem", "")))
            rom["cep_origem"] = cep
            rom["cnpj_pagador_padrao"] = str(data.get("cnpj_pagador", "")).strip()
            fb = cfg.setdefault("fretio", {})
            try:
                fb["max_paralelo"] = max(1, min(7, int(str(data.get("paralelas", "3")).strip() or "3")))
            except (TypeError, ValueError):
                fb["max_paralelo"] = 3

        return {"ok": self._write_config(mut)}

    def config_salvar_aparencia(self, data: dict) -> dict:
        data = data or {}
        tema = str(data.get("tema", "sistema")).lower()
        if tema not in ("claro", "escuro", "sistema"):
            tema = "sistema"

        def mut(cfg):
            fb = cfg.setdefault("fretio", {})
            fb["ui_tema"] = tema
            if data.get("raio"):
                fb["ui_raio"] = str(data["raio"])
            if data.get("botao"):
                fb["ui_botao"] = str(data["botao"])

        ok = self._write_config(mut)
        return {
            "ok": ok, "tema_efetivo": _resolver_tema_efetivo(tema),
            "raio": data.get("raio"), "botao": data.get("botao"),
        }

    def config_salvar_transportadora(self, nome: str, data: dict) -> dict:
        nome = str(nome)
        data = data or {}

        def mut(cfg):
            t = cfg.setdefault("transportadoras", {}).setdefault(nome, {})
            if "habilitado" in data:
                t["habilitado"] = bool(data["habilitado"])
            if "ufs_atendidas" in data:
                t["ufs_atendidas"] = self._norm_ufs(data.get("ufs_atendidas"))

        return {"ok": self._write_config(mut)}

    def config_salvar_credenciais(self, nome: str, campos: dict) -> dict:
        import secure_credentials

        nome = str(nome)
        allowed = {k for k, _, _ in _CARRIER_FIELDS.get(nome, [])}
        senha_keys = {k for k, _, tp in _CARRIER_FIELDS.get(nome, []) if tp == "password"}

        # Senhas vão para o Windows Credential Manager (aplicadas via
        # overlay_secure_credentials em tempo de cotação) e NUNCA são gravadas em
        # texto claro no CONFIG.toml (CWE-312). Senha em branco = manter a já salva.
        for k, v in (campos or {}).items():
            if k in senha_keys and k in allowed:
                v = str(v)
                if v:
                    secure_credentials.set_credential(self._empresa, nome, k, v)

        def mut(cfg):
            t = cfg.setdefault("transportadoras", {}).setdefault(nome, {})
            for k, v in (campos or {}).items():
                if k not in allowed or k in senha_keys:
                    continue
                t[k] = str(v)
            # Nunca mantém senha em texto claro no TOML: migra qualquer valor legado
            # em claro para o Credential Manager antes de removê-lo do arquivo.
            for sk in senha_keys:
                provided = str((campos or {}).get(sk, "")) if sk in (campos or {}) else ""
                legacy = str(t.get(sk) or "")
                if not provided and legacy:
                    secure_credentials.set_credential(self._empresa, nome, sk, legacy)
                t.pop(sk, None)

        return {"ok": self._write_config(mut)}
