import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from src.heylocate_helper import consultar_operadora

load_dotenv()

from src.db import actualizar_estado_dni, obtener_pendiente_por_dni, obtener_pendientes
from src.scraper import (
    FILTER_LIST_URL,
    FILTER_URL,
    REGISTRO_URL,
    ColaEsperaLlenaError,
    ClienteOmitidoError,
    ConfirmacionExitoAnteriorError,
    FiltroSinConfirmarError,
    FiltrosNoDisponiblesError,
    RegistroBloqueadoPorDialogoError,
    RegistroRechazadoError,
    RegistroSinConfirmarError,
    SesionFuvexExpiradaError,
    abrir_pagina,
    dnis_con_filtro_correcto,
    esperar_cierre_dialogo_fuvex,
    filtrar_dni,
    registrar_prospecto,
)

LOGIN_URL = "https://fuvexbn.a365.com.pe:7443/index.php"
MENU_PATTERN = re.compile(r"/menu\.php(?:$|[?#])", re.I)
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
BRAVE_USER_DATA = Path(
    os.getenv("BRAVE_USER_DATA", str(LOCAL_APP_DATA / "BraveSoftware" / "Brave-Browser" / "User Data"))
)


class ContextoNavegadorCerradoError(RuntimeError):
    """El navegador cerró su contexto y no puede recuperar la sesión."""


