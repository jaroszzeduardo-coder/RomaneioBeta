"""Parser de romaneio colado e extração dos dados de envio."""

from __future__ import annotations

from typing import Any
import re

from .validation import _digits, _cep, _cep_para_uf

def _to_float_br(value: str) -> float:
    txt = re.sub(r"[^\d,.\-]", "", str(value or "").strip())
    if not txt:
        return 0.0
    if "," in txt and "." in txt:
        txt = txt.replace(".", "").replace(",", ".")
    elif "," in txt:
        txt = txt.replace(",", ".")
    return float(txt)


def _normalizar_romaneio_colado(texto: str) -> str:
    normalizado = str(texto or "").replace("\r\n", "\n").replace("\r", "\n")
    normalizado = re.sub(r"(?i)<br\s*/?>", "\n", normalizado)
    normalizado = re.sub(r"(?i)</p>", "\n", normalizado)
    normalizado = re.sub(r"(?i)<[^>]+>", " ", normalizado)
    normalizado = normalizado.replace("&nbsp;", " ")
    linhas = [re.sub(r"\s+", " ", linha).strip() for linha in normalizado.split("\n")]
    linhas = [linha for linha in linhas if linha]
    return "\n".join(linhas)


def _parse_dim_cm(raw: str) -> int:
    try:
        val = _to_float_br(raw)
    except ValueError:
        return 0
    # Aceita tanto dimensões em cm (31) quanto em metros (0,31).
    if 0 < val <= 3.5:
        return int(round(val * 100))
    return int(round(val))


def _extrair_cidade_uf_hint_texto(texto: str, pos_referencia: int = -1) -> tuple[str, str]:
    """Tenta extrair cidade/UF próxima ao bloco do destinatário."""
    pattern = re.compile(
        r"\b([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{2,})\s*/\s*([A-Za-z]{2})\b",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(texto or ""))
    if not matches:
        return "", ""

    selected = matches[0]
    if pos_referencia >= 0:
        depois = [m for m in matches if m.start() >= pos_referencia]
        if depois:
            selected = depois[0]
    cidade = re.sub(r"\s+", " ", str(selected.group(1) or "").strip(" -/"))
    uf = str(selected.group(2) or "").strip().upper()
    return cidade, uf


def _extrair_uf_hint_texto(texto: str, pos_referencia: int = -1) -> str:
    """Tenta extrair uma UF (cidade/UF) próxima ao bloco do destinatário."""
    _cidade, uf = _extrair_cidade_uf_hint_texto(texto, pos_referencia=pos_referencia)
    return uf


def _selecionar_cep_destino(texto: str, pos_referencia: int = -1, uf_hint: str = "") -> str:
    """
    Seleciona o CEP mais provável do destinatário.
    Regras:
    1) Preferir CEP após o CNPJ/CPF do destinatário.
    2) Se houver UF hint, priorizar CEPs compatíveis com essa UF.
    3) Priorizar CEP com rótulo explícito "CEP:".
    4) Em empate, usar o mais próximo da referência.
    """
    raw = str(texto or "")
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()

    label_pat = re.compile(r"\bCEP\s*:\s*(\d{2}\.?\d{3}-?\d{3}|\d{5}-?\d{3})\b", re.IGNORECASE)
    generic_pat = re.compile(r"\b(\d{5}-?\d{3})\b")

    for m in label_pat.finditer(raw):
        cep_digits = _cep(m.group(1))
        if len(cep_digits) != 8:
            continue
        key = (m.start(), cep_digits)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "cep": cep_digits,
                "pos": m.start(),
                "labeled": True,
                "uf": _cep_para_uf(cep_digits) or "",
            }
        )

    for m in generic_pat.finditer(raw):
        cep_digits = _cep(m.group(1))
        if len(cep_digits) != 8:
            continue
        key = (m.start(), cep_digits)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "cep": cep_digits,
                "pos": m.start(),
                "labeled": False,
                "uf": _cep_para_uf(cep_digits) or "",
            }
        )

    if not candidates:
        return ""

    ref = int(pos_referencia or 0)
    uf_ref = str(uf_hint or "").strip().upper()

    def _rank(c: dict[str, Any]) -> tuple[int, int, int, int]:
        pos = int(c.get("pos", 0) or 0)
        after = 0 if pos >= ref else 1
        dist = abs(pos - ref)
        labeled_penalty = 0 if bool(c.get("labeled")) else 1
        uf_penalty = 0
        if uf_ref:
            uf_penalty = 0 if str(c.get("uf", "")).upper() == uf_ref else 1
        return (uf_penalty, after, labeled_penalty, dist)

    candidates.sort(key=_rank)
    return str(candidates[0].get("cep", "") or "")


def _selecionar_cnpj_destinatario(texto: str) -> tuple[str, int]:
    matches = list(re.finditer(r"\bCNPJ/CPF\s*:\s*([0-9./-]{11,18})", texto or "", re.IGNORECASE))
    if not matches:
        return "", -1

    for marker_match in re.finditer(r"\bDESTINAT[ÁA]RIO\b", texto or "", re.IGNORECASE):
        marker_pos = marker_match.end()
        depois = [m for m in matches if m.start() >= marker_pos]
        if depois:
            m = depois[0]
            return _digits(m.group(1)), int(m.end())
    m = matches[0]
    return _digits(m.group(1)), int(m.end())


