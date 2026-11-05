import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

from src.db import actualizar_cliente, obtener_pendiente
from src.scraper import procesar_en_fuvex

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCUMENTO_REQUERIDO = PROJECT_ROOT / "assets" / "documento_requerido.jpeg"

# Ruta de tu perfil de Google Chrome en Windows para mantener sesión y Tampermonkey
USER_DATA_DIR = r"C:\Users\leona\AppData\Local\Google\Chrome\User Data"


async def main():
    if not DOCUMENTO_REQUERIDO.is_file():
        raise FileNotFoundError(f"No se encontro el documento requerido: {DOCUMENTO_REQUERIDO}")

    async with async_playwright() as p:
        # Lanzamos un contexto persistente usando tu propio Chrome para heredar sesiones y extensiones
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            channel="chrome",  # Usa Google Chrome instalado en tu PC
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        
        try:
            # Obtener la primera página abierta o crear una nueva
            page = browser.pages[0] if browser.pages else await browser.new_page()

            # Navegar directamente a Fuvex (como ya estás logueado en tu Chrome, entrará directo o usará tu sesión)
            await page.goto("https://fuvexbn.a365.com.pe:7443/general/menu.php")
            
            # Opcional: Si requieres ir a una sección de filtros específica, navega o haz clic aquí
            # await page.click("text=Filtros") # Ajusta según el selector de tu menú

            print("[*] Bot conectado, sesión activa y escuchando Supabase...")

            while True:
                cliente = obtener_pendiente()
                if not cliente:
                    await asyncio.sleep(10)
                    continue

                dni = cliente["dni"]
                print(f"Procesando DNI: {dni}")

                resultado = await procesar_en_fuvex(page, dni, str(DOCUMENTO_REQUERIDO))

                if resultado is None:
                    print("-> Consulta fallida; se conserva el estado pendiente.")
                    await asyncio.sleep(10)
                    continue

                if resultado.get("apto"):
                    actualizar_cliente(cliente["id"], "registrado", resultado["monto"], resultado["tea"])
                    print("-> Registrado con exito.")
                else:
                    actualizar_cliente(cliente["id"], "no_apto")
                    print("-> No habil.")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())