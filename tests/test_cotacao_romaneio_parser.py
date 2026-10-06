import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

from cotacao.romaneio_parser import (
    _dados_envio_romaneio_colado,
    _normalizar_romaneio_colado,
    _selecionar_cep_destino,
)


def _romaneio_valido(*, cep: str = "90010-123", uf_linha: str = "Cidade: Porto Alegre / RS") -> str:
    return f"""
    DESTINATARIO
    CNPJ/CPF: 12.345.678/0001-90
    Endereco: Rua Exemplo, 123
    {uf_linha}
    CEP: {cep}
    - VOL: 2
    - CUBAGEM: 0,044 m3
    - PESO: 3,300 kg
    - TOTAL: R$ 150,99
    2 x Caixas fechadas - 1,650 kg - 0,044 m3 - 31x31x45
    Produto Teste: 2 und
    """


def test_normalizar_romaneio_colado_removes_html_and_blank_lines():
    assert _normalizar_romaneio_colado("Linha 1<br>\n<p>Linha   2</p>\n\nLinha 3") == "Linha 1\nLinha 2\nLinha 3"


def test_selecionar_cep_destino_prefers_uf_hint_after_reference():
    texto = "Origem CEP: 99740-000\nDestinatario CNPJ/CPF: 12.345.678/0001-90\nCidade: Porto Alegre / RS\nCEP: 90010-123"
    pos_ref = texto.index("12.345")
    assert _selecionar_cep_destino(texto, pos_referencia=pos_ref, uf_hint="RS") == "90010123"


def test_dados_envio_romaneio_colado_extracts_core_fields():
    dados = _dados_envio_romaneio_colado(_romaneio_valido())
    assert dados["destino_cep"] == "90010123"
    assert dados["numero_destino"] == "123"
    assert dados["uf_destino"] == "RS"
    assert dados["cidade_destino"] == "Porto Alegre"
    assert dados["cnpj_destinatario"] == "12345678000190"
    assert dados["peso"] == 3.3
    assert dados["valor"] == 150.99
    assert dados["volumes"] == 2
    assert dados["cubagem_m3"] == 0.044
    assert dados["comprimento_cm"] == 45
    assert dados["largura_cm"] == 31
    assert dados["altura_cm"] == 31
    assert dados["cubagens"] == [
        {
            "quantidade": 2,
            "comprimento_cm": 45,
            "largura_cm": 31,
            "altura_cm": 31,
            "peso_por_volume_kg": 1.65,
        }
    ]
    assert dados["descricoes_itens"] == ["Produto Teste"]


def test_dados_envio_romaneio_colado_accepts_masked_and_unmasked_cep():
    assert _dados_envio_romaneio_colado(_romaneio_valido(cep="90010-123"))["destino_cep"] == "90010123"
    assert _dados_envio_romaneio_colado(_romaneio_valido(cep="90010123"))["destino_cep"] == "90010123"


def test_dados_envio_romaneio_colado_uses_cep_after_recipient_cnpj():
    dados = _dados_envio_romaneio_colado(
        """
        REMETENTE
        CEP: 99740-000
        CNPJ/CPF: 11.111.111/0001-11
        DESTINATARIO
        CNPJ/CPF: 12.345.678/0001-90
        Cidade: Porto Alegre / RS
        CEP: 90010-123
        - VOL: 1
        - CUBAGEM: 0,044 m3
        - PESO: 3,300 kg
        - TOTAL: R$ 150,99
        1 x Caixas fechadas - 3,300 kg - 0,044 m3 - 31x31x45
        """
    )

    assert dados["cnpj_destinatario"] == "12345678000190"
    assert dados["destino_cep"] == "90010123"


def test_dados_envio_romaneio_colado_infers_uf_from_cep_when_uf_absent():
    dados = _dados_envio_romaneio_colado(_romaneio_valido(uf_linha="Cidade: Porto Alegre"))
    assert dados["destino_cep"] == "90010123"
    assert dados["uf_destino"] == "RS"


def test_dados_envio_romaneio_colado_reports_empty_text_clearly():
    try:
        _dados_envio_romaneio_colado(" \n\t ")
    except ValueError as exc:
        assert str(exc) == "Romaneio colado vazio"
    else:
        raise AssertionError("romaneio vazio deveria falhar")


def test_dados_envio_romaneio_colado_reports_missing_cnpj_clearly():
    try:
        _dados_envio_romaneio_colado(_romaneio_valido().replace("CNPJ/CPF: 12.345.678/0001-90", ""))
    except ValueError as exc:
        assert "Campos ausentes: CNPJ" in str(exc)
    else:
        raise AssertionError("romaneio sem CNPJ deveria falhar")


def test_dados_envio_romaneio_colado_reports_missing_cep_clearly():
    try:
        _dados_envio_romaneio_colado(_romaneio_valido().replace("CEP: 90010-123", ""))
    except ValueError as exc:
        assert "Campos ausentes: CEP" in str(exc)
    else:
        raise AssertionError("romaneio sem CEP deveria falhar")


def test_dados_envio_romaneio_colado_reports_missing_cubagem_clearly():
    try:
        _dados_envio_romaneio_colado(_romaneio_valido().replace("- CUBAGEM: 0,044 m3", ""))
    except ValueError as exc:
        assert "Campos ausentes: CUBAGEM" in str(exc)
    else:
        raise AssertionError("romaneio sem cubagem deveria falhar")


def test_dados_envio_romaneio_colado_blocks_negative_total():
    try:
        _dados_envio_romaneio_colado(_romaneio_valido().replace("- TOTAL: R$ 150,99", "- TOTAL: R$ -150,99"))
    except ValueError as exc:
        assert "Campo TOTAL não pode ser negativo" in str(exc)
    else:
        raise AssertionError("romaneio com total negativo deveria falhar")


@pytest.mark.parametrize(
    ("original", "replacement", "expected_message"),
    [
        ("- VOL: 2", "- VOL: 0", "Campo VOL deve ser maior que zero"),
        ("- PESO: 3,300 kg", "- PESO: 0,000 kg", "Campo PESO deve ser maior que zero"),
        ("- CUBAGEM: 0,044 m3", "- CUBAGEM: 0,000 m3", "Campo CUBAGEM deve ser maior que zero"),
    ],
)
def test_dados_envio_romaneio_colado_blocks_non_positive_totals(
    original,
    replacement,
    expected_message,
):
    with pytest.raises(ValueError, match=expected_message):
        _dados_envio_romaneio_colado(_romaneio_valido().replace(original, replacement))