def _dados_envio_romaneio_colado(romaneio_colado: str) -> dict[str, Any]:
    texto = _normalizar_romaneio_colado(romaneio_colado)
    if not texto:
        raise ValueError("Romaneio colado vazio")

    cnpj_destinatario, pos_ref = _selecionar_cnpj_destinatario(texto)
    cidade_destino, uf_hint = _extrair_cidade_uf_hint_texto(texto, pos_referencia=pos_ref)
    destino_cep = _selecionar_cep_destino(texto, pos_referencia=pos_ref, uf_hint=uf_hint)
    uf_destino = str(uf_hint or "").strip().upper()
    if not uf_destino and len(destino_cep) == 8:
        uf_destino = str(_cep_para_uf(destino_cep) or "").strip().upper()

    m_volumes = re.search(r"-\s*VOL(?:UME)?\s*:\s*(\d+)", texto, re.IGNORECASE)
    m_cubagem = re.search(r"-\s*CUBAGEM\s*:\s*([\d.,]+)\s*m3", texto, re.IGNORECASE)
    m_peso = re.search(r"-\s*PESO\s*:\s*([\d.,]+)\s*kg", texto, re.IGNORECASE)
    m_total = re.search(r"-\s*TOTAL\s*:\s*R\$\s*(-?[\d.,]+)", texto, re.IGNORECASE)

    missing: list[str] = []
    if len(cnpj_destinatario) != 14:
        missing.append("CNPJ")
    if len(destino_cep) != 8:
        missing.append("CEP")
    if not m_volumes:
        missing.append("VOL")
    if not m_cubagem:
        missing.append("CUBAGEM")
    if not m_peso:
        missing.append("PESO")
    if not m_total:
        missing.append("TOTAL")
    if missing:
        raise ValueError(f"Romaneio colado inválido. Campos ausentes: {', '.join(missing)}")

    volumes = int(m_volumes.group(1))
    cubagem_m3 = _to_float_br(m_cubagem.group(1))
    peso = _to_float_br(m_peso.group(1))
    valor = _to_float_br(m_total.group(1))
    if volumes <= 0:
        raise ValueError("Romaneio colado inválido. Campo VOL deve ser maior que zero.")
    if peso <= 0:
        raise ValueError("Romaneio colado inválido. Campo PESO deve ser maior que zero.")
    if cubagem_m3 <= 0:
        raise ValueError("Romaneio colado inválido. Campo CUBAGEM deve ser maior que zero.")
    if valor < 0:
        raise ValueError("Romaneio colado inválido. Campo TOTAL não pode ser negativo.")

    cubagens: list[dict[str, Any]] = []

    # Ex.: "2 x Caixas fechadas - 1,650 kg - 0,044 m3 - 31x31x45"
    for m in re.finditer(
        r"(?im)^\s*(\d+)\s*x\s+.+?-\s*([\d.,]+)\s*kg\s*-\s*[\d.,]+\s*m3\s*-\s*([\d.,]+)\s*[xX×]\s*([\d.,]+)\s*[xX×]\s*([\d.,]+)\b",
        texto,
    ):
        try:
            qtd = int(m.group(1) or 0)
        except Exception:
            qtd = 0
        peso_por_volume_kg = _to_float_br(m.group(2))
        # Texto colado traz dimensões na ordem A×L×C (altura × largura × comprimento)
        a = _parse_dim_cm(m.group(3))
        l = _parse_dim_cm(m.group(4))
        c = _parse_dim_cm(m.group(5))
        if qtd <= 0 or a <= 0 or l <= 0 or c <= 0 or peso_por_volume_kg <= 0:
            continue
        cubagens.append(
            {
                "quantidade": qtd,
                "comprimento_cm": c,
                "largura_cm": l,
                "altura_cm": a,
                "peso_por_volume_kg": peso_por_volume_kg,
            }
        )

    comprimento_cm = 0
    largura_cm = 0
    altura_cm = 0
    for cub in cubagens:
        c = int(cub.get("comprimento_cm", 0) or 0)
        l = int(cub.get("largura_cm", 0) or 0)
        a = int(cub.get("altura_cm", 0) or 0)
        if c * l * a > comprimento_cm * largura_cm * altura_cm:
            comprimento_cm, largura_cm, altura_cm = c, l, a

    numero_endereco = re.search(
        r"(?im)^\s*(?:Endere[çc]o\s*:\s*)?(?:Rua|Avenida|Av\.?|Travessa|Alameda|Rodovia|Estrada)\s+[^\n]+?,\s*(\d+[A-Za-z]?)\b",
        texto,
    )
    descricoes_itens = []
    for m_desc in re.finditer(r"(?im)^\s*(\S+.*?):\s*\d+\s*und\b", texto):
        descricoes_itens.append(m_desc.group(1).strip())

    return {
        "destino_cep": destino_cep,
        "uf_destino": uf_destino,
        "cidade_destino": cidade_destino,
        "cnpj_destinatario": cnpj_destinatario,
        "peso": peso,
        "valor": valor,
        "volumes": volumes,
        "cubagem_m3": cubagem_m3,
        "comprimento_cm": comprimento_cm,
        "largura_cm": largura_cm,
        "altura_cm": altura_cm,
        "cubagens": cubagens,
        "descricoes_itens": descricoes_itens,
        "numero_destino": numero_endereco.group(1) if numero_endereco else "",
    }



__all__ = [name for name in globals() if not name.startswith("__")]
