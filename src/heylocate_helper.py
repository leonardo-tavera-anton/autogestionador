import asyncio
import re
import time
import unicodedata
from playwright.async_api import Page, Error as PlaywrightError

URL_CONSULTA_OPERADORA = "https://heylocate.mobi/es/"
OPERADORAS = ("CLARO", "MOVISTAR", "ENTEL", "BITEL")


def _normalizar_celular_peruano(valor: str) -> str | None:
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) == 11 and digitos.startswith("51"):
        digitos = digitos[2:]
    if len(digitos) == 9 and digitos.startswith("9"):
        return digitos
    return None


def _validar_celular_consultado(esperado: str, ingresado: str) -> str:
    celular_esperado = _normalizar_celular_peruano(esperado)
    if celular_esperado is None:
        raise ValueError(
            "El cliente no tiene un celular peruano valido; "
            "no se consultara un numero predeterminado."
        )
    celular_ingresado = _normalizar_celular_peruano(ingresado)
    if celular_ingresado != celular_esperado:
        numero_mostrado = celular_ingresado or re.sub(r"\D", "", str(ingresado))
        raise RuntimeError(
            f"HeyLocate debia mostrar {celular_esperado}, pero muestra "
            f"{numero_mostrado or 'un valor vacio o invalido'}; se cancelo "
            "la consulta para no usar un numero distinto al del cliente."
        )
    return celular_esperado


def extraer_operadora(texto: str) -> str:
    """Devuelve una operadora solo cuando aparece junto a una etiqueta de resultado."""
    texto = unicodedata.normalize("NFKD", str(texto or "").upper())
    texto = "".join(
        caracter for caracter in texto if not unicodedata.combining(caracter)
    )
    texto = re.sub(r"\s+", " ", texto)
    patron = (
        r"\b(?:OPERADORA|OPERADOR\s+ASIGNADO\s+ORIGINALMENTE|OPERADOR|"
        r"CARRIER|NETWORK|RED|"
        r"PROVEEDOR\s+DE\s+TELECOMUNICACIONES(?:\s*/\s*AREA)?)\b"
        r"\s*(?::|-|ES)?\s*(?:MOVIL\s+)?"
        r"\b(CLARO|MOVISTAR|ENTEL|BITEL)\b"
    )
    coincidencia = re.search(patron, texto)
    if coincidencia:
        return coincidencia.group(1)
    return "DESCONOCIDA"


async def consultar_operadora(page: Page, celular: str) -> str:
    celular_limpio = _normalizar_celular_peruano(celular)
    if celular_limpio is None:
        raise ValueError(
            "El cliente no tiene un celular peruano valido; "
            "no se consultara un numero predeterminado."
        )

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
        celular_ingresado = _validar_celular_consultado(
            celular_limpio, await campo_input.input_value()
        )
        print(f"[*] HeyLocate consultara el celular del cliente: {celular_ingresado}.")

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
                texto_normalizado = unicodedata.normalize("NFKD", contenido_texto.upper())
                texto_normalizado = "".join(
                    caracter
                    for caracter in texto_normalizado
                    if not unicodedata.combining(caracter)
                )
                if re.search(
                    r"\bOPERADOR\s+ASIGNADO\s+ORIGINALMENTE\b",
                    texto_normalizado,
                ):
                    print(
                        "[!] HeyLocate solo muestra el operador originalmente asignado; "
                        "podria no ser el actual."
                    )
                return operadora
            await asyncio.sleep(0.5)
        return "DESCONOCIDA"
    except PlaywrightError as exc:
        raise RuntimeError(f"No se pudo consultar la operadora en HeyLocate: {exc}") from exc
