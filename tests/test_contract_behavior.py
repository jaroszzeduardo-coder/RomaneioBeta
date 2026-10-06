import json
import sys
import time
from pathlib import Path
from urllib.error import URLError

import pytest


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

import cotacao_transportadoras as ct
import extrator_nfe as nfe
import updater
from fretio.config_manager import ConfigManager


def _clear_license_api_env(monkeypatch):
    for env_name in ("FRETIO_LICENSE_API_URL", "FRETEBOT_LICENSE_API_URL", "Fretio_LICENSE_API_URL"):
        monkeypatch.delenv(env_name, raising=False)


def test_contract_parse_romaneio_colado_preserves_shipping_payload():
    romaneio = """
    <p>DESTINATARIO</p>
    CNPJ/CPF: 12.345.678/0001-90<br>
    Cidade: Porto Alegre / RS<br>
    CEP: 90010-123<br>
    - VOL: 3<br>
    - CUBAGEM: 0,132 m3<br>
    - PESO: 9,900 kg<br>
    - TOTAL: R$ 1.250,75<br>
    2 x Caixas fechadas - 1,650 kg - 0,044 m3 - 31x31x45<br>
    1 x Caixa grande - 6,600 kg - 0,088 m3 - 40x40x55<br>
    Produto A: 2 und<br>
    Produto B: 1 und<br>
    """

    assert ct._dados_envio_romaneio_colado(romaneio) == {
        "destino_cep": "90010123",
        "uf_destino": "RS",
        "cidade_destino": "Porto Alegre",
        "cnpj_destinatario": "12345678000190",
        "peso": 9.9,
        "valor": 1250.75,
        "volumes": 3,
        "cubagem_m3": 0.132,
        "comprimento_cm": 55,
        "largura_cm": 40,
        "altura_cm": 40,
        "cubagens": [
            {
                "quantidade": 2,
                "comprimento_cm": 45,
                "largura_cm": 31,
                "altura_cm": 31,
                "peso_por_volume_kg": 1.65,
            },
            {
                "quantidade": 1,
                "comprimento_cm": 55,
                "largura_cm": 40,
                "altura_cm": 40,
                "peso_por_volume_kg": 6.6,
            },
        ],
        "descricoes_itens": ["Produto A", "Produto B"],
        "numero_destino": "",
    }


def test_contract_formatar_resultados_cotacao_orders_and_marks_best(monkeypatch):
    monkeypatch.setattr(ct, "_diag_log_enabled", lambda: False)

    resultados = [
        ct.ResultadoCotacao("ALFA", "ok", valor_frete=123.45, prazo_dias=3),
        ct.ResultadoCotacao("TRD", "ok", valor_frete=123.45, prazo_dias=2),
        ct.ResultadoCotacao("AGEX", "ok", valor_frete=99.0, prazo_dias=5),
        ct.ResultadoCotacao("BRASPRESS", "erro", detalhes="falha fake"),
    ]

    assert ct.formatar_resultados_cotacao(resultados) == (
        "AGEX   R$ 99,00   5 dia(s)\n"
        "TRD   R$ 123,45   2 dia(s)\n"
        "ALFA   R$ 123,45   3 dia(s)\n"
        "\n"
        "Melhor frete: AGEX   R$ 99,00"
    )


def test_contract_formatar_resultados_cotacao_empty_and_blocked(monkeypatch):
    monkeypatch.setattr(ct, "_diag_log_enabled", lambda: False)

    assert ct.formatar_resultados_cotacao([]) == "Nenhuma cotacao valida retornada"
    assert ct.formatar_resultados_cotacao(
        [ct.ResultadoCotacao("TRD", "erro_divergencia_uf", detalhes="CEP SP, romaneio RS")]
    ) == "COTACAO BLOQUEADA:\nCEP SP, romaneio RS"


@pytest.fixture
def clean_config_manager():
    ConfigManager._instances.clear()
    ConfigManager._config_cache.clear()
    ConfigManager._file_mtimes.clear()
    yield
    ConfigManager._instances.clear()
    ConfigManager._config_cache.clear()
    ConfigManager._file_mtimes.clear()


