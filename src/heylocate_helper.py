import asyncio
import re
import time
import unicodedata
from playwright.async_api import Page, Error as PlaywrightError

URL_CONSULTA_OPERADORA = "https://heylocate.mobi/es/"
OPERADORAS = ("CLARO", "MOVISTAR", "ENTEL", "BITEL")


def extraer_operadora(texto: str) -> str:
    """Devuelve una operadora solo cuando aparece junto a una etiqueta de resultado."""
    texto = unicodedata.normalize("NFKD", str(texto or "").upper())
    texto = "".join(
        caracter for caracter in texto if not unicodedata.combining(caracter)
    )
    texto = re.sub(r"\s+", " ", texto)
    patron = (
        r"\b(?:OPERADORA|OPERADOR|CARRIER|NETWORK|RED|"
        r"PROVEEDOR\s+DE\s+TELECOMUNICACIONES(?:\s*/\s*AREA)?)\b"
        r"\s*(?::|-|ES)?\s*(?:MOVIL\s+)?"
        r"\b(CLARO|MOVISTAR|ENTEL|BITEL)\b"
    )
    coincidencia = re.search(patron, texto)
    if coincidencia:
        return coincidencia.group(1)
    return "DESCONOCIDA"


async def consultar_operadora(page: Page, celular: str) -> str:
    celular_limpio = str(celular or "").strip()
    if not celular_limpio:
        return "DESCONOCIDA"

    try:
        await page.goto(URL_CONSULTA_OPERADORA, wait_until="domcontentloaded", timeout=45000)
        campo_input = page.locator("input[type='tel']:visible").first
        try:
            await campo_input.wait_for(state="visible", timeout=10000)
        except PlaywrightError:
            campo_input = page.locator(
                "input[type='text']:visible:not(.iti__search-input)"
                ":not([aria-label='Search']):not([placeholder='Search'])"
            ).first
            await campo_input.wait_for(state="visible", timeout=10000)

        await campo_input.fill(celular_limpio)

        boton_buscar = page.get_by_role(
            "button", name=re.compile(r"^\s*BUSCAR\s*$", re.I)
        ).first
        if await boton_buscar.count() and await boton_buscar.is_visible():
            await boton_buscar.click()
        else:
            boton_buscar = page.locator(
                "button[type='submit']:visible, input[type='submit']:visible"
            ).first
            if await boton_buscar.count():
                await boton_buscar.click()
            else:
                await campo_input.press("Enter")

        plazo = time.monotonic() + 10
        while time.monotonic() < plazo:
            contenido_texto = await page.locator("body").inner_text()
            operadora = extraer_operadora(contenido_texto)
            if operadora in OPERADORAS:
                return operadora
            await asyncio.sleep(0.5)
        return "DESCONOCIDA"
    except PlaywrightError as exc:
        raise RuntimeError(f"No se pudo consultar la operadora en HeyLocate: {exc}") from exc
