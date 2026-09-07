import sys
from pathlib import Path
from io import BytesIO
from urllib.error import HTTPError
import zipfile


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "installer"))

import launcher
import updater
import validate_update_zip


def _make_zip(path: Path, entries: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return path


def test_updater_collects_repo_aliases_from_config_and_env(monkeypatch, tmp_path):
    appdata = tmp_path / "appdata"
    config_dir = appdata / "Fretio"
    config_dir.mkdir(parents=True)
    (config_dir / "CONFIG.toml").write_text(
        (
            "[fretio]\n"
            'github_repo = "owner/primary"\n'
            'github_repo_aliases = ["owner/legacy", "owner/fallback"]\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("FRETIO_GITHUB_REPO_ALIASES", "env/one, env/two")

    candidates = updater.get_repo_candidates_from_config()

    assert candidates[:5] == [
        "env/one",
        "env/two",
        "owner/primary",
        "owner/legacy",
        "owner/fallback",
    ]
    # Releases sao publicadas e lidas exclusivamente no repositorio principal;
    # a dependencia do antigo repo de releases foi removida.
    assert "kaianesteffens/RomaneioBeta" in candidates
    assert "kaianesteffens/RomaneioBeta-releases" not in candidates


def test_updater_tries_repo_aliases_and_prefers_stable_latest_asset(monkeypatch):
    calls = []
    responses = {
        "https://api.github.com/repos/owner/primary/releases/latest": OSError("offline"),
        "https://api.github.com/repos/owner/fallback/releases/latest": {
            "tag_name": "v2.0.0",
            "body": "notes",
            "html_url": "https://example.test/release",
            "assets": [
                {"name": "Random.zip", "browser_download_url": "https://example.test/random.zip", "size": 1},
                {"name": "Fretio-Update-latest.zip", "browser_download_url": "https://example.test/latest.zip", "size": 2},
            ],
        },
    }

    def fake_api(url):
        calls.append(url)
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(updater, "_github_api", fake_api)
    monkeypatch.setattr(updater, "get_repo_candidates_from_config", lambda: ["owner/fallback"])

    info = updater.check_for_update(["owner/primary", "owner/fallback"], "1.0.0")

    assert info is not None
    assert info.source_repo == "owner/fallback"
    assert info.asset_name == "Fretio-Update-latest.zip"
    assert calls == [
        "https://api.github.com/repos/owner/primary/releases/latest",
        "https://api.github.com/repos/owner/fallback/releases/latest",
    ]


def test_launcher_prefers_stable_latest_zip_alias():
    asset = launcher._select_zip_asset(
        [
            {"name": "Fretio-Update-2.0.zip"},
            {"name": "Fretio-Update-latest.zip"},
        ]
    )

    assert asset is not None
    assert asset["name"] == "Fretio-Update-latest.zip"


def test_version_parsing_accepts_v_prefix_and_suffix():
    assert updater._parse_version("v1.22-beta") == (1, 22)
    assert launcher._parse_ver("V2.0.1") == (2, 0, 1)
    assert updater._parse_version("sem-versao") == (0,)


def test_update_zip_validator_accepts_expected_root_format(tmp_path):
    zip_path = _make_zip(
        tmp_path / "Fretio-Update-1.0.zip",
        {
            "Fretio.exe": "",
            "version.txt": "1.0",
            "_internal/version.txt": "1.0",
            "_internal/lib.dll": "",
        },
    )

    validate_update_zip.validate(zip_path)


def test_launcher_safe_extract_supports_single_root_folder(tmp_path):
    zip_path = _make_zip(
        tmp_path / "update.zip",
        {
            "Fretio/Fretio.exe": "",
            "Fretio/_internal/version.txt": "1.0",
        },
    )
    app_dir = tmp_path / "app"

    launcher._safe_extract_zip_to_app(zip_path, app_dir)

    assert (app_dir / "Fretio.exe").exists()
    assert (app_dir / "_internal" / "version.txt").read_text(encoding="utf-8") == "1.0"


def test_launcher_safe_extract_rejects_path_traversal(tmp_path):
    zip_path = _make_zip(
        tmp_path / "bad.zip",
        {
            "Fretio.exe": "",
            "_internal/version.txt": "1.0",
            "../evil.txt": "x",
        },
    )

    try:
        launcher._safe_extract_zip_to_app(zip_path, tmp_path / "app")
    except ValueError as exc:
        assert "caminho inseguro" in str(exc) or "Path traversal" in str(exc)
    else:
        raise AssertionError("ZIP com path traversal deveria falhar")


def test_updater_rejects_incomplete_zip_before_bat(tmp_path):
    zip_path = _make_zip(
        tmp_path / "bad.zip",
        {
            "version.txt": "1.0",
            "_internal/version.txt": "1.0",
        },
    )
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()

    try:
        updater._safe_extract_update_zip(zip_path, extract_dir)
    except ValueError as exc:
        assert "Fretio.exe/FreteBot.exe" in str(exc)
    else:
        raise AssertionError("ZIP sem executável deveria falhar")


def test_apply_update_does_not_create_bat_for_incomplete_zip(monkeypatch, tmp_path):
    update_dir = tmp_path / "update"
    bad_entries = {
        "version.txt": "2.0",
        "_internal/version.txt": "2.0",
    }

    def fake_download(_url, dest, _total_size=0, callback=None):
        _make_zip(dest, bad_entries)

    monkeypatch.setattr(updater, "_license_dir_update", lambda: update_dir)
    monkeypatch.setattr(updater, "_download_with_progress", fake_download)

    info = updater.UpdateInfo(
        tag="v2.0",
        version="2.0",
        download_url="https://example.test/update.zip",
        asset_name="update.zip",
        asset_size=10,
        release_notes="",
        html_url="",
        source_repo="owner/repo",
    )

    assert updater.apply_update(info) is False
    assert not (update_dir / "_apply_update.bat").exists()
    assert not (update_dir / "_pending_update").exists()


def test_launcher_resolves_valid_legacy_executable(monkeypatch, tmp_path):
    app_dir = tmp_path / "Romaneio Beta"
    app_dir.mkdir()
    (app_dir / "FreteBot.exe").write_text("", encoding="utf-8")
    (app_dir / "version.txt").write_text("1.2", encoding="utf-8")
    monkeypatch.setattr(launcher, "APP_DIR_CANDIDATES", (app_dir,))

    assert launcher._resolve_app_dir() == app_dir
    assert launcher._resolve_app_exe(app_dir) == app_dir / "FreteBot.exe"


def test_launcher_opens_valid_local_app_when_github_is_offline(monkeypatch, tmp_path):
    app_dir = tmp_path / "Fretio"
    app_dir.mkdir()
    exe_path = app_dir / "Fretio.exe"
    exe_path.write_text("", encoding="utf-8")
    (app_dir / "version.txt").write_text("1.2", encoding="utf-8")
    launched = []

    monkeypatch.setattr(launcher, "APP_DIR_CANDIDATES", (app_dir,))
    monkeypatch.setattr(launcher, "_resolve_latest_release", lambda: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(launcher, "_launch_app", lambda app_exe: launched.append(app_exe))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    launcher._worker(None)

    assert launched == [exe_path]


def _fake_release_with_sig():
    return {
        "tag_name": "v2.0",
        "assets": [
            {"name": "Fretio-Update-latest.zip",
             "browser_download_url": "https://example.test/u.zip", "size": 10},
            {"name": "Fretio-Update-latest.zip.sig",
             "browser_download_url": "https://example.test/u.zip.sig", "size": 6},
        ],
    }


def _setup_launcher_worker(monkeypatch, tmp_path, verify, extracted, launched):
    app_dir = tmp_path / "Fretio"
    app_dir.mkdir()
    exe_path = app_dir / "Fretio.exe"
    exe_path.write_text("", encoding="utf-8")
    (app_dir / "version.txt").write_text("1.2", encoding="utf-8")

    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(launcher, "APP_DIR_CANDIDATES", (app_dir,))
    monkeypatch.setattr(launcher, "_resolve_latest_release",
                        lambda: ("kaianesteffens/RomaneioBeta", _fake_release_with_sig()))
    monkeypatch.setattr(launcher, "_download",
                        lambda url, dest, total, status_cb, progress_cb: dest.write_bytes(b"x"))
    monkeypatch.setattr(launcher, "verify_update_signature", verify)
    monkeypatch.setattr(launcher, "_safe_extract_zip_to_app",
                        lambda zip_path, ad: extracted.append(ad))
    monkeypatch.setattr(launcher, "_launch_app", lambda app_exe: launched.append(app_exe))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    return app_dir, exe_path


def test_launcher_rejects_update_with_invalid_signature_and_opens_local(monkeypatch, tmp_path):
    extracted, launched = [], []

    def _bad_sig(zip_path, sig_path):
        raise launcher.UpdateSignatureError("assinatura invalida")

    app_dir, exe_path = _setup_launcher_worker(monkeypatch, tmp_path, _bad_sig, extracted, launched)

    launcher._worker(None)

    assert extracted == []          # update nao assinado NAO foi instalado
    assert launched == [exe_path]   # abriu a versao local valida (fallback)


def test_launcher_installs_update_with_valid_signature(monkeypatch, tmp_path):
    extracted, launched = [], []
    app_dir, exe_path = _setup_launcher_worker(
        monkeypatch, tmp_path, lambda zip_path, sig_path: None, extracted, launched
    )

    launcher._worker(None)

    assert extracted == [app_dir]   # assinatura valida -> update instalado
    assert launched == [exe_path]


def test_pyinstaller_spec_hiddenimports_dynamic_translovato_provider():
    spec_text = (ROOT / "installer" / "Fretio.spec").read_text(encoding="utf-8")

    assert '"fretio.providers.translovato"' in spec_text


def test_build_release_workflow_requires_explicit_version_and_safe_publication():
    workflow_text = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")

    assert "version:" in workflow_text
    assert "required: true" in workflow_text
    assert "Input version deve usar formato X.Y ou X.Y.Z" in workflow_text
    assert 'Set-Content -Path "app\\version.txt" -Value $version' in workflow_text
    assert "Publicar release em outro repositorio" in workflow_text
    assert "exige RELEASES_TOKEN" in workflow_text
    assert "Release oficial exige UPDATE_SIGNING_PRIVATE_KEY_B64 e UPDATE_PUBLIC_KEY_B64" in workflow_text
    assert "publish_release=false; apenas artefatos internos do workflow foram gerados" in workflow_text
    assert "if: env.PUBLISH_RELEASE == 'true'" in workflow_text
    assert "Artefato obrigatorio ausente; release oficial abortada" in workflow_text
    # A chave PÚBLICA de update é embutida no código; nenhuma credencial do
    # desenvolvedor (token de Gist) é gerada ou embarcada.
    assert "_EMBEDDED_UPDATE_PUBLIC_KEY_B64" in workflow_text
    assert "ERROR_REPORT_TOKEN" not in workflow_text
    assert "error_report_token" not in workflow_text
    assert "error_gist_id" not in workflow_text
