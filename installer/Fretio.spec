# -*- mode: python ; coding: utf-8 -*-
"""
Fretio — PyInstaller spec (one-folder mode)
Gera: dist/Fretio/ com Fretio.exe + dependências
"""

from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

project_root = Path(SPECPATH).parent / "app"  # Diretório do app com web_app.py

# ── Hidden imports (módulos que PyInstaller não detecta automaticamente) ───
hiddenimports = [
    # Providers
    "fretio.providers.braspress_playwright",
    "fretio.providers.trd",
    "fretio.providers.agex",
    "fretio.providers.eucatur",
    "fretio.providers.rodonaves",
    "fretio.providers.alfa",
    "fretio.providers.coopex",
    "fretio.providers.translovato",
    "fretio.providers._win_taskbar",
    "fretio.providers.base",
    "fretio.providers.factory",
    "fretio.providers.provider_utils",
    "fretio.providers.rodonaves_browser",
    "fretio.providers.rodonaves_diagnostics",
    "fretio.providers.trd_browser",
    "fretio.providers.trd_diagnostics",
    "fretio.providers.alfa_browser",
    "fretio.providers.agex_browser",
    "fretio.providers.agex_diagnostics",
    "fretio.providers.translovato_browser",
    "fretio.providers.braspress_browser",
    # Fretio core
    "fretio.models",
    "fretio.logging_conf",
    "fretio.config_manager",
    # Dependências externas
    "bs4",
    "certifi",
    "httpx",
    "httpx._transports",
    "httpx._transports.default",
    "httpcore",
    "pdfplumber",
    "toml",
    "tomli",
    "playwright",
    "playwright.async_api",
    "playwright.sync_api",
    # UI web (pywebview + backend .NET WebView2 via pythonnet)
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
    "bottle",
    "proxy_tools",
    "PIL",
    "cryptography",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    # Módulos locais na raiz
    "extrator_pedidos",
    "extrator_pedidos_common",
    "extrator_pedidos_parse",
    "extrator_pedidos_local",
    "extrator_pedidos_boxes",
    "cotacao_transportadoras",
    "cotacao.orchestrator_builders",
    "cotacao.memory_budget",
    "updater",
    "update_security",
    "remote_permissions",
    "error_reporter",
    "error_handler",
    "logging_conf",
    "extrator_nfe",
    "extrator_nfe_info",
    "rastreamento",
    "rastreamento_common",
    "rastreamento_captura",
    "rastreamento_status",
    "web_app_shared",
    "web_app_config",
    "web_app_startup",
    "web_app_rastreio",
    "web_app_cotacao",
    "web_app_romaneio",
    "web_app",
    "app_bootstrap",
    "startup",
]
hiddenimports += collect_submodules("webview")

# ── Data files ────────────────────────────────────────────────────────────
datas = []
datas += collect_data_files("certifi")
# pywebview: arquivos de bridge JS + libs nativas do backend WebView2
datas += collect_data_files("webview")

# UI web (HTML/CSS/JS) — vão para web/ no bundle one-folder
web_dir = project_root / "web"
if web_dir.is_dir():
    for _f in web_dir.rglob("*"):
        if _f.is_file():
            datas.append((str(_f), str(_f.parent.relative_to(project_root))))

# CONFIG.example.toml (template para o usuário)
config_example = project_root / "fretio" / "CONFIG.example.toml"
if config_example.exists():
    datas.append((str(config_example), "fretio"))

config_root = project_root / "CONFIG.example.toml"
if config_root.exists():
    datas.append((str(config_root), "."))

# IMPORTANTE: nunca embarcar um CONFIG.toml real no binário. Ele acabaria em texto
# puro na máquina do cliente. Só o template CONFIG.example.toml é distribuído; o app
# cria a config por empresa no primeiro uso (company_config) com as URLs padrão.

# Romaneio exemplo
romaneio_ex = project_root / "romaneio_exemplo.csv"
if romaneio_ex.exists():
    datas.append((str(romaneio_ex), "."))

# Versão exibida na interface (Fretio X.Y)
version_file = project_root / "version.txt"
if version_file.exists():
    datas.append((str(version_file), "."))

# Assets da interface (ícones)
assets_dir = project_root / "assets"
for asset_name in ("romaneio.ico", "fretio.ico"):
    asset_path = assets_dir / asset_name
    if asset_path.exists():
        datas.append((str(asset_path), "assets"))

# Logos das transportadoras
logos_dir = assets_dir / "logos"
if logos_dir.is_dir():
    for logo_file in logos_dir.iterdir():
        if logo_file.is_file():
            datas.append((str(logo_file), "assets/logos"))

# Fontes locais da UI
fonts_dir = assets_dir / "fonts"
if fonts_dir.is_dir():
    for font_file in fonts_dir.iterdir():
        if font_file.is_file():
            datas.append((str(font_file), "assets/fonts"))

# ── Binaries (DLLs nativas do WebView2/pythonnet) ─────────────────────────
binaries = collect_dynamic_libs("webview")

# ── Analysis ──────────────────────────────────────────────────────────────
a = Analysis(
    [str(project_root / "web_app.py")],
    pathex=[
        str(project_root),
        str(project_root / "fretio" / "src"),
    ],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "IPython",
        "jupyter",
        "pytest",
        "unittest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ── EXE (one-folder) ─────────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # one-folder mode
    name="Fretio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # GUI app, sem console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project_root / "assets" / "romaneio.ico")
        if (project_root / "assets" / "romaneio.ico").exists()
        else (str(project_root / "assets" / "fretio.ico")
              if (project_root / "assets" / "fretio.ico").exists() else None),
)

# ── COLLECT (junta tudo numa pasta) ───────────────────────────────────────
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Fretio",
)
