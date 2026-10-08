"""Fretio — StartupMixin (licença, versão/update e seleção/criação de empresa).

Extraído de web_app.py; opera sobre o estado do Api via self. Importa helpers
compartilhados de web_app_shared (nunca de web_app) para evitar ciclo.
"""
from __future__ import annotations

import os

import company_config as cc
from web_app_shared import _carregar_versao, _load_config


class StartupMixin:
    """Partida: licença, versão/update e seleção/criação de empresa.

    Delegate de domínio do god-object Api. Opera sobre o estado do Api (self) —
    superfície pública pywebview inalterada (test_char_web_app_api_surface.py)."""

    def startup_pos_licenca(self) -> dict:
        from startup import _carregar_versao_app
        from updater import get_repo_from_config, check_for_update

        cur = _carregar_versao_app()
        repo = get_repo_from_config()
        info = None
        try:
            info = check_for_update(repo, cur) if repo else None
        except Exception:
            info = None
        self._update_info = info
        upd = None
        if info:
            upd = {
                "version": getattr(info, "version", ""),
                "notes": str(getattr(info, "release_notes", "") or "")[:1500],
                "mandatory": bool(getattr(info, "mandatory", False)),
            }
        return {"bloqueado": False, "msg": "", "update": upd}

    def startup_aplicar_update(self) -> dict:
        info = self._update_info
        if not info:
            return {"ok": False, "erro": "Nenhuma atualização disponível"}
        from updater import apply_update, restart_app
        try:
            ok = apply_update(info, callback=lambda m: self._emit("startup_progress", {"texto": str(m or "")}))
        except Exception as exc:
            return {"ok": False, "erro": str(exc)}
        if ok:
            # restart_app() lança o _apply_update.bat e chama sys.exit(0); mas este
            # método roda numa thread do bridge do pywebview, onde sys.exit encerra só
            # a thread, não o processo. O .bat espera ESTE PID terminar para copiar os
            # arquivos — sem encerrar o processo, o update trava em "Reiniciando…".
            try:
                restart_app()  # lança o .bat
            except SystemExit:
                pass           # o sys.exit(0) interno não mata o processo aqui
            except Exception as exc:
                return {
                    "ok": False,
                    "erro": f"A atualiza��o foi baixada, mas a instala��o não iniciou: {exc}",
                }
            try:
                self._teardown()  # fecha Chrome/Playwright antes de sair
            except Exception:
                pass
            os._exit(0)  # encerra o PROCESSO inteiro -> o .bat aplica e reinicia
        return {"ok": False, "erro": "Não foi possível aplicar a atualização agora."}

    def startup_empresas(self) -> list:
        # Semeia a config por empresa a partir de um CONFIG.toml legado (raiz) em
        # upgrades, ANTES de listar — senão a lista vem vazia e o usuário perderia
        # as credenciais existentes. Idempotente (no-op se já houver empresas).
        try:
            cc._migrar_config_se_necessario()
        except Exception:
            pass
        return cc._listar_empresas()

    def startup_criar_empresa(self, nome: str) -> dict:
        import re
        # Sanitiza separadores/reservados (espelha o seletor Qt antigo) antes de
        # criar a pasta, e rejeita nomes que escapem de empresas/ (path traversal).
        nome = re.sub(r'[<>:"/\\|?*]', "_", str(nome or "").strip())
        if not nome or nome in (".", ".."):
            return {"ok": False, "erro": "Informe um nome válido para a empresa."}
        try:
            dest = cc._empresa_config_path(nome).resolve()
            if cc._empresas_dir().resolve() not in dest.parents:
                return {"ok": False, "erro": "Nome de empresa inválido."}
        except Exception:
            return {"ok": False, "erro": "Nome de empresa inválido."}
        # Duplicata case-insensitive (o filesystem do Windows não diferencia caixa).
        if nome.lower() in {e.lower() for e in cc._listar_empresas()}:
            return {"ok": False, "erro": "Já existe uma empresa com esse nome."}
        try:
            cc._criar_config_empresa_vazia(nome)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "erro": str(exc)}

    def startup_renomear_empresa(self, atual: str, novo: str) -> dict:
        atual, novo = str(atual or "").strip(), str(novo or "").strip()
        if not atual or not novo:
            return {"ok": False, "erro": "Nome inválido."}
        try:
            ok = cc._renomear_pasta_empresa(atual, novo)
            return {"ok": bool(ok), "erro": "" if ok else "Não foi possível renomear."}
        except Exception as exc:
            return {"ok": False, "erro": str(exc)}

    def startup_entrar(self, empresa: str) -> dict:
        empresa = str(empresa or "").strip()
        if not empresa:
            return {"ok": False, "erro": "Empresa inválida."}
        # Trocar de empresa reusa a mesma instância Api (navegação por load_url),
        # então encerra a sessão/loop da empresa anterior e zera o estado em memória
        # — senão a cotação/rastreio da empresa B reusaria a sessão/credenciais da A
        # (e o dashboard/NF-e vazaria entre empresas).
        try:
            self._teardown()
        except Exception:
            pass
        self._notas = []
        self._romaneios = []
        self._last_cotacao = []
        self._romaneio_texto = ""
        self._empresa = empresa
        self._config_path = cc._empresa_config_path(empresa)
        self._versao = _carregar_versao()
        try:
            cc._salvar_ultima_empresa(empresa)
        except Exception:
            pass
        try:
            from error_reporter import configure as _er_configure
            _er_configure(self._config_path)
        except Exception:
            pass
        try:
            cfg = _load_config(self._config_path)
            changed = cc._garantir_defaults_fretio(cfg)
            changed = cc._garantir_defaults_empresa(cfg) or changed
            if changed:
                cc._escrever_config_toml(cfg, self._config_path)
        except Exception:
            pass
        return {"ok": True}
