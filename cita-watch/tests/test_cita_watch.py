import datetime as dt
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import cita_watch as cw

MADRID = cw.MADRID


def make_cfg(tmp_path: Path, **over) -> cw.Config:
    data = dict(province=8, tramite=4010, doc_type="nie", doc_number="Y0000000Z",
                full_name="Nombre Apellido", heartbeat_hour=None, peak_windows=[])
    data.update(over)
    p = tmp_path / "config.yaml"
    import yaml
    p.write_text(yaml.safe_dump(data))
    return cw.Config.load(p)


# ------------------------------------------------------------------ classify
@pytest.mark.parametrize("text,state", [
    ("En este momento no hay citas disponibles.", "none"),
    ("En este momento NO HAY CITAS DISPONIBLES", "none"),
    ("Seleccione la oficina donde solicitar la cita", "available"),
    ("DISPONE DE 5 MINUTOS para confirmar", "available"),
    ("The requested URL was rejected. Please consult", "blocked"),
    ("Something else entirely", "unknown"),
])
def test_classify(text, state):
    assert cw.classify(text) == state


# ------------------------------------------------------------------ documents / config
@pytest.mark.parametrize("doc_type,number,ok", [
    ("nie", "Y0000000Z", True),
    ("nie", "X1234567L", True),
    ("nie", "X1234567A", False),       # wrong control letter
    ("nie", "12345678Z", False),
    ("dni", "12345678Z", True),
    ("dni", "12345678A", False),
    ("passport", "AB123456", True),
    ("passport", "A1", False),
])
def test_validate_document(doc_type, number, ok):
    assert (cw.validate_document(doc_type, number) is None) is ok


def test_config_normalizes_and_resolves_paths(tmp_path):
    cfg = make_cfg(tmp_path, doc_number="y-0000000 z", country="marruecos", phone="612 345 678")
    assert cfg.doc_number == "Y0000000Z"
    assert cfg.full_name == "NOMBRE APELLIDO"
    assert cfg.country == "MARRUECOS"
    assert cfg.phone == "612345678"
    assert Path(cfg.history_file).parent == tmp_path


def test_config_collects_errors(tmp_path):
    with pytest.raises(SystemExit) as e:
        make_cfg(tmp_path, doc_number="Y0000000X", active_hours=[23, 8], phone="123",
                 telegram_bot_token="t")
    msg = str(e.value)
    assert "final letter" in msg and "active_hours" in msg and "phone" in msg and "telegram" in msg


def test_min_interval_enforced(tmp_path):
    cfg = make_cfg(tmp_path, interval_seconds=10,
                   peak_windows=[{"hours": [8, 10], "interval_seconds": 30}])
    assert cfg.interval_seconds == cw.MIN_INTERVAL
    assert cfg.peak_windows[0]["interval_seconds"] == cw.MIN_INTERVAL


# ------------------------------------------------------------------ timing
def test_active_hours_use_madrid_time(tmp_path):
    cfg = make_cfg(tmp_path, active_hours=[8, 23])
    # 06:30 UTC in summer is 08:30 in Madrid -> active, although UTC hour is 6
    utc = dt.datetime(2026, 7, 1, 6, 30, tzinfo=dt.timezone.utc)
    assert cw.within_active_hours(cfg, utc.astimezone(MADRID))
    assert not cw.within_active_hours(cfg, dt.datetime(2026, 7, 1, 23, 5, tzinfo=MADRID))


def test_peak_windows(tmp_path):
    cfg = make_cfg(tmp_path, interval_seconds=300,
                   peak_windows=[{"days": [0, 1, 2, 3, 4], "hours": [8, 10], "interval_seconds": 150}])
    monday_9 = dt.datetime(2026, 9, 28, 9, 0, tzinfo=MADRID)
    monday_11 = monday_9.replace(hour=11)
    sunday_9 = dt.datetime(2026, 9, 27, 9, 0, tzinfo=MADRID)
    assert cw.current_interval(cfg, monday_9) == 150
    assert cw.current_interval(cfg, monday_11) == 300
    assert cw.current_interval(cfg, sunday_9) == 300


