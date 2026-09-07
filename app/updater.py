"""
Fretio Auto-Updater via GitHub Releases.

Verifica se há nova versão disponível no GitHub e atualiza os arquivos
do aplicativo automaticamente, sem precisar gerar um novo instalador.

Uso:
    from updater import check_for_update, apply_update

    info = check_for_update("owner/repo", "1.21")
    if info:
        apply_update(info, callback=print)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import traceback
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from update_security import verify_update_signature


def _safe_bat_version(version: Any) -> str:
    # A versão vem da tag do GitHub (metadado NÃO assinado) e é interpolada no
    # _apply_update.bat; restringe a caracteres seguros para barrar metacaracteres
    # de batch (&, >, |, %). A assinatura Ed25519 valida só o conteúdo do ZIP.
    return re.sub(r"[^0-9A-Za-z._-]", "", str(version or "")) or "desconhecida"


# Timeout para requisições HTTP (segundos)
_HTTP_TIMEOUT = 30
# Releases sao publicadas e lidas exclusivamente no repositorio principal.
# A dependencia do antigo repo de releases foi removida (unificacao de releases).
_DEFAULT_GITHUB_REPOS = (
    "kaianesteffens/RomaneioBeta",
)
_GITHUB_REPO_ENV_VARS = (
    "FRETIO_GITHUB_REPO",
    "FRETEBOT_GITHUB_REPO",
    "Fretio_GITHUB_REPO",
)
_GITHUB_REPO_ALIAS_ENV_VARS = (
    "FRETIO_GITHUB_REPO_ALIASES",
    "FRETEBOT_GITHUB_REPO_ALIASES",
    "Fretio_GITHUB_REPO_ALIASES",
)
_GITHUB_REPO_CONFIG_SECTIONS = ("fretio", "fretebot", "romaneio")
_PREFERRED_UPDATE_ASSET_NAMES = (
    "fretio-update-latest.zip",
    "fretebot-update-latest.zip",
    "romaneiobeta-update-latest.zip",
)
_APP_EXE_NAMES = ("Fretio.exe", "FreteBot.exe")


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore[import-untyped]

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _log_path() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "Fretio" / "updater.log"
    return Path.home() / ".Fretio" / "updater.log"


def _setup_diag_logger() -> logging.Logger:
    logger = logging.getLogger("fretio.updater")
    if logger.handlers:
        return logger
    try:
        log_file = _log_path()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if log_file.exists() and log_file.stat().st_size > 2 * 1024 * 1024:
            log_file.write_text("", encoding="utf-8")
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    except Exception:
        logger.addHandler(logging.NullHandler())
    return logger


_LOGGER = _setup_diag_logger()


def _log(message: str, *args: Any) -> None:
    try:
        _LOGGER.info(message, *args)
    except Exception:
        pass


def _log_exception(message: str) -> None:
    try:
        _LOGGER.error("%s\n%s", message, traceback.format_exc())
    except Exception:
        pass


@dataclass
class UpdateInfo:
    """Dados de uma atualização disponível."""
    tag: str                  # Ex: "1.22"
    version: str              # Ex: "1.22"
    download_url: str         # URL do asset ZIP
    asset_name: str           # Nome do arquivo ZIP
    asset_size: int           # Bytes
    release_notes: str        # Corpo da release (Markdown)
    html_url: str             # URL da release no GitHub
    source_repo: str = ""     # Repositório usado para resolver a release
    signature_download_url: str = ""  # URL do asset .sig
    signature_asset_name: str = ""    # Nome do asset .sig
    mandatory: bool = False   # Se true, o app deve bloquear uso ate atualizar
    source: str = "github"    # Origem da descoberta: github/server


def _load_toml_file(path: Path) -> dict[str, Any]:
    """Carrega TOML aceitando UTF-8 com/sem BOM."""
    raw = path.read_text(encoding="utf-8-sig")
    data = None
    try:
        import tomllib  # type: ignore[import]
        data = tomllib.loads(raw)
    except ImportError:
        pass
    if data is None:
        try:
            import toml  # type: ignore[import-untyped]
            data = toml.loads(raw)
        except ImportError:
            pass
    if data is None:
        import tomli  # type: ignore[import-not-found]
        data = tomli.loads(raw)
    return data if isinstance(data, dict) else {}


def _require_web_url(url: str) -> None:
    """Rejeita esquemas não-web (file://, ftp://, data:...) antes de urlopen."""
    scheme = urlparse(str(url or "")).scheme.lower()
    if scheme not in ("http", "https"):
        raise URLError(f"Esquema de URL não permitido: {scheme or 'vazio'}")


def _github_api(url: str) -> Any:
    """Faz GET na API do GitHub e retorna JSON."""
    _require_web_url(url)
    req = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Fretio-Updater/1.0",
    })
    with urlopen(req, timeout=_HTTP_TIMEOUT, context=_ssl_context()) as resp:
        return json.loads(resp.read())


