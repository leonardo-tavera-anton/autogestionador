import asyncio
from playwright.async_api import async_playwright
from src.db import obtener_pendiente, actualizar_cliente
from src.scraper import procesar_en_fuvex
import os

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Login inicial en Fuvex
        await page.goto("https://fuvexbn.a365.com.pe:7443/index.php")
        await page.fill("#USUARIO", os.getenv("FUVEX_USER"))
        await page.fill("input[type='password']", os.getenv("FUVEX_PASS"))
        await page.click("button[type='submit']")
        await page.click("text=Iniciar sesión con Google")
        await page.wait_for_url("**/menu.php")

        print("[*] Bot conectado y escuchando Supabase...")

        while True:
            cliente = obtener_pendiente()
            if not cliente:
                await asyncio.sleep(10)
                continue

            dni = cliente["dni"]
            print(f"Procesando DNI: {dni}")

            resultado = await procesar_en_fuvex(page, dni, "assets/documento_requerido.png")

            if resultado and resultado.get("apto"):
                actualizar_cliente(cliente["id"], "registrado", resultado["monto"], resultado["tea"])
                print(f"-> ¡Registrado con éxito!")
            else:
                actualizar_cliente(cliente["id"], "no_apto")
                print(f"-> No hábil.")

if __name__ == "__main__":
    asyncio.run(main())