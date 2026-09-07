"""
Fretio Launcher — bootstrapper universal sem versão no nome.

Fluxo:
  1. Verifica %APPDATA%\\Fretio\\app\\ por instalação existente
  2. Consulta GitHub pela release mais recente
  3. Baixa e extrai o ZIP de atualização se necessário
  4. Lança Fretio.exe
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import PurePosixPath
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


def _ssl_context() -> ssl.SSLContext:
    """Contexto TLS com raízes do certifi quando disponível (espelha updater.py)."""
    try:
        import certifi  # type: ignore[import-untyped]

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    _HAS_TK = True
except ImportError:
    _HAS_TK = False

try:
    # Em runtime frozen o módulo vem no bundle (launcher.spec). O fallback mantém
    # o comportamento fail-closed: sem verificação disponível, nenhum ZIP é instalado.
    from update_security import UpdateSignatureError, verify_update_signature
except Exception:  # pragma: no cover
    class UpdateSignatureError(Exception):
        pass

    def verify_update_signature(zip_path, signature_path, public_key_b64=None):
        raise UpdateSignatureError("Verificação de assinatura indisponível no launcher.")

GITHUB_REPOS = (
    "kaianesteffens/RomaneioBeta",
)
_APPDATA_ROOT = Path(os.environ.get("APPDATA", Path.home()))
_LOCALAPPDATA_ROOT = Path(os.environ.get("LOCALAPPDATA", _APPDATA_ROOT))
_PROGRAMFILES_ROOTS = tuple(
    Path(p) for p in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
    )
    if p
)
APP_DIR_CANDIDATES = (
    _LOCALAPPDATA_ROOT / "Programs" / "Fretio",
    _LOCALAPPDATA_ROOT / "Programs" / "Romaneio Beta",
    _LOCALAPPDATA_ROOT / "Programs" / "FreteBot",
    *(
        root / name
        for root in _PROGRAMFILES_ROOTS
        for name in ("Fretio", "Romaneio Beta", "FreteBot")
    ),
    _APPDATA_ROOT / "Fretio" / "app",
    _APPDATA_ROOT / "FreteBot" / "app",
)
APP_EXE_NAMES = ("Fretio.exe", "FreteBot.exe")
HTTP_TIMEOUT = 20
PREFERRED_UPDATE_ASSET_NAMES = (
    "fretio-update-latest.zip",
    "fretebot-update-latest.zip",
    "romaneiobeta-update-latest.zip",
)
LOG_PATH = _APPDATA_ROOT / "Fretio" / "launcher.log"


# ── helpers ──────────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("fretio.launcher")
    if logger.handlers:
        return logger
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
            LOG_PATH.write_text("", encoding="utf-8")
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    except Exception:
        logger.addHandler(logging.NullHandler())
    return logger


_LOGGER = _setup_logger()


def _log(message: str, *args) -> None:
    try:
        _LOGGER.info(message, *args)
    except Exception:
        pass


def _log_exception(message: str) -> None:
    try:
        _LOGGER.error("%s\n%s", message, traceback.format_exc())
    except Exception:
        pass


def _has_version_file(app_dir: Path) -> bool:
    return (app_dir / "version.txt").exists() or (app_dir / "_internal" / "version.txt").exists()


def _is_valid_app_dir(app_dir: Path) -> bool:
    return any((app_dir / exe_name).exists() for exe_name in APP_EXE_NAMES) and _has_version_file(app_dir)

def _resolve_app_dir() -> Path:
    _log("APP_DIR_CANDIDATES verificados: %s", [str(p) for p in APP_DIR_CANDIDATES])
    for app_dir in APP_DIR_CANDIDATES:
        if _is_valid_app_dir(app_dir):
            _log("Instalação local válida encontrada: %s", app_dir)
            return app_dir
        _log(
            "Candidato inválido/ausente: %s | exe=%s | version=%s",
            app_dir,
            any((app_dir / exe_name).exists() for exe_name in APP_EXE_NAMES),
            _has_version_file(app_dir),
        )
    return APP_DIR_CANDIDATES[0]


def _resolve_app_exe(app_dir: Path) -> Path:
    for exe_name in APP_EXE_NAMES:
        candidate = app_dir / exe_name
        if candidate.exists():
            return candidate
    return app_dir / APP_EXE_NAMES[0]


def _validate_zip_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"ZIP inválido: caminho inseguro {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise ValueError(f"ZIP inválido: caminho absoluto {name!r}")
    return normalized


def _strip_single_root(names: list[str]) -> tuple[list[str], str | None]:
    file_names = [name for name in names if name and not name.endswith("/")]
    if not file_names:
        return names, None
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
    return names, None


def _validate_update_zip(zf: zipfile.ZipFile) -> str | None:
    names = [_validate_zip_member_name(info.filename) for info in zf.infolist()]
    stripped, root = _strip_single_root(names)
    files = {name.rstrip("/").lower() for name in stripped if name and not name.endswith("/")}
    has_exe = any(exe_name.lower() in files for exe_name in APP_EXE_NAMES)
    has_version = "version.txt" in files or "_internal/version.txt" in files
    if not has_exe:
        raise ValueError("ZIP de update inválido: Fretio.exe/FreteBot.exe não encontrado na raiz do pacote.")
    if not has_version:
        raise ValueError("ZIP de update inválido: version.txt ou _internal/version.txt não encontrado.")
    return root


def _safe_extract_zip_to_app(zip_path: Path, app_dir: Path) -> Path:
    app_dir = app_dir.resolve()
    app_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="_fretio_launcher_extract_", dir=str(app_dir.parent)))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            root = _validate_update_zip(zf)
            resolved_extract = temp_root.resolve()
            for member in zf.infolist():
                safe_name = _validate_zip_member_name(member.filename)
                member_path = (temp_root / safe_name).resolve()
                if os.path.commonpath([str(resolved_extract), str(member_path)]) != str(resolved_extract):
                    raise ValueError(f"Path traversal detectado no ZIP: {member.filename!r}")
            zf.extractall(temp_root)

        source_dir = temp_root / root if root else temp_root
        if root:
            _log("ZIP contém pasta raiz única %s; copiando conteúdo interno.", root)
        if not _is_valid_app_dir(source_dir):
            raise ValueError("ZIP extraído inválido: executável ou version.txt ausente após normalização.")

        app_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, app_dir, dirs_exist_ok=True)
        if not _is_valid_app_dir(app_dir):
            raise ValueError(f"Update incompleto em {app_dir}: executável ou version.txt ausente.")
        return app_dir
    finally:
        shutil.rmtree(str(temp_root), ignore_errors=True)


def _local_version(app_dir: Path) -> str | None:
    version_candidates = [
        app_dir / "version.txt",
        app_dir / "_internal" / "version.txt",
    ]
    for p in version_candidates:
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return None


def _parse_ver(tag: str) -> tuple[int, ...]:
    import re as _re
    tag = tag.lstrip("vV").strip()
    parts: list[int] = []
    for part in tag.split("."):
        match = _re.match(r"(\d+)", part)
        if not match:
            break
        parts.append(int(match.group(1)))
    return tuple(parts) if parts else (0,)


def _split_repo_candidates(raw) -> list[str]:
    if isinstance(raw, str):
        normalized = raw.replace(";", ",").replace("\n", ",")
        items = [item.strip() for item in normalized.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item).strip() for item in raw]
    else:
        return []
    return [item for item in items if item and "/" in item]


def _dedupe_repos(repos) -> list[str]:
    seen = set()
    ordered = []
    for repo in repos:
        lowered = repo.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(repo)
    return ordered


def _load_toml_candidates() -> list[Path]:
    app_dir = _resolve_app_dir()
    candidates = [
        _APPDATA_ROOT / "Fretio" / "CONFIG.toml",
        _APPDATA_ROOT / "FreteBot" / "CONFIG.toml",
        app_dir / "_internal" / "CONFIG.toml",
        app_dir / "CONFIG.toml",
        Path(sys.executable).resolve().parent / "CONFIG.toml",
    ]
    return candidates


def _load_repo_candidates() -> list[str]:
    repos = []
    for env_name in (
        "FRETIO_GITHUB_REPO",
        "FRETIO_GITHUB_REPO_ALIASES",
        "FRETEBOT_GITHUB_REPO",
        "FRETEBOT_GITHUB_REPO_ALIASES",
        "Fretio_GITHUB_REPO",
        "Fretio_GITHUB_REPO_ALIASES",
    ):
        repos.extend(_split_repo_candidates(os.environ.get(env_name, "")))

    for candidate in _load_toml_candidates():
        if not candidate.exists():
            continue
        try:
            raw = candidate.read_text(encoding="utf-8-sig")
            try:
                import tomllib
                cfg = tomllib.loads(raw)
            except ImportError:
                import toml
                cfg = toml.loads(raw)
        except Exception:
            continue
        for section_name in ("fretio", "fretebot", "romaneio"):
            section = cfg.get(section_name, {})
            if not isinstance(section, dict):
                continue
            repos.extend(_split_repo_candidates(section.get("github_repo", "")))
            repos.extend(_split_repo_candidates(section.get("github_repo_aliases", [])))

    repos.extend(GITHUB_REPOS)
    result = _dedupe_repos([repo for repo in repos if "/" in repo])
    _log("Repos de release candidatos: %s", result)
    return result


def _fetch_latest(repo: str) -> dict | None:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Fretio-Launcher/1.0",
    })
    with urlopen(req, timeout=HTTP_TIMEOUT, context=_ssl_context()) as resp:
        return json.loads(resp.read())


def _select_zip_asset(assets) -> dict | None:
    zip_assets = [asset for asset in assets if asset["name"].lower().endswith(".zip")]
    if not zip_assets:
        return None

    def _rank(asset):
        lowered = asset["name"].lower()
        if lowered in PREFERRED_UPDATE_ASSET_NAMES:
            return (0, PREFERRED_UPDATE_ASSET_NAMES.index(lowered), lowered)
        if "update" in lowered:
            return (1, 0, lowered)
        if "fretio" in lowered or "fretebot" in lowered or "romaneio" in lowered:
            return (2, 0, lowered)
        return (3, 0, lowered)

    return min(zip_assets, key=_rank)


def _select_signature_asset(assets, zip_asset) -> dict | None:
    if not zip_asset:
        return None
    target = f"{zip_asset.get('name', '')}.sig".lower()
    for asset in assets or []:
        if str(asset.get("name", "")).lower() == target:
            return asset
    return None


def _resolve_latest_release():
    for repo in _load_repo_candidates():
        try:
            release = _fetch_latest(repo)
        except Exception:
            _log_exception(f"Falha ao consultar release em {repo}")
            continue
        if release and release.get("tag_name"):
            _log("Release remota encontrada: repo=%s tag=%s", repo, release.get("tag_name"))
            return repo, release
    return None, None


def _download(url: str, dest: Path, total: int, status_cb, progress_cb) -> None:
    req = Request(url, headers={"User-Agent": "Fretio-Launcher/1.0"})
    downloaded = 0
    with urlopen(req, timeout=180, context=_ssl_context()) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = min(94, int(downloaded / total * 94))
                progress_cb(pct)
                status_cb(
                    f"Baixando... {downloaded/1048576:.1f} / {total/1048576:.1f} MB"
                )


def _launch_app(app_exe: Path) -> None:
    _log("Executável final escolhido: %s", app_exe)
    subprocess.Popen([str(app_exe)], cwd=str(app_exe.parent))


# ── UI ────────────────────────────────────────────────────────────────────────

if _HAS_TK:
    class _Window(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title("Fretio")
            self.geometry("400x148")
            self.resizable(False, False)
            self.configure(bg="#1e1e2e")

            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry(f"+{(sw - 400)//2}+{(sh - 148)//2}")

            self._lbl = tk.Label(
                self, text="Iniciando Fretio...",
                bg="#1e1e2e", fg="#cdd6f4", font=("Segoe UI", 11),
            )
            self._lbl.pack(pady=(22, 6))

            style = ttk.Style(self)
            style.theme_use("default")
            style.configure(
                "L.Horizontal.TProgressbar",
                troughcolor="#313244", background="#89b4fa",
            )
            self._pb = ttk.Progressbar(
                self, orient="horizontal", length=340,
                mode="indeterminate", style="L.Horizontal.TProgressbar",
            )
            self._pb.pack(pady=4)
            self._pb.start(10)

            self._sub = tk.Label(
                self, text="", bg="#1e1e2e", fg="#a6adc8",
                font=("Segoe UI", 8),
            )
            self._sub.pack(pady=6)

            self._closable = False
            self.protocol("WM_DELETE_WINDOW", self._guard_close)

        def _guard_close(self) -> None:
            if self._closable:
                self.destroy()

        def set_label(self, text: str) -> None:
            self.after(0, lambda: self._lbl.configure(text=text))

        def set_status(self, text: str) -> None:
            self.after(0, lambda: self._sub.configure(text=text))

        def set_progress(self, pct: float) -> None:
            def _apply():
                self._pb.stop()
                self._pb.configure(mode="determinate", value=pct, maximum=100)
            self.after(0, _apply)

        def close(self) -> None:
            self._closable = True
            self.after(0, self.destroy)

        def error(self, msg: str) -> None:
            self._closable = True
            self.after(0, lambda: messagebox.showerror("Romaneio – Erro", msg))
            self.after(200, self.destroy)


# ── core logic ────────────────────────────────────────────────────────────────

def _worker(win: "_Window | None") -> None:
    def label(t: str):
        if win:
            win.set_label(t)
        else:
            print(t)

    def status(t: str):
        if win:
            win.set_status(t)
        else:
            print(" ", t)

    def progress(p: float):
        if win:
            win.set_progress(p)

    try:
        _log("=" * 60)
        _log("Launcher iniciando")
        _log("Caminho do launcher: %s", Path(sys.executable).resolve())
        _log("APPDATA: %s", os.environ.get("APPDATA", ""))
        _log("LOCALAPPDATA: %s", os.environ.get("LOCALAPPDATA", ""))
        app_dir = _resolve_app_dir()
        app_exe = _resolve_app_exe(app_dir)
        local_ver = _local_version(app_dir)
        local_valid = _is_valid_app_dir(app_dir)
        _log("Versão local encontrada: %s", local_ver or "nenhuma")
        _log("Diretório local escolhido: %s | válido=%s", app_dir, local_valid)
        label("Verificando atualizações...")
        status(f"Versão instalada: {local_ver or 'nenhuma'}")

        try:
            release_repo, release = _resolve_latest_release()
        except Exception:
            _log_exception("Falha inesperada ao resolver release remota")
            release_repo, release = None, None

        needs_dl = True
        zip_asset = None
        remote_ver = None

        if release:
            tag = release.get("tag_name", "")
            remote_ver = tag.lstrip("vV").strip()
            _log("Repo de release usado: %s", release_repo or "")
            _log("Tag remota encontrada: %s", tag)
            if local_ver and local_valid:
                if _parse_ver(remote_ver) <= _parse_ver(local_ver):
                    needs_dl = False
            if needs_dl:
                zip_asset = _select_zip_asset(release.get("assets", []))
                _log(
                    "Asset ZIP escolhido: %s | tamanho=%s",
                    zip_asset.get("name") if zip_asset else "nenhum",
                    zip_asset.get("size") if zip_asset else "",
                )
                if zip_asset is None and local_valid:
                    _log("Release encontrada sem ZIP válido; abrindo versão local.")
                    needs_dl = False
        elif local_valid:
            _log("Sem release remota disponível; abrindo versão local válida.")
            needs_dl = False
        else:
            msg = (
                "Não foi possível conectar ao servidor e nenhuma versão local "
                "válida foi encontrada.\n\n"
                f"Verifique sua conexão com a internet.\n\nLog: {LOG_PATH}"
            )
            if win:
                win.error(msg)
            else:
                print("ERRO:", msg)
            return

        if needs_dl and zip_asset is None:
            raise FileNotFoundError(
                f"Release remota encontrada, mas nenhum asset ZIP de update foi localizado. Log: {LOG_PATH}"
            )

        installed = False
        if needs_dl and zip_asset:
            ver_str = remote_ver or "?"
            source_label = f" ({release_repo})" if release_repo else ""
            label(f"Baixando Fretio v{ver_str}{source_label}...")
            progress(0)

            temp_root = Path(os.environ.get("TEMP", app_dir.parent))
            tmp = temp_root / "_romaneio_launcher.zip"
            sig_tmp = temp_root / "_romaneio_launcher.zip.sig"
            _download(
                zip_asset["browser_download_url"],
                tmp,
                zip_asset.get("size", 0),
                status,
                progress,
            )
            _log("ZIP baixado: %s", tmp)

            # Verifica a assinatura Ed25519 ANTES de extrair (fail-closed): o
            # updater.py assinado não pode ser contornado por este caminho.
            label("Verificando assinatura...")
            try:
                sig_asset = _select_signature_asset(release.get("assets", []), zip_asset)
                if sig_asset is None:
                    raise UpdateSignatureError("Asset de assinatura (.sig) ausente na release.")
                _download(
                    sig_asset["browser_download_url"],
                    sig_tmp,
                    sig_asset.get("size", 0),
                    status,
                    progress,
                )
                verify_update_signature(tmp, sig_tmp)
                _log("Assinatura Ed25519 do update verificada.")
            except Exception:
                _log_exception("Falha na verificação de assinatura do update; instalação abortada")
                for _p in (tmp, sig_tmp):
                    try:
                        _p.unlink()
                    except OSError:
                        pass
                if not local_valid:
                    raise UpdateSignatureError(
                        f"Não foi possível verificar a assinatura da atualização e não há versão local válida. Log: {LOG_PATH}"
                    )
                _log("Update rejeitado pela assinatura; abrindo versão local válida.")
            else:
                label("Instalando...")
                status("Extraindo arquivos...")
                progress(96)

                _safe_extract_zip_to_app(tmp, app_dir)
                _log("Caminho final de extração: %s", app_dir)

                for _p in (tmp, sig_tmp):
                    try:
                        _p.unlink()
                    except OSError:
                        pass

                app_exe = _resolve_app_exe(app_dir)
                if not _is_valid_app_dir(app_dir):
                    raise FileNotFoundError(
                        f"Update instalado sem executável/version.txt válido em {app_dir}"
                    )
                progress(100)
                status(f"v{ver_str} instalado com sucesso!")
                installed = True

        if not installed:
            label("Fretio está atualizado!")
            status(f"Versão {local_ver} — abrindo...")

        time.sleep(0.7)
        label("Abrindo aplicativo...")

        if not app_exe.exists():
            raise FileNotFoundError(
                f"Fretio.exe/FreteBot.exe não encontrado em {app_dir}. Log: {LOG_PATH}"
            )
        if not _has_version_file(app_dir):
            raise FileNotFoundError(
                f"version.txt/_internal/version.txt não encontrado em {app_dir}. Log: {LOG_PATH}"
            )

        _launch_app(app_exe)

        if win:
            win.close()

    except Exception as exc:
        _log_exception("Erro fatal no launcher")
        msg = f"{exc}\n\nLog: {LOG_PATH}"
        if win:
            win.error(msg)
        else:
            print("ERRO:", msg)
            input("Pressione Enter para sair...")


def main() -> None:
    if _HAS_TK:
        win = _Window()
        threading.Thread(target=_worker, args=(win,), daemon=True).start()
        win.mainloop()
    else:
        _worker(None)


if __name__ == "__main__":
    main()
