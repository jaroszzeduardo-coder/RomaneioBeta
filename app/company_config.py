from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any


_DEFAULT_GITHUB_REPO = "jaroszzeduardo-coder/RomaneioBeta"

TODAS_UFS = [
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA",
    "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN",
    "RO", "RR", "RS", "SC", "SE", "SP", "TO",
]


def _fretio_appdata_dir() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        d = Path(appdata) / "Fretio"
    else:
        d = Path.cwd() / "Fretio_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _empresas_dir() -> Path:
    d = _fretio_appdata_dir() / "empresas"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _empresa_config_path(nome: str) -> Path:
    return _empresas_dir() / nome / "CONFIG.toml"


def _listar_empresas() -> list[str]:
    d = _empresas_dir()
    if not d.exists():
        return []
    return sorted(
        [p.name for p in d.iterdir() if p.is_dir() and (p / "CONFIG.toml").exists()],
        key=str.lower,
    )


def _ultima_empresa_path() -> Path:
    return _fretio_appdata_dir() / "ultima_empresa.txt"


def _ler_ultima_empresa() -> str:
    p = _ultima_empresa_path()
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""


def _salvar_ultima_empresa(nome: str) -> None:
    try:
        _ultima_empresa_path().write_text(nome, encoding="utf-8")
    except Exception:
        pass


def _toml_valor(v: Any) -> str:
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f"{v}"
    if isinstance(v, list):
        items = ", ".join(_toml_valor(x) for x in v)
        return f"[{items}]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _escrever_config_toml(config: dict[str, Any], path: Path) -> None:
    lines: list[str] = []
    for key, val in config.items():
        if not isinstance(val, dict):
            lines.append(f"{key} = {_toml_valor(val)}")
    if any(not isinstance(v, dict) for v in config.values()):
        lines.append("")

    for key, val in config.items():
        if key == "transportadoras" or not isinstance(val, dict):
            continue
        lines.append(f"[{key}]")
        for k, v in val.items():
            if not isinstance(v, dict):
                lines.append(f"{k} = {_toml_valor(v)}")
        lines.append("")

    transportadoras = config.get("transportadoras", {})
    if isinstance(transportadoras, dict):
        for nome, tcfg in transportadoras.items():
            if isinstance(tcfg, dict):
                lines.append(f"[transportadoras.{nome}]")
                for k, v in tcfg.items():
                    if not isinstance(v, dict):
                        lines.append(f"{k} = {_toml_valor(v)}")
                lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# Campos do desenvolvedor que nunca deveriam ter sido gravados na máquina do
# cliente (PAT de Gist embutido em builds antigos). Removidos no startup.
_DEVELOPER_CREDENTIAL_FIELDS = ("error_gist_id", "error_report_token")


def _scrub_developer_credentials_from_configs() -> None:
    """Remove credenciais do desenvolvedor de CONFIG.toml já gravados na máquina.

    Builds antigos embarcavam um token de Gist (error_report_token/error_gist_id)
    que acabava em texto puro nos CONFIG.toml do cliente. Esta varredura, idempotente
    e best-effort, apaga esses campos de todos os CONFIG.toml locais (Fretio e legado
    FreteBot, raiz e por empresa), preservando as credenciais das transportadoras
    (que são do próprio cliente). Blindada para nunca impedir a inicialização do app.
    """
    try:
        _run_credential_scrub()
    except Exception:
        pass


def _run_credential_scrub() -> None:
    appdata = os.getenv("APPDATA")
    if not appdata:
        return

    base = Path(appdata)
    candidates: list[Path] = []
    for root_name in ("Fretio", "FreteBot"):
        root = base / root_name
        candidates.append(root / "CONFIG.toml")
        empresas_dir = root / "empresas"
        if empresas_dir.is_dir():
            try:
                for emp_dir in sorted(empresas_dir.iterdir()):
                    if emp_dir.is_dir():
                        candidates.append(emp_dir / "CONFIG.toml")
            except Exception:
                pass

    def _load(path: Path) -> dict[str, Any]:
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except Exception:
            return {}
        try:
            import tomllib  # type: ignore[import]
            return tomllib.loads(raw)
        except Exception:
            pass
        try:
            import toml  # type: ignore[import-untyped]
            return toml.loads(raw)
        except Exception:
            return {}

    for cfg_path in candidates:
        if not cfg_path.is_file():
            continue
        try:
            data = _load(cfg_path)
            if not isinstance(data, dict):
                continue
            changed = False
            for section_name in ("fretio", "fretebot"):
                section = data.get(section_name)
                if not isinstance(section, dict):
                    continue
                for field in _DEVELOPER_CREDENTIAL_FIELDS:
                    if field in section:
                        del section[field]
                        changed = True
            if changed:
                _escrever_config_toml(data, cfg_path)
        except Exception:
            pass


def _garantir_defaults_fretio(config: dict[str, Any]) -> bool:
    changed = False
    source_sections: list[dict[str, Any]] = []
    for section_name in ("fretio", "fretebot"):
        section = config.get(section_name)
        if isinstance(section, dict):
            source_sections.append(section)

    fretio_cfg = config.get("fretio")
    if not isinstance(fretio_cfg, dict):
        fretio_cfg = {}
        config["fretio"] = fretio_cfg
        changed = True

    def _coletar_valor(key: str, fallback: str = "") -> str:
        for section in source_sections:
            value = str(section.get(key, "") or "").strip()
            if value:
                return value
        return fallback

    required_defaults = {
        "github_repo": _DEFAULT_GITHUB_REPO,
    }
    for key, fallback in required_defaults.items():
        current = str(fretio_cfg.get(key, "") or "").strip()
        if current:
            continue
        fretio_cfg[key] = _coletar_valor(key, fallback)
        changed = True

    return changed


