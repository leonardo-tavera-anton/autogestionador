import asyncio
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

from src.db import actualizar_estado_dni, obtener_pendiente_por_dni, obtener_pendientes
from src.scraper import (
    FILTER_LIST_URL,
    FILTER_URL,
    abrir_pagina,
    dnis_con_filtro_correcto,
    filtrar_dni,
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
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    if MENU_PATTERN.search(page.url):
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
            if MENU_PATTERN.search(candidate.url):
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
            pagina_inicio = browser.pages[0] if browser.pages else await browser.new_page()
            pagina_filtro = await iniciar_sesion(pagina_inicio, browser)
            pagina_lista = await browser.new_page()
            pagina_registro = await browser.new_page()
            await abrir_pagina(pagina_filtro, FILTER_URL)
            await abrir_pagina(pagina_lista, FILTER_LIST_URL)
            await abrir_pagina(
                pagina_registro,
                "https://fuvexbn.a365.com.pe:7443/BN/Registro_Prospecto_BN.php",
            )
            print("[*] Fuvex abierto en tres pestanas: filtrar, revisar lista y registrar.")

            consultados_en_esta_sesion = set()
            intervalo_revision = max(30, int(os.getenv("FUVEX_INTERVAL_SECONDS", "180")))
            espera_resultado = max(300, int(os.getenv("FUVEX_RESULT_WAIT_SECONDS", "300")))
            while True:
                try:
                    dnis_verdes = await dnis_con_filtro_correcto(pagina_lista)
                    filas_verdes = []
                    for dni_verde in dnis_verdes:
                        if dni_verde in consultados_en_esta_sesion:
                            continue
                        fila = obtener_pendiente_por_dni(dni_verde)
                        if fila:
                            filas_verdes.append(fila)
                        if len(filas_verdes) == 2:
                            break
                except Exception as exc:
                    print(f"[!] No pude revisar filtros verdes existentes: {exc}")
                    filas_verdes = []

                if filas_verdes:
                    for cliente in filas_verdes:
                        cliente_dni = str(cliente["dni"]).strip()
                        decision = await asyncio.to_thread(
                            input,
                            "Fuvex muestra VALIDACION CORRECTO para un filtro existente. "
                            "Escribe REGISTRAR para continuar o S para omitir: ",
                        )
                        consultados_en_esta_sesion.add(cliente_dni)
                        if decision.strip().upper() != "REGISTRAR":
                            continue
                        operador = await asyncio.to_thread(
                            input,
                            "Ingresa la operadora confirmada por una fuente autorizada: ",
                        )
                        if await registrar_prospecto(
                            pagina_registro,
                            cliente_dni,
                            cliente.get("nombre", ""),
                            cliente.get("celular", ""),
                            cliente.get("monto", ""),
                            operador.strip(),
                        ):
                            actualizar_estado_dni(cliente_dni, "registrado")
                            print("[*] Prospecto registrado y estado actualizado en Supabase.")
                        else:
                            print("[!] Fuvex no confirmo el registro; queda pendiente de revision.")
                    await asyncio.sleep(intervalo_revision)
                    await abrir_pagina(pagina_lista, FILTER_LIST_URL)
                    continue

                try:
                    clientes = obtener_pendientes(consultados_en_esta_sesion, cantidad=2)
                except Exception as exc:
                    if "No encuentro una columna de monto" in str(exc):
                        raise
                    print(f"[!] Error al leer Supabase: {exc}")
                    await asyncio.sleep(10)
                    continue

                if not clientes:
                    print("[*] No hay filas validas pendientes. Revisare ambas pestanas en 3 minutos.")
                    await asyncio.sleep(intervalo_revision)
                    await abrir_pagina(pagina_lista, FILTER_LIST_URL)
                    await abrir_pagina(pagina_filtro, FILTER_URL)
                    continue

                for cliente in clientes:
                    cliente_dni = str(cliente.get("dni", "")).strip()
                    monto = cliente["monto"]
                    try:
                        await filtrar_dni(pagina_filtro, cliente_dni)
                    except ValueError as exc:
                        consultados_en_esta_sesion.add(cliente_dni)
                        print(f"[!] Se omitio un registro con DNI invalido: {exc}")
                        continue
                    consultados_en_esta_sesion.add(cliente_dni)
                    actualizar_estado_dni(cliente_dni, "filtrado")
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
                await abrir_pagina(pagina_lista, FILTER_LIST_URL)
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
