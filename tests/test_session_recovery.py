import asyncio
import unittest
from unittest.mock import patch

from src import main
from src.scraper import SesionFuvexExpiradaError


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        return int(
            self.selector == "#USUARIO"
            and self.page.url.endswith("/index.php")
        )

    async def is_visible(self):
        return self.page.url.endswith("/index.php")


class FakePage:
    def __init__(self, url, authenticated=False):
        self.url = url
        self.authenticated = authenticated
        self.navigation_count = 0
        self.frames = []

    def is_closed(self):
        return False

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_text(self, text, **kwargs):
        del text, kwargs
        return FakeLocator(self, "")

    async def goto(self, url, **kwargs):
        del kwargs
        self.navigation_count += 1
        self.url = url if self.authenticated else main.LOGIN_URL


class FakeBrowser:
    def __init__(self, pages):
        self.pages = pages


class SessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_module_tab_does_not_resume_expired_session(self):
        page = FakePage(main.FILTER_URL)
        browser = FakeBrowser([page])
        session_required = asyncio.Event()
        session_required.set()
        session_lock = asyncio.Lock()

        recoveries = [
            asyncio.create_task(
                main.esperar_sesion_fuvex(
                    browser, session_required, session_lock
                )
            )
            for _ in range(2)
        ]
        try:
            for _ in range(50):
                if page.navigation_count:
                    break
                await asyncio.sleep(0.02)

            self.assertGreater(page.navigation_count, 0)
            self.assertTrue(session_required.is_set())
            self.assertEqual(page.url, main.LOGIN_URL)

            page.authenticated = True
            page.url = "https://fuvexbn.a365.com.pe:7443/general/menu.php"
            await asyncio.wait_for(asyncio.gather(*recoveries), timeout=5)

            self.assertFalse(session_required.is_set())
            self.assertEqual(page.url, main.FILTER_URL)
        finally:
            for recovery in recoveries:
                if not recovery.done():
                    recovery.cancel()
            await asyncio.gather(*recoveries, return_exceptions=True)

    async def test_module_redirect_to_login_requests_coordinated_reauth(self):
        page = FakePage("about:blank")

        async def redirect_to_login(page_to_redirect, url_requested):
            del page_to_redirect, url_requested
            page.url = main.LOGIN_URL

        with patch.object(main, "abrir_pagina", redirect_to_login):
            with self.assertRaises(SesionFuvexExpiradaError):
                await main._abrir_pagina_con_sesion(
                    page,
                    FakeBrowser([page]),
                    main.FILTER_URL,
                    "ingresar_filtro.php",
                )

    async def test_closed_browser_context_fails_explicitly(self):
        class ClosedBrowser:
            @property
            def pages(self):
                raise RuntimeError("context closed")

        session_required = asyncio.Event()
        session_required.set()
        with self.assertRaises(main.ContextoNavegadorCerradoError):
            await main.esperar_sesion_fuvex(
                ClosedBrowser(), session_required
            )


if __name__ == "__main__":
    unittest.main()
