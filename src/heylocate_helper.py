import asyncio
import re
from playwright.async_api import Page, Error as PlaywrightError

URL_CONSULTA_OPERADORA = "https://heylocate.mobi/es/"

async def consultar_operadora(page: Page, celular: str) -> str:
    celular_limpio = str(celular or "").strip()
    if not celular_limpio:
        return "DESCONOCIDA"

    try:
        await page.goto(URL_CONSULTA_OPERADORA, wait_until="domcontentloaded", timeout=45000)
        input_selector = "input[type='text'], input[type='tel']"
        await page.wait_for_selector(input_selector, timeout=10000)

        campo_input = page.locator(input_selector).first
        await campo_input.fill("")
        await campo_input.type(celular_limpio, delay=40)

        btn_buscar = page.locator("button:has-text('BUSCAR'), button[type='submit'], input[type='submit']")
        if await btn_buscar.count() > 0:
            await btn_buscar.first.click()
        else:
            await campo_input.press("Enter")

        await asyncio.sleep(7)
        contenido_texto = await page.locator("body").inner_text()
        texto_upper = contenido_texto.upper()

        operadora_detectada = "DESCONOCIDA"
        for op in ["CLARO", "MOVISTAR", "ENTEL", "BITEL"]:
            if op in texto_upper:
                operadora_detectada = op
                break

        return operadora_detectada

    except (PlaywrightError, Exception):
        return "DESCONOCIDA"