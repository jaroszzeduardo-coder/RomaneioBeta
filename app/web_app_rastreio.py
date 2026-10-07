"""Fretio — RastreioMixin (rastreio de NF-e carregadas).

Extraído de web_app.py; opera sobre o estado do Api via self. Importa helpers
compartilhados de web_app_shared (nunca de web_app) para evitar ciclo.
"""
from __future__ import annotations

from typing import Any

from web_app_shared import chave_nota


class RastreioMixin:
    """Rastreio de NF-e carregadas. Delegate de domínio do Api (opera sobre self;
    superfície pública pywebview inalterada)."""

    def rastreio_limpar(self) -> dict:
        self._notas = []
        return {"ok": True}

    def _ser_rastreio(self, r: Any) -> dict | None:
        if r is None:
            return None
        return {
            "numero_nfe": getattr(r, "numero_nfe", ""),
            "transportadora": getattr(r, "transportadora", ""),
            "entregue": bool(getattr(r, "entregue", False)),
            "previsao_entrega": getattr(r, "previsao_entrega", ""),
            "link_rastreio": getattr(r, "link_rastreio", ""),
            "screenshot_path": getattr(r, "screenshot_path", ""),
            "status_texto": getattr(r, "status_texto", ""),
            "erro": getattr(r, "erro", ""),
        }

    def rastreio_iniciar(self, chaves: list | None = None) -> dict:
        self._apply_pending_runtime_config()
        if not self._notas:
            return {"erro": "Nenhuma NF-e carregada para rastrear"}
        bloqueio = self._gate("rastreio")
        if bloqueio:
            return bloqueio
        with self._op_lock:
            if self._rastreando:
                return {"erro": "Já existe um rastreamento em andamento"}
            self._rastreando = True
        try:
            self._ensure_backend()
        except Exception as exc:
            self._rastreando = False
            return {"erro": f"Falha ao preparar o rastreamento: {exc}"}
        try:
            from fretio.providers.base import find_chrome
            find_chrome()
        except FileNotFoundError:
            from cotacao_transportadoras import CHROME_MISSING_USER_MESSAGE
            self._rastreando = False
            self._emit("chrome_missing", {"texto": CHROME_MISSING_USER_MESSAGE})
            return {"erro": CHROME_MISSING_USER_MESSAGE}

        alvo = self._notas
        if chaves:
            cset = set(chaves)
            # Casa pela chave canônica (chave_nota), não por chave_acesso cru: do
            # contrário NF-e sem chave de 44 dígitos (cuja chave do frontend é
            # "nf-<numero>") nunca entrariam no subconjunto selecionado.
            sub = [n for n in self._notas if chave_nota(n) in cset]
            if sub:
                alvo = sub
        fut = self._loop.submit(lambda: self._coro_rastreio(list(alvo)))
        if fut is None:
            self._rastreando = False
            return {"erro": "Não foi possível iniciar o rastreamento"}
        return {"ok": True, "total": len(alvo)}

    async def _coro_rastreio(self, alvo: list) -> None:
        from rastreamento import rastrear_multiplas
        from extrator_nfe import identificar_transportadora

        notas_track: list[dict] = []
        chaves: list[str] = []
        for nf in alvo:
            notas_track.append({
                "transportadora": identificar_transportadora(nf),
                "numero_nfe": nf.numero,
                "cnpj_emitente": nf.emitente_cnpj,
                "chave_acesso": nf.chave_acesso,
            })
            chaves.append(chave_nota(nf))

        def _cb(indice: int, total: int, resultado: Any) -> None:
            chave = chaves[indice - 1] if 0 <= indice - 1 < len(chaves) else ""
            self._emit("rastreio_progress", {
                "chave": chave, "indice": indice, "total": total,
                "resultado": self._ser_rastreio(resultado),
            })

        try:
            resultados = await rastrear_multiplas(notas_track, callback=_cb)
            entregues = sum(1 for r in resultados if getattr(r, "entregue", False))
            com_ss = sum(1 for r in resultados if getattr(r, "screenshot_path", ""))
            self._emit("rastreio_finished", {
                "total": len(resultados), "entregues": entregues, "screenshots": com_ss,
            })
        except Exception as exc:
            self._emit("rastreio_finished", {
                "total": 0, "entregues": 0, "screenshots": 0, "erro": str(exc),
            })
        finally:
            self._rastreando = False