def _garantir_defaults_empresa(config: dict[str, Any]) -> bool:
    """Garante campos novos da empresa sem sobrescrever valores existentes."""
    changed = False
    romaneio_cfg = config.get("romaneio")
    if not isinstance(romaneio_cfg, dict):
        romaneio_cfg = {}
        config["romaneio"] = romaneio_cfg
        changed = True
    if "cnpj_pagador_padrao" not in romaneio_cfg:
        romaneio_cfg["cnpj_pagador_padrao"] = ""
        changed = True
    return changed


def _criar_config_empresa_vazia(nome: str) -> None:
    config: dict[str, Any] = {
        "fretio": {
            "fator_cubagem": 6000,
            "cache_dir": "cache",
            "github_repo": _DEFAULT_GITHUB_REPO,
        },
        "romaneio": {"cep_origem": "", "cnpj_pagador_padrao": ""},
        "transportadoras": {
            "braspress": {"habilitado": False, "cnpj": "", "senha": "",
                          "ufs_atendidas": list(TODAS_UFS)},
            "trd": {"habilitado": False, "email": "", "senha": "", "headless": True,
                    "ufs_atendidas": ["RS", "SC", "PR", "SP", "MG", "ES", "RJ"]},
            "agex": {"habilitado": False, "email": "", "senha": "", "cnpj_remetente": "",
                     "cnpj_destinatario": "", "headless": True,
                     "ufs_atendidas": ["PR", "SP", "GO", "DF", "TO", "PA", "MT", "MS"]},
            "eucatur": {"habilitado": False, "dominio": "", "usuario": "", "senha": "",
                        "ufs_atendidas": ["RR", "AM", "AC", "RO", "MT", "MS"]},
            "rodonaves": {"habilitado": False, "dominio": "RTE", "usuario": "", "senha": "",
                          "cnpj_pagador": "", "login_url": "", "cotacao_url": "",
                          "headless": True, "ufs_atendidas": list(TODAS_UFS)},
            "alfa": {"habilitado": False, "login": "", "senha": "", "cnpj_remetente": "",
                     "login_url": "", "cotacao_url": "", "headless": False,
                     "ufs_atendidas": list(TODAS_UFS)},
            "coopex": {"habilitado": False, "dominio": "", "usuario": "", "senha": "",
                       "ufs_atendidas": []},
            "translovato": {"habilitado": False, "cnpj": "", "usuario": "", "senha": "",
                            "cnpj_remetente": "", "produto": "CONFECCAO", "cotacao_url": "",
                            "headless": True, "ufs_atendidas": list(TODAS_UFS)},
        },
    }
    _garantir_defaults_fretio(config)
    _garantir_defaults_empresa(config)
    _escrever_config_toml(config, _empresa_config_path(nome))


def _migrar_config_se_necessario() -> None:
    if _listar_empresas():
        return
    base = Path(__file__).resolve().parent
    candidatos = [
        base / "CONFIG.toml",
        base / "Fretio" / "CONFIG.toml",
    ]
    appdata = os.getenv("APPDATA")
    if appdata:
        candidatos.append(Path(appdata) / "Fretio" / "CONFIG.toml")
        candidatos.append(Path(appdata) / "FreteBot" / "CONFIG.toml")
    for c in candidatos:
        if c.exists():
            destino = _empresa_config_path("darlu")
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(c), str(destino))
            try:
                raw = destino.read_text(encoding="utf-8-sig")
                try:
                    import tomllib  # type: ignore[import]
                    data = tomllib.loads(raw)
                except Exception:
                    import toml  # type: ignore[import-untyped]
                    data = toml.loads(raw)
                if isinstance(data, dict):
                    changed = _garantir_defaults_fretio(data)
                    changed = _garantir_defaults_empresa(data) or changed
                    if changed:
                        _escrever_config_toml(data, destino)
            except Exception:
                pass
            _salvar_ultima_empresa("darlu")
            return
    _criar_config_empresa_vazia("default")
    _salvar_ultima_empresa("default")


def _renomear_pasta_empresa(nome_atual: str, nome_novo: str) -> bool:
    nome_novo = re.sub(r'[<>:"/\\|?*]', '_', nome_novo.strip())
    if not nome_novo or nome_novo == nome_atual:
        return False
    pasta_atual = _empresas_dir() / nome_atual
    pasta_nova = _empresas_dir() / nome_novo
    apenas_case = nome_atual.lower() == nome_novo.lower()
    if pasta_nova.exists() and not apenas_case:
        return False
    try:
        if apenas_case:
            tmp = pasta_atual.with_name(nome_atual + "_tmp_rename")
            pasta_atual.rename(tmp)
            tmp.rename(pasta_nova)
        else:
            pasta_atual.rename(pasta_nova)
        if _ler_ultima_empresa().lower() == nome_atual.lower():
            _salvar_ultima_empresa(nome_novo)
        return True
    except Exception:
        return False