def _parse_version(tag: str) -> tuple[int, ...]:
    """Converte '1.22', 'v1.22' ou '1.22-beta' em (1, 22) para comparação."""
    import re as _re
    tag = tag.lstrip("vV").strip()
    parts: list[int] = []
    for p in tag.split("."):
        m = _re.match(r"(\d+)", p)
        if m:
            parts.append(int(m.group(1)))
        else:
            break
    return tuple(parts) if parts else (0,)


def _validate_zip_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"ZIP inválido: caminho inseguro {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise ValueError(f"ZIP inválido: caminho absoluto {name!r}")
    return normalized


def _strip_single_root(names: Sequence[str]) -> tuple[list[str], str | None]:
    file_names = [name for name in names if name and not name.endswith("/")]
    if not file_names:
        return list(names), None
    split_names = [name.split("/") for name in file_names]
    first_parts = {parts[0] for parts in split_names if parts}
    has_root_file = any(len(parts) == 1 for parts in split_names)
    if len(first_parts) == 1 and not has_root_file:
        root = next(iter(first_parts))
        stripped = [
            name[len(root) + 1:] if name == root or name.startswith(root + "/") else name
            for name in names
        ]
        return stripped, root
    return list(names), None


def _validate_update_zip(zf: zipfile.ZipFile) -> str | None:
    names = [_validate_zip_member_name(info.filename) for info in zf.infolist()]
    stripped, root = _strip_single_root(names)
    files = {name.rstrip("/").lower() for name in stripped if name and not name.endswith("/")}
    has_exe = any(exe_name.lower() in files for exe_name in _APP_EXE_NAMES)
    has_version = "version.txt" in files or "_internal/version.txt" in files
    if not has_exe:
        raise ValueError("ZIP de update inválido: Fretio.exe/FreteBot.exe não encontrado na raiz do pacote.")
    if not has_version:
        raise ValueError("ZIP de update inválido: version.txt ou _internal/version.txt não encontrado.")
    return root


def _has_valid_update_structure(source_dir: Path) -> bool:
    return (
        any((source_dir / exe_name).exists() for exe_name in _APP_EXE_NAMES)
        and ((source_dir / "version.txt").exists() or (source_dir / "_internal" / "version.txt").exists())
    )


def _resolve_extracted_source_dir(extract_dir: Path) -> Path:
    contents = [p for p in extract_dir.iterdir() if p.name != "__MACOSX"]
    if len(contents) == 1 and contents[0].is_dir():
        return contents[0]
    return extract_dir


def _safe_extract_update_zip(zip_path: Path, extract_dir: Path) -> Path:
    with zipfile.ZipFile(zip_path, "r") as zf:
        root = _validate_update_zip(zf)
        resolved_extract = extract_dir.resolve()
        for member in zf.infolist():
            safe_name = _validate_zip_member_name(member.filename)
            member_path = (extract_dir / safe_name).resolve()
            if os.path.commonpath([str(resolved_extract), str(member_path)]) != str(resolved_extract):
                raise ValueError(f"Path traversal detectado no ZIP: {member.filename!r}")
        zf.extractall(extract_dir)

    source_dir = extract_dir / root if root else _resolve_extracted_source_dir(extract_dir)
    if not _has_valid_update_structure(source_dir):
        raise ValueError(
            "ZIP de update inválido: executável e version.txt/_internal/version.txt são obrigatórios."
        )
    return source_dir


def _split_repo_candidates(raw: Any) -> list[str]:
    if isinstance(raw, str):
        normalized = raw.replace(";", ",").replace("\n", ",")
        items = [item.strip() for item in normalized.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item).strip() for item in raw]
    else:
        return []
    return [item for item in items if item and "/" in item]