def test_block_backoff_doubles_and_caps():
    assert [cw.block_backoff(n) // 60 for n in (1, 2, 3, 4, 5)] == [15, 30, 60, 120, 120]


def test_applescript_escaping():
    assert cw._applescript_str('say "hi" \\ bye') == '"say \\"hi\\" \\\\ bye"'


# ------------------------------------------------------------------ history
def test_history_and_stats(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    for i, s in enumerate(["none", "none", "available"], 1):
        cw.append_history(cfg, i, s)
    cw.print_stats(cfg)
    out = capsys.readouterr().out
    assert "3 checks" in out and "available 1" in out and "When slots were seen" in out


# ------------------------------------------------------------------ control panel
def test_control_panel(tmp_path):
    ctl = cw.Control(server_mode=True)
    srv = cw.start_control_server(ctl, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status = json.load(urllib.request.urlopen(base + "/api/status"))
        assert status["status"] == "starting" and status["server_mode"] is True
        assert b"Cita watch" in urllib.request.urlopen(base + "/").read()

        # Without the custom header (e.g. a cross-site form post) -> refused
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/api/pause", method="POST", data=b""))
        assert e.value.code == 403
        assert not ctl.paused

        req = lambda a: urllib.request.Request(base + "/api/" + a, method="POST", data=b"",
                                               headers={"X-Requested-With": "cita"})
        urllib.request.urlopen(req("pause"))
        assert ctl.paused
        urllib.request.urlopen(req("done"))
        assert ctl.done.is_set()
        urllib.request.urlopen(req("unpause"))
        assert not ctl.paused and ctl.wake.is_set()
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ end-to-end against a fake ICP site
def _page(body: str) -> bytes:
    return f"<html><body>{body}</body></html>".encode()


class FakeICP(BaseHTTPRequestHandler):
    result = "none"      # "none" | "available" | "blocked"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if self.result == "blocked" and path.endswith("/acInfo"):
            body = _page("The requested URL was rejected.")
        elif path.endswith("/citar"):
            body = _page("Provincia")
        elif path.endswith("/acInfo"):
            body = _page("INTERNET CITA PREVIA <a id='btnEntrar' href='acEntrada'>Entrar</a>")
        elif path.endswith("/acEntrada"):
            body = _page("""<form action='acValidarEntrada' method='get'>
                <input type='radio' id='rdbTipoDocNie' name='t'>
                <input id='txtIdCitado' name='id'><input id='txtDesCitado' name='n'>
                <input type='submit' id='btnEnviar' value='Aceptar'></form>""")
        elif path.endswith("/acValidarEntrada"):
            body = _page("""<form action='acCitar' method='get'>
                <input type='submit' id='btnEnviar' value='Solicitar Cita'></form>
                <button id='btnSalir'>Salir</button>""")
        elif path.endswith("/acCitar"):
            body = _page("Seleccione la oficina donde solicitar la cita <select><option>A</option></select>"
                         if self.result == "available" else
                         "En este momento no hay citas disponibles. <button id='btnSalir'>Salir</button>")
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_site(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeICP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(cw, "BASE", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(cw, "pause", lambda *a: None)
    yield FakeICP
    srv.shutdown()


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            # CHROMIUM_PATH lets you reuse an already-installed browser
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {e}")
        yield browser.new_page()
        browser.close()


@pytest.mark.parametrize("result", ["none", "available", "blocked"])
def test_check_once_end_to_end(fake_site, page, tmp_path, monkeypatch, result):
    monkeypatch.setattr(fake_site, "result", result)
    cfg = make_cfg(tmp_path)
    assert cw.check_once(cfg, page, cw.Control(server_mode=True)) == result


def test_prefill_contact_only_fills_empty_fields(page, tmp_path):
    cfg = make_cfg(tmp_path, phone="612345678", email="me@example.com")
    page.set_content("<input id='txtTelefonoCitado' value='699999999'>"
                     "<input id='emailUNO'><input id='emailDOS'>")
    cw.prefill_contact(cfg, page)
    assert page.input_value("#txtTelefonoCitado") == "699999999"   # user's own input kept
    assert page.input_value("#emailUNO") == "me@example.com"
    assert page.input_value("#emailDOS") == "me@example.com"


def test_wait_for_human_times_out_and_returns(page, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    ctl = cw.Control(server_mode=True)
    monkeypatch.setattr(cw.time, "monotonic", iter([0, 0, 10_000]).__next__)
    assert cw.wait_for_human(cfg, page, ctl, "test", minutes=1, alert=False) is False
    ctl.done.set()
    monkeypatch.setattr(cw.time, "monotonic", iter([0, 0, 0, 10_000]).__next__)
    # done is cleared at the start of each wait, so a stale ENTER doesn't count
    assert cw.wait_for_human(cfg, page, ctl, "test", minutes=1, alert=False) is False
