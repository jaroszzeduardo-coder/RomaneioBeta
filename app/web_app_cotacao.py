"""Fretio — CotacaoMixin (cotação de frete: serialização, callbacks, coroutine).

Extraído de web_app.py; opera sobre o estado do Api via self. Importa helpers
compartilhados de web_app_shared (nunca de web_app) para evitar ciclo.
"""
from __future__ import annotations

from typing import Any


class CotacaoMixin:
    """Cotação de frete: serialização de resultado, callbacks de progresso e a
    coroutine de cotação. Delegate de domínio do Api (opera sobre self; superfície
    pública pywebview inalterada). _ensure_backend fica no Api (infra compartilhada
    com o rastreio)."""

    @staticmethod
    def _num(v: Any) -> Any:
        if v is None:
            return None
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return None

    def _ser_resultado(self, r: Any) -> dict | None:
        if r is None:
            return None
        return {
            "status": getattr(r, "status", None),
            "valor_frete": self._num(getattr(r, "valor_frete", None)),
            "prazo_dias": self._num(getattr(r, "prazo_dias", None)),
            "transportadora": getattr(r, "transportadora", None),
            "detalhes": getattr(r, "detalhes", None),
            "duration_ms": self._num(getattr(r, "duration_ms", None)),
        }

    def _cb_progress(self, payload: dict) -> None:
        p = dict(payload or {})
        self._emit("cotacao_progress", {
            "provider": p.get("provider"),
            "status": p.get("status"),
            "stage": p.get("stage"),
            "mensagem": p.get("mensagem"),
            "duration_ms": self._num(p.get("duration_ms")),
            "resultado": self._ser_resultado(p.get("resultado")),
        })

    def _cb_status(self, msg: str) -> None:
        self._emit("status_update", {"texto": str(msg or "")})

    def _cb_login(self, nome: str, status: str) -> None:
        self._emit("login_status", {"nome": str(nome or ""), "status": str(status or "")})

    def cotacao_iniciar(self, romaneio_texto: str, cnpj_remetente: str = "", cep_origem: str = "") -> dict:
        self._apply_pending_runtime_config()
        texto = str(romaneio_texto or "").strip()
        if not texto:
            return {"erro": "Cole um romaneio antes de cotar"}
        bloqueio = self._gate("cotacao")
        if bloqueio:
            return bloqueio
        # Romaneio colado (sem cnpj_remetente) também passa pelo gate "romaneio";
        # cotação de fornecedor/FOB (cnpj_remetente preenchido) fica só sob "cotacao".
        if not str(cnpj_remetente or "").strip():
            bloqueio_rom = self._gate("romaneio")
            if bloqueio_rom:
                return bloqueio_rom
        with self._op_lock:
            if self._cotando:
                return {"erro": "Já existe uma cotação em andamento"}
            self._cotando = True
        try:
            self._ensure_backend()
        except Exception as exc:  # backend indisponível
            self._cotando = False
            return {"erro": f"Falha ao preparar a cotação: {exc}"}

        fut = self._loop.submit(lambda: self._coro_cotacao(texto, cnpj_remetente, cep_origem))
        if fut is None:
            self._cotando = False
            return {"erro": "Não foi possível iniciar a cotação"}
        return {"ok": True}

    async def _coro_cotacao(self, texto: str, cnpj_remetente: str, cep_origem: str) -> None:
        from cotacao_transportadoras import (
            cotar_transportadoras_romaneio_colado,
            formatar_resultados_cotacao,
            CHROME_MISSING_USER_MESSAGE,
        )
        try:
            if not self._sessao.pronto:
                self._emit("status_update", {"texto": "Executando pré-login antes da cotação..."})
                await self._sessao.inicializar(
                    callback=self._cb_status,
                    login_status_callback=self._cb_login,
                )
                if self._sessao.chrome_missing:
                    self._emit("cotacao_result", {"resumo": CHROME_MISSING_USER_MESSAGE})
                    self._emit("chrome_missing", {"texto": CHROME_MISSING_USER_MESSAGE})
                    return

            kwargs: dict[str, Any] = dict(
                romaneio_colado=texto,
                cep_origem=cep_origem or "",
                sessao=self._sessao,
                progresso_callback=self._cb_progress,
            )
            if cnpj_remetente:
                kwargs["cnpj_remetente"] = cnpj_remetente
                kwargs["tipo_frete"] = "2"  # FOB (modo fornecedor)
            resultados = await cotar_transportadoras_romaneio_colado(**kwargs)
            self._last_cotacao = list(resultados or [])
            resumo = formatar_resultados_cotacao(resultados)
            self._emit("cotacao_result", {"resumo": resumo})
            self._emit("dashboard_dirty", {})
        except Exception as exc:  # falha técnica
            self._emit("cotacao_result", {
                "resumo": "Erro ao cotar transportadoras. Tente novamente em alguns minutos.",
            })
            self._emit("toast", {"texto": f"Erro na cotação: {type(exc).__name__}"})
        finally:
            self._cotando = False
            self._emit("cotacao_finished", {})
