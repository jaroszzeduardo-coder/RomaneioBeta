import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

import rastreamento
from rastreamento_common import ResultadoRastreio


class _Page:
    async def screenshot(self, **kwargs):
        self.kwargs = kwargs


def test_consulta_em_transito_tambem_salva_screenshot(monkeypatch):
    chamadas = []

    async def salvar(page, resultado, numero_nfe):
        chamadas.append((page, numero_nfe))
        resultado.screenshot_path = "captura.png"

    monkeypatch.setattr(rastreamento, "_salvar_screenshot_entrega", salvar)
    resultado = ResultadoRastreio(numero_nfe="123", transportadora="AGEX")
    page = _Page()

    asyncio.run(
        rastreamento._aplicar_resultado_texto(
            resultado,
            "123",
            "Status: Em trânsito\nPrevisão de entrega: 10/10/2026",
            page,
        )
    )

    assert resultado.entregue is False
    assert resultado.screenshot_path == "captura.png"
    assert chamadas == [(page, "123")]


def test_links_publicos_de_rastreio_atualizados():
    assert rastreamento._TRACKING_URLS["braspress"] == "https://blue.braspress.com/site/w/tracking/view"
    assert "sigla_emp=CLD" in rastreamento._TRACKING_URLS["coopex"]
    assert rastreamento._TRACKING_URLS["rodonaves"].endswith("/rastreio-de-mercadoria.html")
