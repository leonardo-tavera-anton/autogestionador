import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src import main
from src.scraper import (
    ClienteYaFiltradoError,
    _mensaje_acceso_denegado,
    _aceptar_confirmacion_exito,
    _es_confirmacion_exito,
    SesionFuvexExpiradaError,
    _url_sin_parametros,
    _url_solicita_inicio_sesion,
    _cliente_omitido,
    _cliente_ya_filtrado,
    filtrar_dni,
)
from src.heylocate_helper import (
    _validar_celular_consultado,
    consultar_operadora,
)


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
    async def test_google_account_selector_clicks_only_configured_email(self):
        account = Mock()
        account.count = AsyncMock(return_value=1)
        account.is_visible = AsyncMock(return_value=True)
        account.click = AsyncMock()
        text_locator = Mock()
        text_locator.first = account
        page = Mock()
        page.url = "https://accounts.google.com/v3/signin/accountchooser"
        page.get_by_text = Mock(return_value=text_locator)
        browser = FakeBrowser([page])

        selected = await main._seleccionar_cuenta_google(
            browser, "authorized@example.com"
        )

        self.assertTrue(selected)
        page.get_by_text.assert_called_once_with(
            "authorized@example.com", exact=True
        )
        account.click.assert_awaited_once_with(timeout=5000)
        self.assertEqual(
            main._url_sin_parametros(
                "https://accounts.google.com/signin?state=temporary"
            ),
            "https://accounts.google.com/signin",
        )

    def test_google_account_chooser_is_detected_without_logging_query_tokens(self):
        url = (
            "https://accounts.google.com/v3/signin/accountchooser"
            "?client_id=private&state=temporary"
        )

        self.assertTrue(_url_solicita_inicio_sesion(url))
        self.assertEqual(
            _url_sin_parametros(url),
            "https://accounts.google.com/v3/signin/accountchooser",
        )
        self.assertFalse(_url_solicita_inicio_sesion(main.FILTER_URL))

    async def test_filter_redirect_to_google_requests_session_recovery(self):
        page = FakePage(main.FILTER_URL, authenticated=True)
        campo_dni = Mock()
        campo_dni.fill = AsyncMock()
        boton = Mock()

        async def redirect_to_google():
            page.url = (
                "https://accounts.google.com/v3/signin/accountchooser"
                "?client_id=private"
            )

        boton.click = AsyncMock(side_effect=redirect_to_google)
        botones = Mock()
        botones.evaluate_all = AsyncMock(
            return_value=[{"index": 0, "text": "Filtrar"}]
        )
        botones.nth = Mock(return_value=boton)
        page.locator = Mock(
            side_effect=lambda selector: (
                FakeLocator(page, selector)
                if selector == "#USUARIO"
                else botones
            )
        )

        with (
            patch("src.scraper._buscar_campo", new_callable=AsyncMock, return_value=campo_dni),
            patch("src.scraper._avisos_visibles", new_callable=AsyncMock, return_value=[]),
        ):
            with self.assertRaises(SesionFuvexExpiradaError):
                await filtrar_dni(page, "12345678")

        boton.click.assert_awaited_once()

    def test_heylocate_rejects_a_different_default_number(self):
        self.assertEqual(
            _validar_celular_consultado("999999999", "+51 999 999 999"),
            "999999999",
        )
        with self.assertRaisesRegex(RuntimeError, "988888888"):
            _validar_celular_consultado("999999999", "988888888")

    def test_heylocate_rejects_missing_or_invalid_client_number(self):
        with self.assertRaises(ValueError):
            _validar_celular_consultado("", "999999999")
        with self.assertRaises(ValueError):
            _validar_celular_consultado("123456789", "123456789")

    async def test_heylocate_does_not_search_if_field_contains_another_number(self):
        campo = AsyncMock()
        campo.first = campo
        campo.input_value.return_value = "988888888"
        page = Mock()
        page.goto = AsyncMock()
        page.locator.return_value = campo
        page.get_by_role = Mock()

        with self.assertRaises(RuntimeError):
            await consultar_operadora(page, "999999999")

        campo.fill.assert_awaited_once_with("999999999")
        page.get_by_role.assert_not_called()

    async def test_main_restarts_after_browser_context_closes(self):
        with (
            patch.object(
                main,
                "_ejecutar_una_vez",
                new_callable=AsyncMock,
                side_effect=[
                    main.ContextoNavegadorCerradoError("context closed"),
                    asyncio.CancelledError,
                ],
            ) as run_once,
            patch.object(main.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main.main()

        self.assertEqual(run_once.await_count, 2)
        sleep.assert_awaited_once_with(5)

    def test_existing_filter_message_is_classified_separately(self):
        message = (
            "este cliente (32042765) ya se encuentra filtrado "
            "el (06/11/2026 10:15:37)"
        )

        self.assertTrue(_cliente_omitido(message))
        self.assertTrue(_cliente_ya_filtrado(message))
        self.assertTrue(issubclass(ClienteYaFiltradoError, main.ClienteOmitidoError))

    def test_registration_access_denied_message_is_detected(self):
        self.assertEqual(
            _mensaje_acceso_denegado("Acceso denegado.2"),
            "ACCESO DENEGADO.2",
        )
        self.assertIsNone(_mensaje_acceso_denegado("Registro guardado con éxito"))

    def test_registration_success_notice_variants_are_detected(self):
        self.assertTrue(_es_confirmacion_exito("El registro se realizó con éxito"))
        self.assertTrue(_es_confirmacion_exito("Registro guardado exitosamente"))
        self.assertTrue(_es_confirmacion_exito("Prospecto registrado con éxito"))

    async def test_registration_success_notice_closes_cerrar_button(self):
        modal = Mock()
        modal.count = AsyncMock(return_value=1)
        modal.wait_for = AsyncMock()

        special_buttons = Mock()
        special_buttons.count = AsyncMock(return_value=1)
        special_buttons.evaluate_all = AsyncMock(return_value=["Cerrar"])
        special_buttons.nth = Mock()
        modal_buttons = Mock()
        modal_buttons.count = AsyncMock(return_value=1)
        modal_buttons.evaluate_all = AsyncMock(return_value=["Cerrar"])
        modal_buttons.nth = Mock()

        close_button = Mock()
        close_button.click = AsyncMock()
        special_buttons.nth.return_value = close_button
        modal.locator = Mock(
            side_effect=lambda selector: (
                special_buttons
                if ".swal-button--confirm" in selector
                else modal_buttons
            )
        )

        overlay = Mock()
        overlay.count = AsyncMock(return_value=0)
        overlay.last = overlay

        page = Mock()
        page.locator = Mock(
            side_effect=lambda selector: (
                Mock(last=modal)
                if ".swal-modal" in selector
                else overlay
            )
        )

        self.assertTrue(await _aceptar_confirmacion_exito(page))
        close_button.click.assert_awaited_once_with(force=True, timeout=5000)

    async def test_registration_cycle_refreshes_list_and_registers_pending_client(self):
        page_list = FakePage(main.FILTER_LIST_URL)
        page_registration = FakePage(main.REGISTRO_URL)
        browser = FakeBrowser([page_list])
        registered = asyncio.Event()
        session_required = asyncio.Event()
        client = {
            "dni": "12345678",
            "nombre": "Cliente de prueba",
            "celular": "999999999",
        }

        async def stop_after_first_check(_delay):
            raise asyncio.CancelledError

        with (
            patch.object(main, "esperar_sesion_fuvex", new_callable=AsyncMock),
            patch.object(
                main,
                "abrir_pagina_con_sesion",
                new_callable=AsyncMock,
                side_effect=[page_list, page_registration, page_registration],
            ) as open_page,
            patch.object(
                main,
                "dnis_con_filtro_correcto",
                new_callable=AsyncMock,
                return_value=["12345678"],
            ),
            patch.object(
                main.asyncio,
                "to_thread",
                new_callable=AsyncMock,
                side_effect=[client, None],
            ),
            patch.object(
                main,
                "consultar_operadora",
                new_callable=AsyncMock,
                return_value="ENTEL",
            ),
            patch.object(
                main,
                "registrar_prospecto",
                new_callable=AsyncMock,
                return_value=True,
            ) as register,
            patch.object(main.asyncio, "sleep", stop_after_first_check),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main.proceso_registros(
                    page_list,
                    page_registration,
                    FakePage("about:blank"),
                    browser,
                    5,
                    set(),
                    registered,
                    asyncio.Lock(),
                    None,
                    session_required,
                )

        self.assertEqual(open_page.await_count, 3)
        self.assertTrue(open_page.await_args_list[0].kwargs["recargar"])
        register.assert_awaited_once()
        self.assertEqual(register.await_args.args[1], "12345678")
        self.assertTrue(registered.is_set())

    async def test_filter_limiter_uses_random_delay_range_between_sends(self):
        sleeps = []

        async def record_sleep(delay):
            sleeps.append(delay)

        limiter = main.crear_limitador_envios((10.0, 15.0), "filtro")
        with (
            patch.object(main.random, "uniform", return_value=12.5) as choose_interval,
            patch.object(main.asyncio, "sleep", record_sleep),
        ):
            await limiter()
            await limiter()

        choose_interval.assert_called_once_with(10.0, 15.0)
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)
        self.assertLessEqual(sleeps[0], 12.5)

    async def test_expired_session_is_restored_with_existing_browser_profile(self):
        page = FakePage(main.FILTER_URL)
        browser = FakeBrowser([page])
        session_required = asyncio.Event()
        session_required.set()
        session_lock = asyncio.Lock()

        async def restore_existing_login(page_to_restore, _browser):
            page_to_restore.authenticated = True
            page_to_restore.url = (
                "https://fuvexbn.a365.com.pe:7443/general/menu.php"
            )
            return page_to_restore

        with patch.object(
            main, "iniciar_sesion", side_effect=restore_existing_login
        ) as login:
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        main.esperar_sesion_fuvex(
                            browser, session_required, session_lock
                        )
                        for _ in range(2)
                    )
                ),
                timeout=5,
            )

        login.assert_awaited_once()
        self.assertFalse(session_required.is_set())
        self.assertEqual(page.url, main.FILTER_URL)

    async def test_closed_context_during_reauth_triggers_browser_restart(self):
        page = FakePage(main.FILTER_URL)
        browser = FakeBrowser([page])
        session_required = asyncio.Event()
        session_required.set()

        with patch.object(
            main,
            "iniciar_sesion",
            new_callable=AsyncMock,
            side_effect=RuntimeError(
                "BrowserContext.new_page: Target page, context or browser has been closed"
            ),
        ):
            with self.assertRaises(main.ContextoNavegadorCerradoError):
                await main.esperar_sesion_fuvex(browser, session_required)

    async def test_module_open_reauthenticates_and_retries_during_startup(self):
        page = FakePage(main.FILTER_URL, authenticated=True)
        browser = FakeBrowser([page])
        session_required = asyncio.Event()
        open_attempts = 0

        async def open_module(*args, **kwargs):
            nonlocal open_attempts
            del args, kwargs
            open_attempts += 1
            if open_attempts == 1:
                raise SesionFuvexExpiradaError("Fuvex requiere iniciar sesión.")
            return page

        async def restore_session(_browser, required, _lock):
            required.clear()

        with (
            patch.object(main, "_abrir_pagina_con_sesion", side_effect=open_module),
            patch.object(main, "esperar_sesion_fuvex", side_effect=restore_session) as wait,
        ):
            result = await main.abrir_pagina_con_sesion(
                page,
                browser,
                main.FILTER_URL,
                "ingresar_filtro.php",
                sesion_requerida=session_required,
            )

        self.assertIs(result, page)
        self.assertEqual(open_attempts, 2)
        self.assertEqual(wait.await_count, 2)

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