def encontrar_brave():
    configurado = os.getenv("BRAVE_PATH")
    posibles_rutas = [
        Path(configurado) if configurado else None,
        LOCAL_APP_DATA / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path(r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    return next((ruta for ruta in posibles_rutas if ruta and ruta.is_file()), None)


def intervalo_aviso_google():
    valor = os.getenv(
        "GOOGLE_LOGIN_REMINDER_SECONDS",
        os.getenv("GOOGLE_LOGIN_TIMEOUT_SECONDS", "600"),
    )
    try:
        return max(60, int(valor))
    except ValueError as exc:
        raise RuntimeError(
            "GOOGLE_LOGIN_REMINDER_SECONDS debe ser un numero entero."
        ) from exc


async def iniciar_sesion(page, browser):
    if not MENU_PATTERN.search(page.url):
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    if MENU_PATTERN.search(page.url) and not await pagina_pide_google(page):
        return page

    usuario = os.getenv("FUVEX_USER")
    clave = os.getenv("FUVEX_PASS")
    campos_usuario = page.locator("#USUARIO")
    if await campos_usuario.count():
        if not usuario or not clave:
            raise RuntimeError("Configura FUVEX_USER y FUVEX_PASS en .env para iniciar sesion.")
        await campos_usuario.fill(usuario)
        await page.locator("input[type='password']").fill(clave)
        await page.locator("button[type='submit']").click()

    boton_google = page.get_by_text(re.compile(r"Iniciar sesi.n con Google", re.I)).first
    try:
        await boton_google.wait_for(state="visible", timeout=15000)
    except Exception as exc:
        if MENU_PATTERN.search(page.url):
            return page
        raise RuntimeError(
            f"No aparecio el boton 'Iniciar sesion con Google'. URL actual: {page.url}"
        ) from exc

    await boton_google.click()
    intervalo_aviso = intervalo_aviso_google()
    print("[*] Google espera que elijas la cuenta autorizada en Brave y completes cualquier verificacion.")

    siguiente_aviso = asyncio.get_running_loop().time() + intervalo_aviso
    while True:
        try:
            paginas = list(browser.pages)
        except Exception as exc:
            raise ContextoNavegadorCerradoError(
                "Se cerro Brave durante la autenticacion de Google. "
                "Comprueba el navegador y vuelve a iniciar el programa."
            ) from exc
        if not paginas:
            raise ContextoNavegadorCerradoError(
                "Se cerraron todas las pestañas durante la autenticacion de Google. "
                "Abre Brave y vuelve a iniciar el programa."
            )

        for candidate in paginas:
            try:
                if not MENU_PATTERN.search(candidate.url) or await pagina_pide_google(candidate):
                    continue
                try:
                    await candidate.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    # El callback puede completar la navegacion antes de emitir load.
                    pass
                await candidate.wait_for_timeout(750)
                return candidate
            except Exception:
                continue

        ahora = asyncio.get_running_loop().time()
        if ahora >= siguiente_aviso:
            urls = ", ".join(candidate.url for candidate in paginas) or "ninguna pestaña abierta"
            print(
                "[!] El acceso de Google sigue pendiente. Completa la selección de cuenta "
                f"y cualquier verificación en Brave. Pestañas: {urls}"
            )
            siguiente_aviso = ahora + intervalo_aviso
        await asyncio.sleep(1)


async def pagina_pide_google(page):
    """Detecta el menu que aun requiere completar el acceso mediante Google."""
    if (urlsplit(page.url).hostname or "").lower() == "accounts.google.com":
        return True
    boton = page.get_by_text(re.compile(r"Iniciar sesi.n con Google", re.I)).first
    try:
        return await boton.count() > 0 and await boton.is_visible()
    except Exception:
        return False


async def pagina_sesion_fuvex_activa(page):
    """Reconoce el menú o un módulo Fuvex cargado tras la autenticación."""
    direccion = urlsplit(page.url)
    if (direccion.hostname or "").lower() != "fuvexbn.a365.com.pe":
        return False
    ruta = direccion.path.lower()
    if await pagina_pide_google(page):
        return False
    if not MENU_PATTERN.search(page.url) and not (
        ruta.startswith("/bn/") and ruta.endswith(".php")
    ):
        return False

    campo_login = page.locator("#USUARIO")
    try:
        return not (await campo_login.count() and await campo_login.is_visible())
    except Exception:
        return False


async def pagina_inicio_sesion_fuvex(page):
    host = (urlsplit(page.url).hostname or "").lower()
    ruta = urlsplit(page.url).path.lower()
    if host == "accounts.google.com":
        return True
    if host != "fuvexbn.a365.com.pe":
        return False
    if ruta.endswith("/index.php") or await pagina_pide_google(page):
        return True
    campo_login = page.locator("#USUARIO")
    try:
        return await campo_login.count() > 0 and await campo_login.is_visible()
    except Exception:
        return False


async def esperar_sesion_fuvex(browser, sesion_requerida, session_lock=None):
    """Pausa ambos ciclos hasta que el usuario complete el acceso autorizado."""
    if not sesion_requerida.is_set():
        return

    print(
        "[!] Sesión pausada. Completa el acceso a Fuvex en Brave."
    )
    siguiente_aviso = asyncio.get_running_loop().time() + intervalo_aviso_google()
    while sesion_requerida.is_set():
        try:
            paginas = list(browser.pages)
        except Exception as exc:
            raise ContextoNavegadorCerradoError(
                "Se cerro el contexto de Brave/Playwright; no es posible "
                "reanudar la sesion. Reinicia el programa."
            ) from exc
        if not paginas:
            try:
                if session_lock is None:
                    pagina_acceso = await browser.new_page()
                    await pagina_acceso.goto(
                        FILTER_URL, wait_until="domcontentloaded", timeout=45000
                    )
                else:
                    async with session_lock:
                        if not browser.pages:
                            pagina_acceso = await browser.new_page()
                            await pagina_acceso.goto(
                                FILTER_URL,
                                wait_until="domcontentloaded",
                                timeout=45000,
                            )
                print("[!] Se abrió una pestaña de acceso a Fuvex en Brave.")
            except Exception as exc:
                raise ContextoNavegadorCerradoError(
                    "No pude abrir una pestaña para recuperar Fuvex. "
                    "Comprueba que Brave siga abierto y reinicia el programa."
                ) from exc
            continue

        for page in paginas:
            try:
                if not await pagina_sesion_fuvex_activa(page):
                    continue
                if session_lock is None:
                    await page.goto(
                        FILTER_URL, wait_until="domcontentloaded", timeout=45000
                    )
                else:
                    async with session_lock:
                        if page.is_closed() or not await pagina_sesion_fuvex_activa(page):
                            continue
                        await page.goto(
                            FILTER_URL,
                            wait_until="domcontentloaded",
                            timeout=45000,
                        )
                if await pagina_sesion_fuvex_activa(page):
                    sesion_requerida.clear()
                    print("[*] Sesión verificada. Se reanudan los ciclos.")
                    return
            except Exception:
                continue
        ahora = asyncio.get_running_loop().time()
        if ahora >= siguiente_aviso:
            print("[!] Fuvex aún requiere acceso. Completa Google/verificación en Brave.")
            siguiente_aviso = ahora + intervalo_aviso_google()
        await asyncio.sleep(2)


async def limpiar_pestanas_fuvex(browser, conservar=()):
    """Cierra pestañas antiguas de Fuvex/Google, sin tocar otras páginas del usuario."""
    conservar_ids = {
        id(getattr(page, "page", page) or page) for page in conservar
    }
    for page in list(browser.pages):
        if id(page) in conservar_ids:
            continue
        host = (urlsplit(page.url).hostname or "").lower()
        if host == "fuvexbn.a365.com.pe" or host == "accounts.google.com":
            try:
                await page.close()
            except Exception:
                pass


async def _abrir_pagina_con_sesion(page, browser, url, ruta_esperada, recargar=False):
    """Abre un módulo y señala al coordinador si Fuvex requiere autenticación."""
    if page.is_closed():
        try:
            page = await browser.new_page()
        except Exception as exc:
            raise ContextoNavegadorCerradoError(
                "La pestaña se cerro y no se pudo abrir otra. "
                "Comprueba que Brave siga abierto."
            ) from exc

    ruta = ruta_esperada.lower()
    if not recargar and ruta in page.url.lower():
        return page
    if not recargar:
        for frame in getattr(page, "frames", []):
            if ruta in frame.url.lower():
                return frame

    await abrir_pagina(page, url)
    if ruta in page.url.lower():
        return page
    for frame in getattr(page, "frames", []):
        if ruta in frame.url.lower():
            return frame

    if await pagina_inicio_sesion_fuvex(page):
        raise SesionFuvexExpiradaError(
            f"Fuvex requiere volver a iniciar sesión antes de abrir {ruta_esperada}."
        )

    if MENU_PATTERN.search(page.url):
        modulo, enlaces = await abrir_modulo_desde_menu(page, browser, ruta_esperada)
        if modulo:
            return modulo
        detalle = " | ".join(enlaces) or "el menú no expone enlaces"
        raise RuntimeError(
            f"Fuvex redirigio al menu y no pude abrir el módulo {ruta_esperada}. "
            f"URL: {page.url}. Enlaces visibles: {detalle}"
        )

    raise RuntimeError(
        f"No se abrió {ruta_esperada}; Fuvex quedó en {page.url}."
    )


async def abrir_pagina_con_sesion(
    page,
    browser,
    url,
    ruta_esperada,
    recargar=False,
    session_lock=None,
    sesion_requerida=None,
):
    if sesion_requerida is not None:
        await esperar_sesion_fuvex(browser, sesion_requerida, session_lock)
    if session_lock is None:
        return await _abrir_pagina_con_sesion(
            page, browser, url, ruta_esperada, recargar
        )
    async with session_lock:
        return await _abrir_pagina_con_sesion(
            page, browser, url, ruta_esperada, recargar
        )


def crear_limitador_envios(intervalo_minimo, tipo_envio):
    """Espacia los envios de un solo tipo sin mezclar ambos ciclos."""
    lock = asyncio.Lock()
    estado: dict[str, float | None] = {"ultimo_envio": None}

    async def esperar_turno():
        async with lock:
            loop = asyncio.get_running_loop()
            ultimo_envio = estado["ultimo_envio"]
            if ultimo_envio is not None:
                espera = intervalo_minimo - (loop.time() - ultimo_envio)
                if espera > 0:
                    print(
                        f"[*] Pausa de {espera:.0f} segundos antes del {tipo_envio}."
                    )
                    await asyncio.sleep(espera)
            estado["ultimo_envio"] = loop.time()

    return esperar_turno


async def enviar_filtro_con_espera(
    page,
    browser,
    dni,
    session_lock=None,
    antes_de_enviar=None,
    sesion_requerida=None,
):
    """Reintenta el mismo DNI cada minuto si la cola de Fuvex esta llena."""
    while True:
        print(f"[*] Recargando el formulario antes del filtro para {dni}.")
        page = await abrir_pagina_con_sesion(
            page,
            browser,
            FILTER_URL,
            "ingresar_filtro.php",
            recargar=True,
            session_lock=session_lock,
            sesion_requerida=sesion_requerida,
        )
        try:
            await filtrar_dni(page, dni, antes_de_enviar)
            return page
        except ColaEsperaLlenaError as exc:
            print(
                f"[!] La cola de espera de Fuvex esta llena para {dni}: {exc}. "
                "No se marcara como enviado; recargare y reintentare en 60 segundos."
            )
            await asyncio.sleep(60)


async def abrir_modulo_desde_menu(page, browser, ruta_esperada, _reintento_google=False):
    """Abre el enlace del menu aunque viva en otra pestana o dentro de un frame."""
    ruta = ruta_esperada.lower()
    selector = "a[href], button[onclick], [role='menuitem']"
    metadata_js = """els => els.map((e, i) => ({index: i,
        href: e.getAttribute('href') || '', absoluteHref: e.href || '',
        onclick: e.getAttribute('onclick') || '', text: (e.innerText || '').trim(),
        title: e.getAttribute('title') || '', target: e.getAttribute('target') || ''}))"""
    descripciones, errores = [], []

    contextos = []
    for candidate in list(browser.pages):
        contextos.append(candidate)
        contextos.extend(candidate.frames)
    contextos.extend(getattr(page, "frames", []))
    contextos = list({id(ctx): ctx for ctx in contextos}.values())

    for contexto in contextos:
        try:
            elementos = await contexto.locator(selector).evaluate_all(metadata_js)
        except Exception:
            continue
        coincidencias = [e for e in elementos if ruta in (
            e['href'] + ' ' + e['absoluteHref'] + ' ' + e['onclick']
        ).lower()]
        if not coincidencias:
            continue
        descripciones.extend(
            f"{e['text'] or e['title'] or '(sin texto)'} -> "
            f"{e['href'] or e['onclick']} [target={e['target'] or '_self'}]"
            for e in elementos[:30]
        )

        for item in coincidencias:
            locator = contexto.locator(
                f"a[href*='{ruta}' i], button[onclick*='{ruta}' i], "
                f"[role='menuitem'][href*='{ruta}' i]"
            ).first
            if not await locator.count():
                locator = contexto.locator(selector).nth(item['index'])
            try:
                antes = set(browser.pages)
                if await locator.is_visible():
                    await locator.click(timeout=10000)
                else:
                    if "ingresar_filtro.php" in ruta or "lista_filtros_f.php" in ruta:
                        seccion = contexto.locator("a[href='#Filtros']").first
                        if await seccion.count() and await seccion.is_visible():
                            await seccion.click(timeout=5000)
                    try:
                        await locator.wait_for(state="visible", timeout=1500)
                        await locator.click(timeout=10000)
                    except Exception:
                        # El elemento puede seguir oculto dentro de un menu colapsado.
                        await locator.evaluate("(elemento) => elemento.click()")
            except Exception as exc:
                errores.append(f"{item['href'] or item['onclick']}: {exc}")
                continue

            vio_google = False
            timeout = 30
            inicio_espera = asyncio.get_running_loop().time()
            while True:
                for candidate in list(browser.pages):
                    if ruta in candidate.url.lower():
                        return candidate, list(dict.fromkeys(descripciones))
                    for frame in candidate.frames:
                        if ruta in frame.url.lower():
                            return frame, list(dict.fromkeys(descripciones))
                    if (urlsplit(candidate.url).hostname or "").lower() == "accounts.google.com":
                        if not vio_google:
                            print("[*] Google solicita confirmar la cuenta en Brave; esperando el retorno a Fuvex.")
                            vio_google = True
                            timeout = intervalo_aviso_google()
                            inicio_espera = asyncio.get_running_loop().time()
                for candidate in browser.pages:
                    if candidate not in antes and ruta in candidate.url.lower():
                        return candidate, list(dict.fromkeys(descripciones))
                if vio_google and not _reintento_google:
                    menu = None
                    for candidate in browser.pages:
                        if (
                            MENU_PATTERN.search(candidate.url)
                            and not await pagina_pide_google(candidate)
                        ):
                            menu = candidate
                            break
                    if menu:
                        return await abrir_modulo_desde_menu(
                            menu, browser, ruta_esperada, _reintento_google=True
                        )
                transcurrido = asyncio.get_running_loop().time() - inicio_espera
                if transcurrido >= timeout:
                    if not vio_google:
                        break
                    urls = ", ".join(candidate.url for candidate in browser.pages)
                    print(
                        "[!] Google todavía no devolvió el acceso a Fuvex. "
                        f"Completa la autenticación en Brave. Pestañas: {urls}"
                    )
                    inicio_espera = asyncio.get_running_loop().time()
                await asyncio.sleep(0.5)
            urls = ', '.join(candidate.url for candidate in browser.pages)
            errores.append(f"el enlace se pulso, pero no aparecio {ruta_esperada}; pestanas: {urls}")

    if not descripciones:
        descripciones.append('No encontre el enlace en ninguna pestana o frame de Fuvex.')
    return None, list(dict.fromkeys(descripciones + errores))


async def proceso_registros(
    pagina_lista,
    pagina_registro,
    pagina_operadora,
    browser,
    intervalo_revision,
    dnis_aprobados,
    registro_inicializado,
    session_lock,
    antes_de_enviar,
    sesion_requerida,
):
    """Revisa aprobados y registra prospectos de forma independiente."""
    dnis_verdes_atendidos = set()
    while True:
        try:
            await esperar_sesion_fuvex(browser, sesion_requerida, session_lock)
            pagina_lista = await abrir_pagina_con_sesion(
                pagina_lista,
                browser,
                FILTER_LIST_URL,
                "lista_filtros_f.php",
                session_lock=session_lock,
                sesion_requerida=sesion_requerida,
            )
            dnis_verdes = await dnis_con_filtro_correcto(pagina_lista)
            dnis_aprobados.update(dnis_verdes)
            registro_inicializado.set()
            filas_verdes = []
            for dni in dnis_verdes:
                if dni in dnis_verdes_atendidos:
                    continue
                fila = await asyncio.to_thread(obtener_pendiente_por_dni, dni)
                if fila:
                    filas_verdes.append(fila)
                if len(filas_verdes) == 2:
                    break
            print(
                f"[*] Proceso de registro: {len(dnis_verdes)} filtros aprobados; "
                f"{len(filas_verdes)} clientes pendientes."
            )
        except SesionFuvexExpiradaError as exc:
            sesion_requerida.set()
            registro_inicializado.set()
            print(f"[!] {exc}")
            await esperar_sesion_fuvex(browser, sesion_requerida, session_lock)
            continue
        except ContextoNavegadorCerradoError:
            registro_inicializado.set()
            raise
        except Exception as exc:
            registro_inicializado.set()
            print(f"[!] No pude revisar filtros aprobados: {exc}")
            await asyncio.sleep(10)
            continue

        operadores_validos = {
            "MOVISTAR": "Movistar",
            "ENTEL": "Entel",
            "CLARO": "Claro",
            "BITEL": "Bitel",
        }
        for cliente in filas_verdes:
            dni = str(cliente["dni"]).strip()
            try:
                operador_detectado = await consultar_operadora(
                    pagina_operadora, cliente.get("celular", "")
                )
            except Exception as exc:
                print(f"[!] No pude consultar la operadora del DNI {dni}: {exc}")
                continue

            operador = operadores_validos.get(operador_detectado)
            if operador is None:
                print(
                    f"[!] HeyLocate no dio una operadora verificable para DNI {dni}; "
                    "se volvera a intentar en el proximo ciclo."
                )
                continue

            try:
                pagina_registro = await abrir_pagina_con_sesion(
                    pagina_registro,
                    browser,
                    REGISTRO_URL,
                    "registro_prospecto_bn.php",
                    session_lock=session_lock,
                    sesion_requerida=sesion_requerida,
                )
                registrado = await registrar_prospecto(
                    pagina_registro,
                    dni,
                    cliente.get("nombre", ""),
                    cliente.get("celular", ""),
                    operador,
                    antes_de_enviar,
                )
            except ClienteOmitidoError as exc:
                dnis_verdes_atendidos.add(dni)
                print(f"[*] Se omite el DNI {dni} y se continua: {exc}")
                continue
            except RegistroRechazadoError as exc:
                print(f"[!] {exc}; se revisara de nuevo en el siguiente ciclo.")
                continue
            except ConfirmacionExitoAnteriorError as exc:
                dnis_verdes_atendidos.add(dni)
                print(
                    f"[!] {exc} No marcaré ni enviaré automáticamente el DNI {dni}; "
                    "verifica en la lista de ventas a qué prospecto corresponde."
                )
                try:
                    pagina_registro = await abrir_pagina_con_sesion(
                        pagina_registro,
                        browser,
                        REGISTRO_URL,
                        "registro_prospecto_bn.php",
                        recargar=True,
                        session_lock=session_lock,
                        sesion_requerida=sesion_requerida,
                    )
                    print("[*] Formulario recargado tras cerrar la confirmación previa.")
                except Exception as error_recarga:
                    print(
                        "[!] No pude recargar el formulario después de cerrar "
                        f"la confirmación previa: {error_recarga}"
                    )
                continue
            except RegistroBloqueadoPorDialogoError as exc:
                print(f"[!] {exc}")
                await esperar_cierre_dialogo_fuvex(pagina_registro)
                continue
            except SesionFuvexExpiradaError as exc:
                dnis_verdes_atendidos.add(dni)
                sesion_requerida.set()
                print(
                    f"[!] {exc} El resultado puede ser incierto; verifica este DNI "
                    "en Fuvex antes de volver a procesarlo."
                )
                await esperar_sesion_fuvex(browser, sesion_requerida, session_lock)
                break
            except RegistroSinConfirmarError as exc:
                dnis_verdes_atendidos.add(dni)
                print(
                    f"[!] {exc}. No lo reintentare durante esta ejecucion para "
                    "evitar un posible registro duplicado; verifica el resultado en Fuvex."
                )
                await esperar_cierre_dialogo_fuvex(pagina_registro)
                continue
            except ContextoNavegadorCerradoError:
                raise
            except Exception as exc:
                print(f"[!] Fallo el registro del DNI {dni}: {exc}")
                continue

            if registrado:
                dnis_verdes_atendidos.add(dni)
                try:
                    await asyncio.to_thread(actualizar_estado_dni, dni, "registrado")
                    print(f"[*] Prospecto {dni} registrado y estado actualizado.")
                except Exception as exc:
                    print(
                        f"[!] El prospecto {dni} se registro en Fuvex, pero Supabase "
                        f"no se actualizo: {exc}"
                    )
                try:
                    pagina_registro = await abrir_pagina_con_sesion(
                        pagina_registro,
                        browser,
                        REGISTRO_URL,
                        "registro_prospecto_bn.php",
                        recargar=True,
                        session_lock=session_lock,
                        sesion_requerida=sesion_requerida,
                    )
                    print(
                        "[*] Formulario de registro recargado para limpiar "
                        "la confirmacion antes del siguiente prospecto."
                    )
                except Exception as exc:
                    print(
                        f"[!] Prospecto {dni} ya fue registrado, pero no pude "
                        f"recargar el formulario: {exc}. Se intentara recuperar "
                        "antes del siguiente registro."
                    )
            else:
                print(f"[!] Fuvex no confirmo el registro de {dni}; se revisara de nuevo.")

        await asyncio.sleep(intervalo_revision)


async def main():
    brave_exe = encontrar_brave()
    if brave_exe is None:
        raise FileNotFoundError("No se encontro brave.exe. Configura BRAVE_PATH en .env.")
    if not BRAVE_USER_DATA.is_dir():
        raise FileNotFoundError(f"No se encontro el perfil de Brave: {BRAVE_USER_DATA}")

    async with async_playwright() as p:
        # Cierra Brave antes de ejecutar: el perfil persistente no puede abrirse dos veces.
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(BRAVE_USER_DATA),
            executable_path=str(brave_exe),
            headless=False,
        )
        proceso_registro = None
        try:
            paginas = list(browser.pages)
            pagina_inicio = next(
                (
                    page for page in paginas
                    if (urlsplit(page.url).hostname or "").lower()
                    == "fuvexbn.a365.com.pe"
                ),
                None,
            )
            if pagina_inicio is None:
                pagina_inicio = next(
                    (
                        page for page in paginas
                        if (urlsplit(page.url).hostname or "").lower()
                        != "accounts.google.com"
                    ),
                    None,
                )
            if pagina_inicio is None:
                pagina_inicio = await browser.new_page()

            await limpiar_pestanas_fuvex(browser, conservar=(pagina_inicio,))
            pagina_filtro = await iniciar_sesion(pagina_inicio, browser)
            await limpiar_pestanas_fuvex(browser, conservar=(pagina_filtro,))
            pagina_lista = await browser.new_page()
            pagina_registro = await browser.new_page()
            pagina_operadora = await browser.new_page()
            pagina_filtro = await abrir_pagina_con_sesion(
                pagina_filtro, browser, FILTER_URL, "ingresar_filtro.php"
            )
            pagina_lista = await abrir_pagina_con_sesion(
                pagina_lista, browser, FILTER_LIST_URL, "lista_filtros_f.php"
            )
            pagina_registro = await abrir_pagina_con_sesion(
                pagina_registro,
                browser,
                "https://fuvexbn.a365.com.pe:7443/BN/Registro_Prospecto_BN.php",
                "registro_prospecto_bn.php",
            )
            await limpiar_pestanas_fuvex(
                browser,
                conservar=(
                    pagina_filtro,
                    pagina_lista,
                    pagina_registro,
                    pagina_operadora,
                ),
            )
            print("[*] Fuvex abierto. Filtrado y registro usaran ciclos independientes.")

            dnis_enviados_en_esta_sesion = set()
            intervalo_revision = max(30, int(os.getenv("FUVEX_INTERVAL_SECONDS", "60")))
            dnis_aprobados = set()
            registro_inicializado = asyncio.Event()
            sesion_requerida = asyncio.Event()
            session_lock = asyncio.Lock()
            intervalo_entre_filtros = max(
                10.0, float(os.getenv("FUVEX_FILTER_DELAY_SECONDS", "15"))
            )
            intervalo_entre_registros = max(
                60.0, float(os.getenv("FUVEX_REGISTRATION_DELAY_SECONDS", "60"))
            )
            esperar_filtro = crear_limitador_envios(
                intervalo_entre_filtros, "filtro"
            )
            esperar_registro = crear_limitador_envios(
                intervalo_entre_registros, "registro"
            )
            proceso_registro = asyncio.create_task(
                proceso_registros(
                    pagina_lista,
                    pagina_registro,
                    pagina_operadora,
                    browser,
                    intervalo_revision,
                    dnis_aprobados,
                    registro_inicializado,
                    session_lock,
                    esperar_registro,
                    sesion_requerida,
                )
            )
            print("[*] Proceso de registro ejecutandose en paralelo al filtrado.")
            await registro_inicializado.wait()
            if proceso_registro.done():
                error = proceso_registro.exception()
                if error is not None:
                    raise error
                raise RuntimeError("El proceso de registro terminó inesperadamente.")
            fallos_navegacion = 0
            while True:
                if proceso_registro.done():
                    error = proceso_registro.exception()
                    if error is not None:
                        raise error
                    raise RuntimeError("El proceso de registro terminó inesperadamente.")
                await esperar_sesion_fuvex(browser, sesion_requerida, session_lock)
                try:
                    clientes = await asyncio.to_thread(
                        obtener_pendientes,
                        dnis_enviados_en_esta_sesion | dnis_aprobados,
                        cantidad=2,
                    )
                except Exception as exc:
                    if "No encuentro una columna de monto" in str(exc):
                        raise
                    print(f"[!] Error al leer Supabase: {exc}")
                    await asyncio.sleep(10)
                    continue

                if not clientes:
                    print("[*] Proceso de filtros: no hay pendientes; revisare en 60 segundos.")
                    await asyncio.sleep(60)
                    continue

                print(f"[*] Supabase entrego {len(clientes)} clientes pendientes validos.")

                pausa_por_filtros_agotados = False
                for cliente in clientes:
                    await esperar_sesion_fuvex(browser, sesion_requerida, session_lock)
                    cliente_dni = str(cliente.get("dni", "")).strip()
                    try:
                        pagina_filtro = await enviar_filtro_con_espera(
                            pagina_filtro,
                            browser,
                            cliente_dni,
                            session_lock,
                            esperar_filtro,
                            sesion_requerida,
                        )
                        fallos_navegacion = 0
                    except ClienteOmitidoError as exc:
                        fallos_navegacion = 0
                        dnis_enviados_en_esta_sesion.add(cliente_dni)
                        print(
                            f"[*] Se omite el DNI {cliente_dni} y se continua con el "
                            f"siguiente, sin modificar Supabase: {exc}"
                        )
                        continue
                    except FiltrosNoDisponiblesError as exc:
                        fallos_navegacion = 0
                        pausa_por_filtros_agotados = True
                        print(
                            f"[!] Fuvex no acepta mas filtros por ahora. Se pausa "
                            f"la tanda y se revisara de nuevo en el siguiente ciclo: {exc}"
                        )
                        break
                    except SesionFuvexExpiradaError as exc:
                        fallos_navegacion = 0
                        sesion_requerida.set()
                        print(f"[!] {exc}")
                        await esperar_sesion_fuvex(browser, sesion_requerida, session_lock)
                        break
                    except ValueError as exc:
                        fallos_navegacion = 0
                        dnis_enviados_en_esta_sesion.add(cliente_dni)
                        print(f"[!] Se omitio un registro con DNI invalido: {exc}")
                        continue
                    except FiltroSinConfirmarError as exc:
                        fallos_navegacion = 0
                        dnis_enviados_en_esta_sesion.add(cliente_dni)
                        print(f"[!] Resultado de filtro incierto para {cliente_dni}: {exc}")
                        continue
                    except Exception as exc:
                        if isinstance(exc, ContextoNavegadorCerradoError):
                            raise
                        fallos_navegacion += 1
                        intento_cerrado = (
                            "Target page, context or browser has been closed"
                            in str(exc)
                        )
                        if intento_cerrado and pagina_filtro.is_closed():
                            try:
                                pagina_filtro = await browser.new_page()
                            except Exception as error_pestana:
                                raise ContextoNavegadorCerradoError(
                                    "Brave o su contexto de automatizacion se cerro. "
                                    "Reabre Brave y reinicia el programa."
                                ) from error_pestana
                        print(
                            f"[!] No se abrio la pagina de filtro para {cliente_dni}; "
                            f"se reintentara en el siguiente ciclo: {exc}"
                        )
                        espera = min(30 * (2 ** min(fallos_navegacion - 1, 4)), 480)
                        print(
                            f"[*] Pausa de {espera} segundos antes de volver a "
                            "consultar Fuvex."
                        )
                        await asyncio.sleep(espera)
                        break
                    dnis_enviados_en_esta_sesion.add(cliente_dni)
                    try:
                        await asyncio.to_thread(
                            actualizar_estado_dni, cliente_dni, "filtrado"
                        )
                    except Exception as exc:
                        print(
                            f"[!] Fuvex recibio el filtro de {cliente_dni}, pero no pude "
                            f"actualizar Supabase: {exc}. No lo reenviere en esta sesion."
                        )
                    print("[*] Filtro enviado; el resultado queda en Lista_Filtros_F.")

                if pausa_por_filtros_agotados:
                    print(
                        "[*] Proceso de filtros: consultare Supabase de nuevo en "
                        f"{intervalo_revision} segundos."
                    )
                    await asyncio.sleep(intervalo_revision)
                    continue

                await asyncio.sleep(1)
        finally:
            if proceso_registro is not None:
                proceso_registro.cancel()
                await asyncio.gather(proceso_registro, return_exceptions=True)
            try:
                await browser.close()
            except Exception:
                # Brave pudo haberse cerrado manualmente durante la revision.
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Script detenido por el usuario.")
    except ContextoNavegadorCerradoError as exc:
        print(f"[!] {exc}")