def _dedupe_repos(repos: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for repo in repos:
        normalized = repo.strip()
        if not normalized or "/" not in normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(normalized)
    return ordered


def _select_update_asset(assets: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    zip_assets = [
        asset for asset in assets
        if str(asset.get("name", "")).lower().endswith(".zip")
    ]
    if not zip_assets:
        return None

    def _asset_rank(asset: dict[str, Any]) -> tuple[int, int, str]:
        name = str(asset.get("name", "")).strip()
        lowered = name.lower()
        if lowered in _PREFERRED_UPDATE_ASSET_NAMES:
            return (0, _PREFERRED_UPDATE_ASSET_NAMES.index(lowered), lowered)
        if "update" in lowered:
            return (1, 0, lowered)
        if "fretio" in lowered or "fretebot" in lowered or "romaneio" in lowered:
            return (2, 0, lowered)
        return (3, 0, lowered)

    selected = min(zip_assets, key=_asset_rank)
    _log(
        "Asset ZIP escolhido: %s | tamanho=%s",
        selected.get("name", ""),
        selected.get("size", 0),
    )
    return selected


def _select_signature_asset(
    assets: Sequence[dict[str, Any]],
    zip_asset_name: str,
) -> dict[str, Any] | None:
    expected_name = f"{zip_asset_name}.sig".lower()
    for asset in assets:
        if str(asset.get("name", "")).strip().lower() == expected_name:
            return asset
    return None


def get_repo_candidates_from_config() -> list[str]:
    repos: list[str] = []

    for env_name in _GITHUB_REPO_ENV_VARS:
        repos.extend(_split_repo_candidates(os.environ.get(env_name, "")))
    for env_name in _GITHUB_REPO_ALIAS_ENV_VARS:
        repos.extend(_split_repo_candidates(os.environ.get(env_name, "")))

    try:
        config_paths: list[Path] = []
        appdata = os.getenv("APPDATA")
        if appdata:
            config_paths.append(Path(appdata) / "Fretio" / "CONFIG.toml")
            config_paths.append(Path(appdata) / "FreteBot" / "CONFIG.toml")
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        config_paths.append(base / "CONFIG.toml")
        if base != Path(__file__).parent:
            config_paths.append(Path(__file__).parent / "CONFIG.toml")

        for cp in config_paths:
            if not cp.exists():
                continue
            cfg = _load_toml_file(cp)
            for section_name in _GITHUB_REPO_CONFIG_SECTIONS:
                section = cfg.get(section_name, {})
                if not isinstance(section, dict):
                    continue
                repos.extend(_split_repo_candidates(section.get("github_repo", "")))
                repos.extend(_split_repo_candidates(section.get("github_repo_aliases", [])))
    except Exception:
        pass

    repos.extend(_DEFAULT_GITHUB_REPOS)
    return _dedupe_repos(repos)


def get_repo_from_config() -> str:
    """Lê o primeiro repositório GitHub configurado para auto-update."""
    candidates = get_repo_candidates_from_config()
    return candidates[0] if candidates else _DEFAULT_GITHUB_REPOS[0]


def _resolve_repo_candidates(repo: str | Sequence[str] | None) -> list[str]:
    repos: list[str] = []
    if repo:
        repos.extend(_split_repo_candidates(repo))
    repos.extend(get_repo_candidates_from_config())
    return _dedupe_repos(repos)


def check_for_update(
    repo: str | Sequence[str] | None,
    current_version: str,
) -> Optional[UpdateInfo]:
    """
    Verifica se há uma versão mais nova nas GitHub Releases.

    Args:
        repo: "owner/repo" ou lista de aliases para fallback GitHub
        current_version: versão atual (ex: "1.21")

    Returns:
        UpdateInfo se houver atualização, None caso contrário.
    """
    repo_candidates = _resolve_repo_candidates(repo)
    _log("=" * 60)
    _log("Verificando atualização | versão atual=%s", current_version)
    _log("Repos candidatos: %s", repo_candidates)
    if not repo_candidates:
        return None

    local_ver = _parse_version(current_version)

    for repo_name in repo_candidates:
        try:
            url = f"https://api.github.com/repos/{repo_name}/releases/latest"
            data = _github_api(url)
        except (URLError, OSError, json.JSONDecodeError):
            _log_exception(f"Falha ao consultar release em {repo_name}")
            continue

        tag = data.get("tag_name", "")
        if not tag:
            _log("Release ignorada sem tag: repo=%s", repo_name)
            continue

        remote_ver = _parse_version(tag)
        if remote_ver <= local_ver:
            _log("Release encontrada sem update: repo=%s tag=%s", repo_name, tag)
            continue

        _log("Release encontrada: repo=%s tag=%s html=%s", repo_name, tag, data.get("html_url", ""))
        assets = data.get("assets", [])
        zip_asset = _select_update_asset(assets)
        if not zip_asset:
            _log("Release sem asset ZIP válido: repo=%s tag=%s", repo_name, tag)
            continue
        signature_asset = _select_signature_asset(assets, str(zip_asset.get("name", "")))

        return UpdateInfo(
            tag=tag,
            version=tag.lstrip("vV").strip(),
            download_url=zip_asset["browser_download_url"],
            asset_name=zip_asset["name"],
            asset_size=zip_asset.get("size", 0),
            release_notes=data.get("body", "") or "",
            html_url=data.get("html_url", ""),
            source_repo=repo_name,
            signature_download_url=(signature_asset or {}).get("browser_download_url", ""),
            signature_asset_name=(signature_asset or {}).get("name", ""),
            source="github",
        )
    return None


def _get_app_dir() -> Path:
    """Retorna o diretório raiz do app (onde está o exe ou script)."""
    if getattr(sys, "frozen", False):
        # PyInstaller one-folder: exe está em dist/Fretio/Fretio.exe
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _download_with_progress(
    url: str,
    dest: Path,
    total_size: int = 0,
    callback: Optional[Callable[[str], None]] = None,
) -> None:
    """Baixa arquivo com progresso."""
    _require_web_url(url)
    req = Request(url, headers={"User-Agent": "Fretio-Updater/1.0"})
    with urlopen(req, timeout=120, context=_ssl_context()) as resp:
        downloaded = 0
        chunk_size = 64 * 1024  # 64 KB
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if callback and total_size > 0:
                    pct = min(100, int(downloaded / total_size * 100))
                    callback(f"Baixando atualização... {pct}%")


def _signature_download_url(info: UpdateInfo) -> str:
    if info.signature_download_url:
        return info.signature_download_url
    if info.download_url:
        return f"{info.download_url}.sig"
    return ""


def _signature_asset_name(info: UpdateInfo) -> str:
    if info.signature_asset_name:
        return info.signature_asset_name
    if info.asset_name:
        return f"{info.asset_name}.sig"
    return "update.zip.sig"


def _download_update_signature(info: UpdateInfo, dest: Path) -> None:
    signature_url = _signature_download_url(info)
    if not signature_url:
        raise ValueError("Assinatura do update ausente.")
    _download_with_progress(signature_url, dest, 0, None)


def apply_update(
    info: UpdateInfo,
    callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Baixa e aplica a atualização.

    Estratégia para Windows: como o exe e DLLs estão em uso e não podem
    ser sobrescritos diretamente, o updater:
    1. Baixa e extrai o ZIP para uma pasta temporária em %APPDATA%
    2. Cria um script .bat que:
       - Aguarda o app fechar
       - Copia os novos arquivos por cima dos antigos
       - Reinicia o app
    3. O app fecha e o script .bat assume

    Args:
        info: informações da atualização (de check_for_update)
        callback: função para reportar progresso

    Returns:
        True se a atualização foi preparada com sucesso.
    """
    app_dir = _get_app_dir()
    _log("=" * 60)
    _log("Preparando update | versão=%s | asset=%s | repo=%s", info.version, info.asset_name, info.source_repo)
    _log("Asset tamanho: %s", info.asset_size)
    _log("app_dir: %s", app_dir)

    if callback:
        callback(f"Baixando v{info.version}...")

    # Pasta persistente para o update (não temp, senão é apagada)
    update_dir = _license_dir_update()
    update_dir.mkdir(parents=True, exist_ok=True)

    zip_path = update_dir / info.asset_name
    signature_path = update_dir / _signature_asset_name(info)
    _log("Caminho do ZIP baixado: %s", zip_path)

    try:
        _download_with_progress(info.download_url, zip_path, info.asset_size, callback)
        _download_update_signature(info, signature_path)
        verify_update_signature(zip_path, signature_path)

        if callback:
            callback("Extraindo atualização...")

        # Extrai para subpasta
        extract_dir = update_dir / "extracted"
        if extract_dir.exists():
            shutil.rmtree(str(extract_dir), ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)
        _log("Diretório extraído: %s", extract_dir)

        source_dir = _safe_extract_update_zip(zip_path, extract_dir)
        _log("Diretório fonte validado: %s", source_dir)

        if callback:
            callback("Preparando atualização...")

        # Cria script batch que faz a substituição após o app fechar
        bat_path = update_dir / "_apply_update.bat"
        _log("Arquivo BAT gerado: %s", bat_path)
        app_exe = Path(sys.executable) if getattr(sys, "frozen", False) else None
        pid = os.getpid()

        safe_version = _safe_bat_version(getattr(info, "version", ""))

        # Backup do app instalado para rollback caso o xcopy falhe no meio.
        # O version.txt vem do pacote ASSINADO (copiado pelo xcopy), então não é
        # reescrito com o valor da tag (que não passa por verificação de assinatura).
        backup_dir = update_dir / "backup"
        restart_line = f'start "" "{app_exe}"\n' if app_exe else ""
        bat_content = f'''@echo off
chcp 65001 >nul 2>&1
title Fretio - Atualizando...
echo Aguardando Fretio fechar...

:wait_loop
tasklist /FI "PID eq {pid}" 2>nul | find /I "{pid}" >nul
if %ERRORLEVEL% == 0 (
    timeout /t 1 /nobreak >nul
    goto wait_loop
)

echo Fretio fechou. Aplicando atualizacao v{safe_version}...
timeout /t 2 /nobreak >nul

if exist "{backup_dir}" rmdir /S /Q "{backup_dir}" >nul 2>&1
xcopy /E /Y /I /Q "{app_dir}" "{backup_dir}" >nul 2>&1

xcopy /E /Y /I /Q "{source_dir}" "{app_dir}" >nul 2>&1
if errorlevel 1 goto rollback

echo Atualizacao concluida! Reiniciando...
timeout /t 1 /nobreak >nul

del /Q "{zip_path}" >nul 2>&1
rmdir /S /Q "{extract_dir}" >nul 2>&1
rmdir /S /Q "{backup_dir}" >nul 2>&1

{restart_line}del "%~f0" >nul 2>&1
exit

:rollback
echo Falha ao aplicar atualizacao. Restaurando versao anterior...
xcopy /E /Y /I /Q "{backup_dir}" "{app_dir}" >nul 2>&1
del /Q "{zip_path}" >nul 2>&1
rmdir /S /Q "{extract_dir}" >nul 2>&1
rmdir /S /Q "{backup_dir}" >nul 2>&1
{restart_line}del "%~f0" >nul 2>&1
exit
'''

        bat_encoding = "mbcs" if os.name == "nt" else "utf-8"
        bat_path.write_text(bat_content, encoding=bat_encoding)

        # Marca que há uma atualização pendente
        pending_file = update_dir / "_pending_update"
        pending_file.write_text(str(bat_path), encoding="utf-8")

        if callback:
            callback(f"Atualização v{info.version} preparada! Reiniciando...")

        return True

    except Exception as e:
        _log_exception("Falha ao preparar atualização")
        if callback:
            callback(f"Erro na atualização: {e}. Log: {_log_path()}")
        # Limpa em caso de erro
        try:
            if zip_path.exists():
                zip_path.unlink()
        except Exception:
            pass
        try:
            if signature_path.exists():
                signature_path.unlink()
        except Exception:
            pass
        return False


def _license_dir_update() -> Path:
    """Diretório para armazenar updates temporários."""
    appdata = os.getenv("APPDATA")
    if appdata:
        d = Path(appdata) / "Fretio" / "update"
    else:
        d = Path.home() / ".Fretio" / "update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def needs_restart() -> bool:
    """Verifica se há uma atualização pendente que precisa de restart."""
    update_dir = _license_dir_update()
    return (update_dir / "_pending_update").exists()


def restart_app() -> None:
    """
    Reinicia o aplicativo executando o script de atualização.
    O script aguarda o app fechar, copia os novos arquivos e reinicia.
    """
    update_dir = _license_dir_update()
    pending_file = update_dir / "_pending_update"
    # O alvo executável é SEMPRE o BAT determinístico gerado por apply_update
    # dentro de update_dir. Não executamos o caminho gravado em _pending_update:
    # esse arquivo fica no perfil do usuário e poderia ser adulterado por outro
    # processo para apontar a um binário arbitrário (CWE-494).
    expected_bat = (update_dir / "_apply_update.bat").resolve()

    if pending_file.exists():
        if expected_bat.exists():
            # Lança o script batch em processo separado (não é filho do app)
            # CREATE_NO_WINDOW: cria console oculto para cmd.exe rodar o .bat
            # CREATE_NEW_PROCESS_GROUP: garante que o processo sobrevive ao pai
            # NÃO usar DETACHED_PROCESS junto, pois bloqueia o console do cmd
            subprocess.Popen(
                ["cmd", "/c", str(expected_bat)],
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                close_fds=True,
            )
        else:
            _log("restart_app: _apply_update.bat ausente em %s; restart cancelado.", update_dir)
        pending_file.unlink(missing_ok=True)

    # Fecha o app
    sys.exit(0)
