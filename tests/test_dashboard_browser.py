"""Optional Chromium acceptance using the same loopback APIs as the operator."""

import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from masf import cli, queue, worker
from masf.system import CommandTimeout
from tests.test_dashboard import JOB, TOKEN, create_fixture


@unittest.skipUnless(os.environ.get("MASF_DASHBOARD_BROWSER") == "1",
                     "requires local Playwright and Chromium")
class DashboardBrowser(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import expect, sync_playwright

        self.expect = expect
        self.app = create_fixture(self)
        self.origin = f"http://127.0.0.1:{self.app.port}"
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch()
        self.addCleanup(browser.close)
        self.page = browser.new_page(viewport={"width": 1440, "height": 1000})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def capture(self, name):
        folder = Path(os.environ.get("MASF_DASHBOARD_EVIDENCE", "/tmp/masf-dashboard-evidence"))
        folder.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=folder / f"{name}.png", full_page=True)
        self.assertEqual(self.errors, [])

    def dashboard(self, query=""):
        self.page.goto(self.origin + "/dashboard/" + query)
        self.expect(self.page.locator("#freshness")).to_contain_text("Última actualización")

    def refresh(self):
        self.page.get_by_role("button", name="Actualizar", exact=True).click()
        self.expect(self.page.locator("#main")).to_have_attribute("aria-busy", "false")

    def authenticate(self, token=TOKEN):
        self.page.get_by_label("Token del operador").fill(token)
        self.page.get_by_role("button", name="Consultar acciones").click()

    def decision(self, text):
        form = self.page.locator("#operator-actions fieldset")
        form.get_by_role("textbox").fill(text)
        form.get_by_role("checkbox").check()
        form.get_by_role("button", name="Enviar decisión").click()

    def applied(self, state):
        receipt = self.page.get_by_role("heading", name="Recibo de la decisión").locator("..")
        self.expect(receipt.get_by_text("Aplicada", exact=True)).to_be_visible()
        self.expect(receipt.get_by_text(state, exact=True).first).to_be_visible()

    def test_overview_active_and_terminal_keyboard_mobile(self):
        app = self.app
        self.dashboard()
        self.expect(self.page.get_by_role("heading", name="En cola")).to_be_visible()
        self.capture("01-cola")
        self.page.keyboard.press("Tab")
        self.expect(self.page.get_by_role("link", name="Saltar al contenido")).to_be_focused()
        self.page.get_by_label("Estado").select_option("queued")
        self.page.get_by_role("button", name="Filtrar").click()
        self.page.get_by_role("link", name="#1 · Add a greeting").click()
        self.expect(self.page.get_by_role("heading", name="Add a greeting")).to_be_visible()
        self.expect(self.page.get_by_role("link", name="Volver al listado")).to_have_attribute("href", "/dashboard/?state=queued")

        ready, release = threading.Event(), threading.Event()
        agent = worker.providers.invoke

        def pause_plan(role, *args, **kwargs):
            if role == "plan":
                ready.set()
                if not release.wait(15):
                    raise RuntimeError("Fixture plan wait expired")
            return agent(role, *args, **kwargs)

        def create(*args):
            instance = worker.Worker(*args)
            instance.url = str(app.source)
            instance.recover = lambda: None
            instance.test = app.check
            return instance

        with patch.object(cli, "Worker", create), patch.object(worker.providers, "invoke", pause_plan):
            results = []
            controller = threading.Thread(target=lambda: results.append(
                cli.process_task(app.cfg, app.server.config, app.server.root, app.job)))
            controller.start()
            try:
                self.assertTrue(ready.wait(5))
                self.page.set_viewport_size({"width": 390, "height": 844})
                self.refresh()
                self.expect(self.page.get_by_role("heading", name="Etapa activa")).to_be_visible()
                self.expect(self.page.get_by_text("Resumen del razonamiento").first).to_be_visible()
                self.expect(self.page.get_by_text("Pendiente", exact=True).first).to_be_visible()
                self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth === innerWidth"))
                self.capture("02-etapa-activa-movil")
            finally:
                release.set()
                controller.join(15)
            self.assertEqual(results, [0])
        queue.save(app.worker.db, app.ci_job(), "merged")
        self.refresh()
        self.expect(self.page.get_by_text("Fusionado", exact=True).first).to_be_visible()
        self.capture("03-trabajo-terminado")

    def test_browser_answers_handles_stale_and_retry_authorizes_and_resumes(self):
        app = self.app
        app.scenario()
        self.assertEqual(app.run_ready(), 0)
        self.dashboard("?view=intervention")
        self.expect(self.page.get_by_text("Pregunta", exact=True)).to_be_visible()
        self.page.get_by_role("link", name="#1 · Add a greeting").click()
        self.expect(self.page.get_by_text("¿Para quién es el saludo?", exact=True).first).to_be_visible()
        self.authenticate("incorrecto")
        self.expect(self.page.get_by_text("Token de operador inválido")).to_be_visible()
        self.capture("04-autenticacion")
        self.authenticate()
        self.expect(self.page.get_by_label("Respuesta", exact=True)).to_be_visible()
        queue.save(app.worker.db, app.job, "needs_input", reason="Contexto revisado por otro operador")
        self.decision("Para Ana")
        self.expect(self.page.get_by_text("El trabajo cambió. Revisa el contexto actualizado.")).to_be_visible()
        self.expect(self.page.get_by_role("checkbox")).not_to_be_checked()
        self.capture("05-pregunta-conflicto")
        idempotency_keys = []

        def lose_first_response(route):
            if route.request.method != "POST":
                route.continue_()
                return
            idempotency_keys.append(route.request.headers["idempotency-key"])
            if len(idempotency_keys) == 1:
                route.fetch()
                route.abort()
            else:
                route.continue_()

        self.page.route("**/actions", lose_first_response)
        self.decision("Para Ana")
        self.page.get_by_role("button", name="Reintentar el mismo envío").click()
        self.applied("Planificación")
        self.assertEqual(idempotency_keys[0], idempotency_keys[1])
        self.page.unroute("**/actions", lose_first_response)
        self.capture("06-respuesta-aplicada")

        self.assertEqual(app.run_ready(), 1)
        self.refresh()
        self.expect(self.page.get_by_text(app.changed, exact=True).first).to_be_visible()
        self.expect(self.page.get_by_text("Confirmo la autorización de todos los archivos enumerados")).to_be_visible()
        self.capture("07-ruta-protegida")
        self.decision("Archivos revisados")
        self.applied("Comprobaciones")
        self.assertEqual(app.run_ready(), 1)
        self.refresh()
        self.expect(self.page.get_by_role("heading", name="Última comprobación local")).to_be_visible()
        self.capture("08-checks-y-bloqueo")
        app.deny = False
        self.decision("Permiso reparado")
        self.applied("Publicación")
        self.assertEqual(app.run_ready(), 0)
        self.expect(self.page.get_by_text("PR abierto", exact=True).first).to_be_visible(timeout=15000)
        for expected in ("fixture-plan-1", "fixture-plan-2", "No comunicado", "cursor", "codex"):
            self.expect(self.page.get_by_text(expected, exact=True).first).to_be_visible()
        self.expect(self.page.get_by_text("0 tokens", exact=False).first).to_be_visible()
        self.capture("09-reanudacion-completa-y-trazabilidad")

    def test_timeout_recovers_files_from_browser(self):
        app = self.app
        app.cfg["service"]["max_recovery_attempts"] = 0
        seen = []

        def agent(role, job, cfg, workspace, inputs, owner, **kwargs):
            if role == "build":
                seen.append(workspace)
                if len(seen) == 1:
                    (workspace / "partial").write_text("keep")
                    raise CommandTimeout("docker: tiempo agotado (900s)")
                self.assertEqual((workspace / "partial").read_text(), "keep")
                self.assertEqual(cfg["service"]["step_timeout_seconds"], 1200)
            return app.agent(role, job, cfg, workspace, inputs, owner, **kwargs)

        with patch.object(worker.providers, "invoke", agent):
            self.assertEqual(app.run_ready(), 1)
            self.dashboard("?job=123/1")
            self.authenticate()
            self.page.get_by_label("Nuevo plazo de construcción (segundos)").fill("1200")
            self.capture("10-timeout")
            self.decision("Continuar con la copia compatible")
            self.applied("Construcción")
            self.assertEqual(app.run_ready(), 0)
            self.refresh()
            self.expect(self.page.get_by_text("PR abierto", exact=True).first).to_be_visible()
            self.capture("11-timeout-reanudado")
        self.assertEqual(seen[0], seen[1])

    def test_ci_shows_running_functional_repair_infrastructure_and_success(self):
        app = self.app
        app.open_ci_pr()
        app.snapshot_ci("in_progress", None)
        self.dashboard("?view=pulls")
        self.expect(self.page.get_by_text("CI en ejecución", exact=True)).to_be_visible()
        self.capture("12-ci-en-ejecucion")

        app.annotation = "Runner lost"
        app.ci_logs = "Runner lost"
        self.assertEqual(app.ci_tick(observe_only=True), 0)
        self.dashboard("?job=123/1")
        self.expect(self.page.get_by_text("Infraestructura", exact=True).first).to_be_visible()
        self.capture("13-ci-infraestructura")
        with patch("masf.cli.time.time", return_value=app.ci_job()["ci_retry_at"] + 1):
            self.assertEqual(app.ci_tick(), 0)
        self.refresh()
        self.expect(self.page.get_by_role("heading", name="Intervención · Bloqueo")).to_be_visible()
        self.capture("14-ci-intervencion")
        self.authenticate()
        self.decision("Infraestructura restaurada y CI relanzado")
        self.applied("PR abierto")

        app.action_attempt = 2
        app.annotation = "FAIL: test_ci_repair"
        app.ci_logs = app.annotation
        app.snapshot_ci("completed", "failure")
        self.assertEqual(app.ci_tick(observe_only=True), 0)
        self.refresh()
        self.expect(self.page.get_by_role("heading", name="Reparación de CI")).to_be_visible()
        self.expect(self.page.get_by_text("Funcional", exact=True).first).to_be_visible()
        self.capture("15-ci-reparacion-funcional")
        self.assertEqual(app.ci_tick(), 0)
        self.assertEqual(app.ci_tick(), 0)
        app.snapshot_ci("completed", "success")
        self.refresh()
        self.expect(self.page.get_by_text("CI correcto", exact=True).first).to_be_visible()
        self.expect(self.page.get_by_role("heading", name="Head histórico").first).to_be_visible()
        self.capture("16-ci-reparado")


if __name__ == "__main__":
    unittest.main()
