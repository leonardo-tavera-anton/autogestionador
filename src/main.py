import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

from src.db import actualizar_estado_dni, obtener_pendiente_por_dni, obtener_pendientes
from src.scraper import (
    FILTER_LIST_URL,
    FILTER_URL,
    FiltroSinConfirmarError,
    abrir_pagina,
    dnis_con_filtro_correcto,
    filtrar_dni,
    buscar_evidencia_cliente,
    registrar_prospecto,
)

LOGIN_URL = "https://fuvexbn.a365.com.pe:7443/index.php"
MENU_PATTERN = re.compile(r"/menu\.php(?:$|[?#])", re.I)
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
BRAVE_USER_DATA = Path(
    os.getenv("BRAVE_USER_DATA", str(LOCAL_APP_DATA / "BraveSoftware" / "Brave-Browser" / "User Data"))
)


def encontrar_brave():
    configurado = os.getenv("BRAVE_PATH")
    posibles_rutas = [
        Path(configurado) if configurado else None,
        LOCAL_APP_DATA / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path(r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    return next((ruta for ruta in posibles_rutas if ruta and ruta.is_file()), None)


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
    espera_google = max(60, int(os.getenv("GOOGLE_LOGIN_TIMEOUT_SECONDS", "600")))
    print("[*] Google espera que elijas la cuenta autorizada en Brave y completes cualquier verificacion.")

    # Google puede abrir otra pestaña o completar el acceso en la misma.
    for _ in range(espera_google):
        for candidate in browser.pages:
            if MENU_PATTERN.search(candidate.url) and not await pagina_pide_google(candidate):
                try:
                    await candidate.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    # El callback puede completar la navegacion antes de emitir load.
                    pass
                await candidate.wait_for_timeout(750)
                return candidate
        await asyncio.sleep(1)

    urls = ", ".join(candidate.url for candidate in browser.pages)
    raise TimeoutError(f"No se completo el acceso a Fuvex en {espera_google} s. Pestanas abiertas: {urls}")


async def pagina_pide_google(page):
    """Detecta el menu que aun requiere completar el acceso mediante Google."""
    if (urlsplit(page.url).hostname or "").lower() == "accounts.google.com":
        return True
    boton = page.get_by_text(re.compile(r"Iniciar sesi.n con Google", re.I)).first
    try:
        return await boton.count() > 0 and await boton.is_visible()
    except Exception:
        return False


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


async def abrir_pagina_con_sesion(page, browser, url, ruta_esperada):
    """Abre una página de Fuvex y renueva el acceso si hubo redirección al login."""
    ruta = ruta_esperada.lower()
    if ruta in page.url.lower():
        return page
    for frame in getattr(page, "frames", []):
        if ruta in frame.url.lower():
            return frame

    await abrir_pagina(page, url)
    if ruta in page.url.lower():
        return page
    for frame in getattr(page, "frames", []):
        if ruta in frame.url.lower():
            return frame

    if MENU_PATTERN.search(page.url):
        if await pagina_pide_google(page):
            page = await iniciar_sesion(page, browser)
        modulo, enlaces = await abrir_modulo_desde_menu(page, browser, ruta_esperada)
        if modulo:
            return modulo
        detalle = " | ".join(enlaces) or "el menú no expone enlaces"
        raise RuntimeError(
            f"Fuvex redirigio al menu y no pude abrir el módulo {ruta_esperada}. "
            f"URL: {page.url}. Enlaces visibles: {detalle}"
        )

    print(f"[!] Fuvex redirigio a {page.url}; intentando recuperar la sesion.")
    page = await iniciar_sesion(page, browser)
    await abrir_pagina(page, url)
    if ruta not in page.url.lower():
        for frame in getattr(page, "frames", []):
            if ruta in frame.url.lower():
                return frame
        if MENU_PATTERN.search(page.url):
            if await pagina_pide_google(page):
                page = await iniciar_sesion(page, browser)
            modulo, enlaces = await abrir_modulo_desde_menu(
                page, browser, ruta_esperada
            )
            if modulo:
                return modulo
            detalle = " | ".join(enlaces) or "el menú no expone enlaces"
            raise RuntimeError(
                f"La sesión llegó al menú, pero no se encontró el módulo "
                f"{ruta_esperada}. Enlaces visibles: {detalle}"
            )
        raise RuntimeError(
            f"No se pudo abrir {url}. URL final: {page.url}; titulo: {await page.title()}"
        )
    return page


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
                if not await locator.is_visible() and ('ingresar_filtro.php' in ruta or 'lista_filtros_f.php' in ruta):
                    seccion = contexto.locator("a[href='#Filtros']").first
                    if await seccion.count() and await seccion.is_visible():
                        await seccion.click(timeout=5000)
                    await locator.wait_for(state='visible', timeout=5000)
                antes = set(browser.pages)
                await locator.click(timeout=10000)
            except Exception as exc:
                errores.append(f"{item['href'] or item['onclick']}: {exc}")
                continue

            vio_google = False
            timeout = 30
            inicio_espera = asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() - inicio_espera < timeout:
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
                            timeout = max(60, int(os.getenv("GOOGLE_LOGIN_TIMEOUT_SECONDS", "600")))
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
                await asyncio.sleep(0.5)
            urls = ', '.join(candidate.url for candidate in browser.pages)
            errores.append(f"el enlace se pulso, pero no aparecio {ruta_esperada}; pestanas: {urls}")

    if not descripciones:
        descripciones.append('No encontre el enlace en ninguna pestana o frame de Fuvex.')
    return None, list(dict.fromkeys(descripciones + errores))


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
                conservar=(pagina_filtro, pagina_lista, pagina_registro),
            )
            print("[*] Fuvex abierto en tres pestanas: filtrar, revisar lista y registrar.")

            dnis_enviados_en_esta_sesion = set()
            dnis_verdes_atendidos = set()
            dnis_sin_evidencia_reportados = set()
            intervalo_revision = max(30, int(os.getenv("FUVEX_INTERVAL_SECONDS", "180")))
            espera_resultado = max(300, int(os.getenv("FUVEX_RESULT_WAIT_SECONDS", "300")))
            while True:
                try:
                    pagina_lista = await abrir_pagina_con_sesion(
                        pagina_lista, browser, FILTER_LIST_URL, "lista_filtros_f.php"
                    )
                    dnis_verdes = await dnis_con_filtro_correcto(pagina_lista)
                    filas_verdes = []
                    for dni_verde in dnis_verdes:
                        if dni_verde in dnis_verdes_atendidos:
                            continue
                        fila = obtener_pendiente_por_dni(dni_verde)
                        if fila:
                            filas_verdes.append(fila)
                        if len(filas_verdes) == 2:
                            break
                    print(
                        f"[*] Resultados verdes en la lista: {len(dnis_verdes)}; "
                        f"asociados a clientes pendientes: {len(filas_verdes)}."
                    )
                except Exception as exc:
                    print(f"[!] No pude revisar filtros verdes existentes: {exc}")
                    filas_verdes = []

                if filas_verdes:
                    for cliente in filas_verdes:
                        cliente_dni = str(cliente["dni"]).strip()
                        if buscar_evidencia_cliente(cliente_dni) is None:
                            if cliente_dni not in dnis_sin_evidencia_reportados:
                                print(
                                    f"[!] DNI {cliente_dni}: falta su evidencia individual en "
                                    "assets/documentos/{DNI}.pdf/.jpg/.jpeg/.png. "
                                    "assets/documento_requerido.jpeg no corresponde a este cliente; "
                                    "se omitira el registro."
                                )
                                dnis_sin_evidencia_reportados.add(cliente_dni)
                            continue
                        dnis_sin_evidencia_reportados.discard(cliente_dni)
                        decision = await asyncio.to_thread(
                            input,
                            "Fuvex muestra VALIDACION CORRECTO para un filtro existente. "
                            "Escribe REGISTRAR para continuar o S para omitir: ",
                        )
                        if decision.strip().upper() != "REGISTRAR":
                            dnis_verdes_atendidos.add(cliente_dni)
                            continue
                        operadores_validos = {
                            "MOVISTAR": "Movistar",
                            "ENTEL": "Entel",
                            "CLARO": "Claro",
                            "BITEL": "Bitel",
                        }
                        while True:
                            operador_ingresado = await asyncio.to_thread(
                                input,
                                "Operadora confirmada (Movistar/Entel/Claro/Bitel): ",
                            )
                            operador = operadores_validos.get(
                                operador_ingresado.strip().upper()
                            )
                            if operador:
                                break
                            print("[!] Ingresa una de las operadoras mostradas en FUVEX.")
                        try:
                            pagina_registro = await abrir_pagina_con_sesion(
                                pagina_registro,
                                browser,
                                "https://fuvexbn.a365.com.pe:7443/BN/Registro_Prospecto_BN.php",
                                "registro_prospecto_bn.php",
                            )
                            registrado = await registrar_prospecto(
                                pagina_registro,
                                cliente_dni,
                                cliente.get("nombre", ""),
                                cliente.get("celular", ""),
                                cliente.get("monto", ""),
                                operador,
                            )
                        except Exception as exc:
                            print(f"[!] Fallo el formulario para DNI {cliente_dni}: {exc}")
                            continue

                        if registrado:
                            dnis_verdes_atendidos.add(cliente_dni)
                            try:
                                actualizar_estado_dni(cliente_dni, "registrado")
                                print("[*] Prospecto registrado y estado actualizado en Supabase.")
                            except Exception as exc:
                                print(
                                    f"[!] El prospecto {cliente_dni} se registro en Fuvex, "
                                    f"pero no pude actualizar Supabase: {exc}"
                                )
                        else:
                            print("[!] Fuvex no confirmo el registro; queda pendiente de revision.")
                    await asyncio.sleep(intervalo_revision)
                    pagina_lista = await abrir_pagina_con_sesion(
                        pagina_lista, browser, FILTER_LIST_URL, "lista_filtros_f.php"
                    )
                    continue

                try:
                    clientes = obtener_pendientes(
                        dnis_enviados_en_esta_sesion, cantidad=2
                    )
                except Exception as exc:
                    if "No encuentro una columna de monto" in str(exc):
                        raise
                    print(f"[!] Error al leer Supabase: {exc}")
                    await asyncio.sleep(10)
                    continue

                if not clientes:
                    print("[*] No hay filas validas pendientes. Revisare ambas pestanas en 3 minutos.")
                    await asyncio.sleep(intervalo_revision)
                    pagina_lista = await abrir_pagina_con_sesion(
                        pagina_lista, browser, FILTER_LIST_URL, "lista_filtros_f.php"
                    )
                    pagina_filtro = await abrir_pagina_con_sesion(
                        pagina_filtro, browser, FILTER_URL, "ingresar_filtro.php"
                    )
                    continue

                print(f"[*] Supabase entrego {len(clientes)} clientes pendientes validos.")

                for cliente in clientes:
                    cliente_dni = str(cliente.get("dni", "")).strip()
                    try:
                        pagina_filtro = await abrir_pagina_con_sesion(
                            pagina_filtro, browser, FILTER_URL, "ingresar_filtro.php"
                        )
                    except Exception as exc:
                        print(
                            f"[!] No se abrio la pagina de filtro para {cliente_dni}; "
                            f"se reintentara en el siguiente ciclo: {exc}"
                        )
                        break

                    try:
                        await filtrar_dni(pagina_filtro, cliente_dni)
                    except ValueError as exc:
                        dnis_enviados_en_esta_sesion.add(cliente_dni)
                        print(f"[!] Se omitio un registro con DNI invalido: {exc}")
                        continue
                    except FiltroSinConfirmarError as exc:
                        # No reenviar este DNI en esta sesion: Fuvex pudo procesarlo,
                        # aunque la confirmacion no haya llegado al navegador.
                        dnis_enviados_en_esta_sesion.add(cliente_dni)
                        print(f"[!] Resultado de filtro incierto para {cliente_dni}: {exc}")
                        continue
                    except Exception as exc:
                        print(
                            f"[!] No se completo el filtro de {cliente_dni}: {exc}. "
                            "No se marcara como enviado; se reintentara en el siguiente ciclo."
                        )
                        break

                    dnis_enviados_en_esta_sesion.add(cliente_dni)
                    try:
                        actualizar_estado_dni(cliente_dni, "filtrado")
                    except Exception as exc:
                        print(
                            f"[!] Fuvex recibio el filtro de {cliente_dni}, pero no pude "
                            f"actualizar Supabase: {exc}. No lo reenviere en esta sesion."
                        )
                    print("[*] Filtro enviado; el resultado queda en Lista_Filtros_F.")

                print(
                    f"[*] Tanda enviada. Revisare la lista en {intervalo_revision} "
                    f"segundos; el siguiente lote espera {espera_resultado} segundos."
                )
                await asyncio.sleep(intervalo_revision)
                await dnis_con_filtro_correcto(pagina_lista)
                restante = espera_resultado - intervalo_revision
                if restante > 0:
                    await asyncio.sleep(restante)
                pagina_lista = await abrir_pagina_con_sesion(
                    pagina_lista, browser, FILTER_LIST_URL, "lista_filtros_f.php"
                )
        finally:
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