def test_contract_config_manager_loads_company_config_before_fallbacks(
    monkeypatch,
    tmp_path,
    clean_config_manager,
):
    appdata = tmp_path / "appdata"
    company_cfg = appdata / "Fretio" / "empresas" / "DARLU" / "CONFIG.toml"
    company_cfg.parent.mkdir(parents=True)
    company_cfg.write_text(
        "[romaneio]\n"
        'cep_origem = "11111111"\n'
        "[transportadoras.trd]\n"
        "habilitado = true\n",
        encoding="utf-8",
    )
    base = tmp_path / "base"
    base.mkdir()
    (base / "CONFIG.toml").write_text('[romaneio]\ncep_origem = "22222222"\n', encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("PROGRAMDATA", raising=False)

    cm = ConfigManager.get_instance("DARLU")
    monkeypatch.setattr(cm, "_base_dir", lambda: base)

    config = cm.load_config()

    assert config["romaneio"]["cep_origem"] == "11111111"
    assert config["transportadoras"]["trd"]["habilitado"] is True
    assert cm._config_path == company_cfg


def test_contract_config_manager_uses_hardcoded_fallback_when_no_config_exists(
    monkeypatch,
    tmp_path,
    clean_config_manager,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    cm = ConfigManager.get_instance("SEM_CONFIG")
    monkeypatch.setattr(cm, "_base_dir", lambda: tmp_path / "base_inexistente")

    config = cm.load_config()

    assert sorted(config.keys()) == ["fretio", "romaneio", "transportadoras"]
    assert config["fretio"]["fator_cubagem"] == 6000
    assert config["romaneio"]["cep_origem"] == "99740000"
    assert config["transportadoras"]["braspress"]["habilitado"] is True


def test_contract_updater_check_for_update_returns_none_for_current_version(monkeypatch):
    calls = []

    def fake_api(url):
        calls.append(url)
        return {
            "tag_name": "v2.0.0",
            "body": "notes",
            "html_url": "https://example.test/release",
            "assets": [
                {
                    "name": "Fretio-Update-latest.zip",
                    "browser_download_url": "https://example.test/update.zip",
                    "size": 123,
                }
            ],
        }

    monkeypatch.setattr(updater, "get_repo_candidates_from_config", lambda: [])
    monkeypatch.setattr(updater, "_github_api", fake_api)

    assert updater.check_for_update("owner/releases", "2.0.0") is None
    assert calls == ["https://api.github.com/repos/owner/releases/releases/latest"]


def test_contract_updater_check_for_update_returns_update_info(monkeypatch):
    monkeypatch.setattr(updater, "get_repo_candidates_from_config", lambda: [])
    monkeypatch.setattr(
        updater,
        "_github_api",
        lambda _url: {
            "tag_name": "v2.1.0",
            "body": "Notas da release",
            "html_url": "https://example.test/releases/v2.1.0",
            "assets": [
                {"name": "debug.txt", "browser_download_url": "https://example.test/debug.txt", "size": 1},
                {
                    "name": "Fretio-Update-latest.zip",
                    "browser_download_url": "https://example.test/Fretio-Update-latest.zip",
                    "size": 456,
                },
            ],
        },
    )

    assert updater.check_for_update("owner/releases", "2.0.0") == updater.UpdateInfo(
        tag="v2.1.0",
        version="2.1.0",
        download_url="https://example.test/Fretio-Update-latest.zip",
        asset_name="Fretio-Update-latest.zip",
        asset_size=456,
        release_notes="Notas da release",
        html_url="https://example.test/releases/v2.1.0",
        source_repo="owner/releases",
    )


def test_contract_extrair_xml_nfe_preserves_parsed_fields(tmp_path):
    chave = "12345678901234567890123456789012345678901234"
    xml_path = tmp_path / "nfe.xml"
    xml_path.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe">
  <NFe>
    <infNFe Id="NFe{chave}">
      <ide>
        <nNF>1234</nNF>
        <serie>1</serie>
        <dhEmi>2026-05-01T10:00:00-03:00</dhEmi>
      </ide>
      <emit>
        <CNPJ>11111111000111</CNPJ>
        <xNome>Emitente Teste LTDA</xNome>
      </emit>
      <dest>
        <CNPJ>22222222000122</CNPJ>
        <xNome>Prefeitura Teste</xNome>
        <enderDest>
          <xMun>Porto Alegre</xMun>
          <UF>RS</UF>
          <CEP>90010123</CEP>
        </enderDest>
      </dest>
      <det nItem="1"><prod><xProd>Luva Cirurgica - Caixa</xProd></prod></det>
      <det nItem="2"><prod><xProd>Mascara Descartavel Tripla</xProd></prod></det>
      <transp>
        <transporta>
          <CNPJ>33333333000133</CNPJ>
          <xNome>RTE Rodonaves</xNome>
        </transporta>
        <vol><qVol>2</qVol><pesoB>10.500</pesoB><pesoL>9.500</pesoL></vol>
        <vol><qVol>1</qVol><pesoB>2.000</pesoB><pesoL>1.800</pesoL></vol>
      </transp>
      <total><ICMSTot><vNF>1234.56</vNF><vFrete>78.90</vFrete></ICMSTot></total>
      <infAdic><infCpl>PROCESSO: 456</infCpl><infAdFisco>FISCO OK</infAdFisco></infAdic>
    </infNFe>
  </NFe>
</nfeProc>
""",
        encoding="utf-8",
    )

    notas = nfe.extrair_xml(str(xml_path))

    assert len(notas) == 1
    assert notas[0] == nfe.NotaFiscal(
        chave_acesso=chave,
        numero="1234",
        serie="1",
        data_emissao="2026-05-01T10:00:00-03:00",
        emitente_nome="Emitente Teste LTDA",
        emitente_cnpj="11111111000111",
        destinatario_nome="Prefeitura Teste",
        destinatario_cnpj="22222222000122",
        destinatario_uf="RS",
        destinatario_cidade="Porto Alegre",
        destinatario_cep="90010123",
        transportadora_nome="RTE Rodonaves",
        transportadora_cnpj="33333333000133",
        volumes=3,
        peso_bruto=12.5,
        peso_liquido=11.3,
        valor_total=1234.56,
        valor_frete=78.9,
        produtos_resumo="LUVA CIRURGICA, MASCARA DESCARTAVEL",
        info_complementar="PROCESSO: 456 | FISCO OK",
        arquivo_origem=str(xml_path),
    )
    assert nfe.identificar_transportadora(notas[0]) == "rodonaves"
    assert nfe.formatar_nota_resumo(notas[0]) == (
        "NF-e: 1234 (Serie 1)\n"
        f"Chave: {chave}\n"
        "Emitente: Emitente Teste LTDA\n"
        "Destinatario: Prefeitura Teste (Porto Alegre/RS)\n"
        "CEP destino: 90010123\n"
        "Transportadora: RTE Rodonaves\n"
        "Volumes: 3\n"
        "Peso bruto: 12.50 kg\n"
        "Valor NF: R$ 1.234,56\n"
        "Valor frete: R$ 78,90"
    )


def test_contract_parsear_info_complementar_delivery_fields():
    info = (
        "PEDIDO DE COMPRA: PC-123 | PROCESSO: 456 | "
        "LOCAL DE ENTREGA: Almoxarifado Central | "
        "ENDEREÇO: Rua Um, 123 | BAIRRO: Centro | CEP: 90010123 | "
        "CIDADE/UF: Porto Alegre/rs | AGENDAMENTO: SIM | "
        "HORÁRIO: 08:00 as 12:00 | CONTATO: Maria | "
        "TELEFONE: (51) 99999-0000 | OBS: Entregar na doca 2"
    )

    assert nfe.parsear_info_complementar(info) == {
        "pedido_compra": "PC-123",
        "processo": "456",
        "local_entrega": "Almoxarifado Central\nRua Um, 123, BAIRRO Centro\nCEP 90010-123\nPorto Alegre/RS",
        "local_entrega_nome": "Almoxarifado Central",
        "endereco_entrega": "Rua Um, 123, BAIRRO Centro",
        "bairro_entrega": "Centro",
        "cep_entrega": "90010-123",
        "cidade_uf_entrega": "Porto Alegre/RS",
        "agendamento": "SIM",
        "horario": "08:00 as 12:00",
        "contato": "Maria",
        "recebedor": "Maria",
        "telefone": "(51) 99999-0000",
        "outras_info_entrega": "Entregar na doca 2",
    }
