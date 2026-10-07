import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

import web_app


class _FakeLoop:
    def __init__(self):
        self.shutdown_calls = []

    def shutdown(self, *, cleanup_coro_factory=None):
        self.shutdown_calls.append(cleanup_coro_factory)


class _FakeSession:
    async def cleanup(self):
        return None


def test_saved_config_recreates_runtime_session(tmp_path):
    config_path = tmp_path / "CONFIG.toml"
    config_path.write_text("[fretio]\nmax_paralelo = 3\n", encoding="utf-8")
    api = web_app.Api(empresa="TESTE", config_path=config_path)
    loop = _FakeLoop()
    session = _FakeSession()
    api._loop, api._sessao = loop, session

    result = api.config_salvar_empresa({"paralelas": 5})

    assert result == {"ok": True}
    assert loop.shutdown_calls == [session.cleanup]
    assert api._loop is None and api._sessao is None


def test_saved_config_waits_for_active_operation_before_refresh(tmp_path):
    config_path = tmp_path / "CONFIG.toml"
    config_path.write_text("[fretio]\nmax_paralelo = 3\n", encoding="utf-8")
    api = web_app.Api(empresa="TESTE", config_path=config_path)
    loop = _FakeLoop()
    session = _FakeSession()
    api._loop, api._sessao = loop, session
    api._cotando = True

    assert api.config_salvar_empresa({"paralelas": 4}) == {"ok": True}
    assert api._runtime_config_dirty is True
    assert loop.shutdown_calls == []

    api._cotando = False
    api._apply_pending_runtime_config()

    assert loop.shutdown_calls == [session.cleanup]
    assert api._runtime_config_dirty is False


def test_dashboard_is_not_in_active_web_shell():
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")

    assert 'data-page="dashboard"' not in html
    assert 'pages/dashboard.js' not in html
    assert 'navigate("romaneio")' in js
    assert 'navigate("dashboard")' not in js


def test_quote_text_areas_grow_with_content():
    css = (ROOT / "app" / "web" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "app" / "web" / "pages" / "cotacao.js").read_text(encoding="utf-8")

    assert "resize: none; overflow-y: hidden" in css
    assert "max-height: 360px" not in css
    assert "el.scrollHeight" in js
