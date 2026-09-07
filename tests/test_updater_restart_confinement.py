"""Regressão de segurança: restart_app confina o alvo executável ao BAT
determinístico em update_dir, ignorando caminhos adulterados em _pending_update
(CWE-494)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import updater  # noqa: E402


def _arm_restart(monkeypatch, update_dir):
    """Aponta o updater para um update_dir de teste e neutraliza Popen/exit."""
    monkeypatch.setattr(updater, "_license_dir_update", lambda: update_dir)
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(updater.sys, "exit", lambda code=0: None)
    return calls


def test_restart_app_ignores_tampered_pointer(tmp_path, monkeypatch):
    update_dir = tmp_path / "update"
    update_dir.mkdir()
    legit_bat = update_dir / "_apply_update.bat"
    legit_bat.write_text("@echo legit\n", encoding="utf-8")

    # Atacante reescreve o ponteiro para um script fora do update_dir.
    evil_bat = tmp_path / "evil.bat"
    evil_bat.write_text("@echo pwned\n", encoding="utf-8")
    (update_dir / "_pending_update").write_text(str(evil_bat), encoding="utf-8")

    calls = _arm_restart(monkeypatch, update_dir)
    updater.restart_app()

    assert len(calls) == 1, "deveria lançar exatamente um processo"
    (args, _kwargs) = calls[0]
    argv = args[0]
    assert argv[:2] == ["cmd", "/c"]
    executed = argv[2]
    assert executed == str(legit_bat.resolve()), "deve executar o BAT determinístico"
    assert str(evil_bat) not in executed, "nunca deve executar o caminho adulterado"


def test_restart_app_no_launch_when_bat_missing(tmp_path, monkeypatch):
    update_dir = tmp_path / "update"
    update_dir.mkdir()
    # Marcador presente, mas o BAT determinístico não existe.
    (update_dir / "_pending_update").write_text(str(tmp_path / "evil.bat"), encoding="utf-8")

    calls = _arm_restart(monkeypatch, update_dir)
    updater.restart_app()

    assert calls == [], "sem _apply_update.bat legítimo não deve lançar nada"
    assert not (update_dir / "_pending_update").exists(), "marcador deve ser limpo"
