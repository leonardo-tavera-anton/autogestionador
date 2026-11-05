import re
import unicodedata
from pathlib import Path

from playwright.async_api import Error as PlaywrightError

FILTER_URL = "https://fuvexbn.a365.com.pe:7443/BN/ingresar_filtro.php"
FILTER_LIST_URL = "https://fuvexbn.a365.com.pe:7443/BN/Lista_Filtros_F.php"
REGISTRO_URL = "https://fuvexbn.a365.com.pe:7443/BN/Registro_Prospecto_BN.php"
DOCUMENTOS_DIR = Path(__file__).resolve().parent.parent / "assets" / "documentos"


def _normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto or "").upper())
    return "".join(caracter for caracter in texto if not unicodedata.combining(caracter))


async def abrir_pagina(page, url):
    """Navega y reintenta si Fuvex interrumpe una redireccion inicial."""
    ultimo_error = None
    for intento in range(3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return
        except PlaywrightError as exc:
            ultimo_error = exc
            if "ERR_ABORTED" not in str(exc) or intento == 2:
                raise
            await page.wait_for_timeout(1000 * (intento + 1))
            if url.split("?")[0].lower() in page.url.lower():
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    return
                except PlaywrightError:
                    pass
    raise RuntimeError(f"No se pudo abrir {url}: {ultimo_error}")


async def _buscar_campo(page, patron):
    campos = await page.locator("input").evaluate_all(
        "els => els.map((e, i) => ({index: i, visible: e.offsetParent !== null, type: (e.type || '').toLowerCase(), meta: [e.name, e.id, e.placeholder, e.getAttribute('aria-label'), ...(e.labels ? Array.from(e.labels).map(l => l.innerText) : [])].filter(Boolean).join(' ')})).filter(x => x.visible && !['hidden','submit','button','checkbox','radio'].includes(x.type))"
    )
    campo = next((item for item in campos if re.search(patron, item["meta"], re.I)), None)
    if campo is None:
        detalle = ", ".join(item["meta"].strip() or "sin etiqueta" for item in campos)
        raise RuntimeError(
            f"No encontre el campo requerido ({patron}). Campos visibles: {detalle or 'ninguno'}"
        )
    return page.locator("input").nth(campo["index"])


async def _buscar_selector(page, patron):
    selectores = await page.locator("select").evaluate_all(
        "els => els.map((e, i) => ({index: i, meta: [e.name, e.id, e.getAttribute('aria-label'), ...(e.labels ? Array.from(e.labels).map(l => l.innerText) : [])].filter(Boolean).join(' ')}))"
    )
    selector = next(
        (item for item in selectores if re.search(patron, item["meta"], re.I)),
        None,
    )
    if selector is None:
        detalle = ", ".join(item["meta"] or "sin etiqueta" for item in selectores)
        raise RuntimeError(
            f"No encontre el selector ({patron}). Selectores disponibles: {detalle or 'ninguno'}"
        )
    return page.locator("select").nth(selector["index"])


async def dnis_con_filtro_correcto(page):
    """Lee los DNI que la tabla Filtros sin registro marca como validos."""
    await abrir_pagina(page, FILTER_LIST_URL)
    await page.wait_for_timeout(1000)
    encontrados = []
    total_filas = 0
    tablas = page.locator("table")

    for indice_tabla in range(await tablas.count()):
        tabla = tablas.nth(indice_tabla)
        encabezados = await tabla.locator("thead th").all_inner_texts()
        if not encabezados:
            encabezados = await tabla.locator("tr").first.locator("th,td").all_inner_texts()
        normalizados = [_normalizar(item) for item in encabezados]
        try:
            indice_dni = next(i for i, item in enumerate(normalizados) if "DNI_FILTRADO" in item)
            indice_valido = next(i for i, item in enumerate(normalizados) if item == "VALIDO")
            indice_incorrecto = next(i for i, item in enumerate(normalizados) if item == "INCORRECTO")
        except StopIteration:
            continue

        filas = tabla.locator("tbody tr")
        cantidad_filas = await filas.count()
        total_filas += cantidad_filas
        for indice_fila in range(cantidad_filas):
            celdas = await filas.nth(indice_fila).locator("td").evaluate_all(
                "els => els.map(e => ({text: (e.innerText || '').trim(), className: e.className || '', aria: e.getAttribute('aria-label') || '', title: e.getAttribute('title') || '', background: getComputedStyle(e).backgroundColor}))"
            )
            if max(indice_dni, indice_valido, indice_incorrecto) >= len(celdas):
                continue

            valor_valido = _normalizar(" ".join(
                celdas[indice_valido][k] or "" for k in ("text", "className", "aria", "title")
            ))
            valor_incorrecto = _normalizar(" ".join(
                celdas[indice_incorrecto][k] or "" for k in ("text", "className", "aria", "title")
            ))
            fondo = celdas[indice_valido]["background"].lower()
            marcado_valido = (
                valor_valido not in {"", "0", "NO", "FALSE", "NULL", "NONE", "-"}
                or "SUCCESS" in valor_valido
                or "VALID" in valor_valido
                or "CORRECT" in valor_valido
                or "198, 239, 206" in fondo
                or "212, 237, 218" in fondo
            )
            marcado_incorrecto = valor_incorrecto not in {"", "0", "NO", "FALSE", "NULL", "NONE", "-"}
            coincidencia = re.search(r"\b(\d{8})\b", celdas[indice_dni]["text"])
            if marcado_valido and not marcado_incorrecto and coincidencia:
                dni = coincidencia.group(1)
                if dni not in encontrados:
                    encontrados.append(dni)

    print(
        f"[*] Lista sin registro: {total_filas} filas, "
        f"{len(encontrados)} filtros marcados como validos."
    )
    return encontrados


async def filtrar_dni(page, dni):
    dni = str(dni).strip()
    if not re.fullmatch(r"\d{8}", dni):
        raise ValueError("El registro no contiene un DNI valido de 8 digitos.")

    await abrir_pagina(page, FILTER_URL)
    if "ingresar_filtro.php" not in page.url.lower():
        raise RuntimeError("Fuvex redirigio fuera de la pagina de filtros; revisa la sesion.")

    locator = await _buscar_campo(page, r"dni|documento|identidad")
    await locator.fill(dni)
    selector_botones = "button:visible, a:visible, input[type='submit']:visible, input[type='button']:visible"
    botones = await page.locator(selector_botones).evaluate_all(
        "els => els.map((e, i) => ({index: i, text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim()}))"
    )
    accion = next(
        (b for b in botones if re.search(r"consultar|filtrar|buscar", b["text"], re.I)),
        None,
    )
    if accion is None:
        raise RuntimeError("No encontre el boton Buscar/Filtrar en Fuvex.")

    await page.locator(selector_botones).nth(accion["index"]).click()
    try:
        texto = _normalizar(await page.locator("body").inner_text())
        if "SE REGISTRO CON EXITO" not in texto:
            await page.get_by_text(re.compile(r"Se registro con exito", re.I)).wait_for(
                state="visible", timeout=10000
            )
        await page.get_by_role("button", name="OK", exact=True).last.click(timeout=5000)
    except PlaywrightError:
        pass
    return True


async def preparar_registro(page, dni, nombre, celular, monto, operador):
    await abrir_pagina(page, REGISTRO_URL)
    if "registro_prospecto_bn.php" not in page.url.lower():
        raise RuntimeError("Fuvex redirigio fuera del formulario de registro.")

    campo_dni = await _buscar_campo(page, r"dni|documento|identidad")
    campo_nombre = await _buscar_campo(page, r"nombre")
    campo_celular = await _buscar_campo(page, r"celular|tel.?fono|movil|phone")
    campo_monto = await _buscar_campo(page, r"monto|importe")
    selector_operador = await _buscar_selector(page, r"operadora|operador")
    await campo_dni.fill(str(dni).strip())
    await campo_nombre.fill(str(nombre or "").strip())
    telefono = str(celular or "").strip()
    if telefono.endswith(".0"):
        telefono = telefono[:-2]
    await campo_celular.fill(telefono)
    await campo_monto.fill(str(monto))
    await selector_operador.select_option(label=operador)

    documentos = [
        DOCUMENTOS_DIR / f"{dni}{extension}"
        for extension in (".pdf", ".jpg", ".jpeg", ".png")
    ]
    documento = next((ruta for ruta in documentos if ruta.is_file()), None)
    if documento is None:
        raise FileNotFoundError(
            f"Falta la evidencia individual de este cliente en {DOCUMENTOS_DIR} "
            f"(nombre base: {dni})."
        )
    campo_archivo = page.locator("input[type='file']").first
    if not await campo_archivo.count():
        raise RuntimeError("El formulario no muestra un campo para adjuntar evidencias.")
    await campo_archivo.set_input_files(str(documento))


async def registrar_prospecto(page, dni, nombre, celular, monto, operador):
    await preparar_registro(page, dni, nombre, celular, monto, operador)
    selector = "button:visible, a:visible, input[type='submit']:visible"
    acciones = await page.locator(selector).evaluate_all(
        "els => els.map((e, i) => ({index: i, text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim()}))"
    )
    accion = next(
        (item for item in acciones if re.search(r"registrar|guardar|enviar", item["text"], re.I)),
        None,
    )
    if accion is None:
        opciones = ", ".join(item["text"] or "sin texto" for item in acciones)
        raise RuntimeError(f"No encontre el boton Registrar. Acciones: {opciones or 'ninguna'}")

    await page.locator(selector).nth(accion["index"]).click()
    await page.wait_for_timeout(1500)
    texto = _normalizar(await page.locator("body").inner_text())
    return "REGISTRADO" in texto or "GUARDADO" in texto or "EXITO" in texto
