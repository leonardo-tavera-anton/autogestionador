import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

FILTER_URL = "https://fuvexbn.a365.com.pe:7443/BN/ingresar_filtro.php"
FILTER_LIST_URL = "https://fuvexbn.a365.com.pe:7443/BN/Lista_Filtros_F.php"
REGISTRO_URL = "https://fuvexbn.a365.com.pe:7443/BN/Registro_Prospecto_BN.php"
EVIDENCIA_REQUERIDA = (
    Path(__file__).resolve().parent.parent / "assets" / "documento_requerido.jpeg"
)


class FiltroSinConfirmarError(RuntimeError):
    """El clic se envio, pero Fuvex no mostro una confirmacion verificable."""


class ClienteOmitidoError(RuntimeError):
    """Fuvex indico que el cliente ya se proceso o no esta disponible."""


class FiltrosNoDisponiblesError(RuntimeError):
    """Fuvex no permite continuar porque no hay filtros disponibles."""


class ColaEsperaLlenaError(RuntimeError):
    """Fuvex rechaza el filtro porque la cola de espera alcanzo su limite."""


class RegistroSinConfirmarError(RuntimeError):
    """Fuvex no mostro una respuesta verificable luego del registro."""


class RegistroRechazadoError(RuntimeError):
    """Fuvex rechazo el registro y mostro un mensaje de error."""


class SesionFuvexExpiradaError(RuntimeError):
    """Fuvex devolvio al inicio de sesion durante una operacion."""


_CLIENTE_OMITIDO_RE = re.compile(
    r"\b(?:YA )?(?:SE ENCUENTRA|FUE|HA SIDO|SE HA|ESTA|SIGUE) "
    r"(?:FILTRAD[OA]|REGISTRAD[OA]|PENDIENTE|EN ESPERA|EN PROCESO)\b"
    r"|\bNO (?:ESTA|SE ENCUENTRA) DISPONIBLE\b"
    r"|\b(?:SOLICITUD|FILTRO) (?:YA )?(?:ESTA|SE ENCUENTRA) "
    r"(?:PENDIENTE|EN ESPERA|EN PROCESO)\b"
)
_SIN_FILTROS_RE = re.compile(
    r"\b(?:NO HAY|NO TIENE|NO CUENTA CON|SIN|AGOT[OA]S?) "
    r"(?:MAS )?(?:FILTROS?|SOLICITUDES?)\b"
    r"|\bFILTROS? DISPONIBLES? (?:AGOTAD[OA]S?|INSUFICIENTES?)\b"
)
_COLA_ESPERA_LLENA_RE = re.compile(
    r"\b(?:COMO )?MAXIMO\b.{0,80}\bCOLA DE ESPERA\b"
    r"|\bCOLA DE ESPERA\b.{0,80}\bMAXIMO\b"
)


def _normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto or "").upper())
    return "".join(caracter for caracter in texto if not unicodedata.combining(caracter))


def _cliente_omitido(texto):
    return bool(_CLIENTE_OMITIDO_RE.search(_normalizar(texto)))


def _sin_filtros_disponibles(texto):
    return bool(_SIN_FILTROS_RE.search(_normalizar(texto)))


def _cola_espera_llena(texto):
    normalizado = re.sub(r"\s+", " ", _normalizar(texto))
    return bool(_COLA_ESPERA_LLENA_RE.search(normalizado))


def fecha_estimada_visita(hoy=None):
    hoy = hoy or date.today()
    anio, mes = hoy.year, hoy.month
    if hoy.day > 23:
        mes += 1
        if mes > 12:
            mes = 1
            anio += 1
    return date(anio, mes, 23)


async def _avisos_visibles(page):
    avisos = page.locator(
        ".alert:visible, [role='alert']:visible, .swal2-popup:visible, "
        ".toast:visible, .modal:visible"
    )
    return await avisos.all_inner_texts()


async def _aceptar_confirmacion_exito(page):
    selectores_modal = (
        ".swal-modal:visible, .swal2-popup:visible, "
        "[role='dialog']:visible, .modal:visible"
    )
    modal = page.locator(selectores_modal).last
    modal_visible = await modal.count() > 0
    selector_botones_modal = (
        ".swal-button--confirm:visible, .swal2-confirm:visible, "
        "button:visible, [role='button']:visible, "
        "input[type='button']:visible, a:visible"
    )
    selector_botones = (
        "button:visible, [role='button']:visible, "
        "input[type='button']:visible, a:visible"
    )
    if modal_visible:
        botones_especiales = modal.locator(
            ".swal-button--confirm:visible, .swal2-confirm:visible"
        )
        botones = (
            botones_especiales
            if await botones_especiales.count()
            else modal.locator(selector_botones_modal)
        )
    else:
        botones = page.locator(selector_botones)
    etiquetas = await botones.evaluate_all(
        "els => els.map(e => (e.innerText || e.value || "
        "e.getAttribute('aria-label') || '').trim())"
    )
    indice_ok = next(
        (
            indice
            for indice, etiqueta in enumerate(etiquetas)
            if re.fullmatch(r"(?:OK|ACEPTAR|CONTINUAR)", etiqueta, re.I)
        ),
        None,
    )
    if indice_ok is None:
        if modal_visible:
            raise RuntimeError(
                "Fuvex mostro la confirmacion de registro, pero no encontre "
                "un boton visible OK/Aceptar para cerrarla."
            )
        return False

    boton_ok = botones.nth(indice_ok)
    try:
        await boton_ok.click(force=True, timeout=5000)
    except PlaywrightError as exc:
        try:
            await boton_ok.evaluate("(elemento) => elemento.click()")
        except PlaywrightError as fallback_exc:
            raise RuntimeError(
                "No pude pulsar el boton OK/Aceptar de la confirmacion de Fuvex."
            ) from fallback_exc

    if modal_visible:
        try:
            await modal.wait_for(state="hidden", timeout=5000)
        except PlaywrightError as exc:
            raise RuntimeError(
                "Se pulso OK/Aceptar, pero la confirmacion de Fuvex sigue visible."
            ) from exc
        overlay = page.locator(".swal-overlay:visible").last
        if await overlay.count():
            try:
                await overlay.wait_for(state="hidden", timeout=5000)
            except PlaywrightError as exc:
                raise RuntimeError(
                    "Se cerro el mensaje, pero la capa de confirmacion "
                    "sigue bloqueando el formulario."
                ) from exc
    return True


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


async def _selector_por_id(page, identificador):
    selector = page.locator(f"#{identificador}")
    if not await selector.count():
        raise RuntimeError(
            f"No encontre el campo requerido #{identificador} en el formulario."
        )
    return selector


async def _establecer_valor_por_id(page, identificador, valor):
    campo = await _selector_por_id(page, identificador)
    etiqueta = await campo.evaluate("(el) => el.tagName.toLowerCase()")
    if etiqueta == "select":
        opciones = await campo.locator("option").evaluate_all(
            "els => els.map(e => ({value: e.value, text: (e.textContent || '').trim()}))"
        )
        opcion = next(
            (item for item in opciones if item["value"] == valor),
            next(
                (item for item in opciones if item["text"] == valor),
                None,
            ),
        )
        if opcion is not None:
            await campo.select_option(value=opcion["value"])
        else:
            raise RuntimeError(
                f"El selector #{identificador} no ofrece el valor esperado: {valor}."
            )
    else:
        await campo.fill(valor)
    return campo


async def _seleccionar_menor_tasa(page):
    selector = await _selector_por_id(page, "TASA")
    firma_anterior = None
    muestras_estables = 0
    opciones = []
    for _ in range(24):
        opciones = await selector.locator("option").evaluate_all(
            "els => els.map(e => ({value: e.value, text: (e.textContent || '').trim()}))"
        )
        firma_actual = tuple((item["value"], item["text"]) for item in opciones)
        if firma_actual == firma_anterior:
            muestras_estables += 1
            if muestras_estables >= 3:
                break
        else:
            firma_anterior = firma_actual
            muestras_estables = 0
        await page.wait_for_timeout(250)

    tasas = []
    for opcion in opciones:
        if not opcion["value"]:
            continue
        coincidencia = re.fullmatch(
            r"\s*(\d+(?:[.,]\d+)?)\s*%?\s*", opcion["text"]
        )
        if coincidencia is None:
            continue
        try:
            tasa = Decimal(coincidencia.group(1).replace(",", "."))
        except InvalidOperation:
            continue
        tasas.append((tasa, opcion))
    if not tasas:
        opciones_disponibles = ", ".join(
            opcion["text"] or opcion["value"] or "(vacia)"
            for opcion in opciones
        )
        raise RuntimeError(
            "No encontre tasas numericas seleccionables en #TASA. "
            f"Opciones visibles: {opciones_disponibles or 'ninguna'}"
        )

    tasa_menor, opcion_menor = min(tasas, key=lambda item: item[0])
    for _ in range(3):
        await selector.select_option(value=opcion_menor["value"])
        await page.wait_for_timeout(250)
        seleccionada = await selector.locator("option:checked").evaluate(
            "e => ({value: e.value, text: (e.textContent || '').trim()})"
        )
        tasa_visible = re.fullmatch(
            r"\s*(\d+(?:[.,]\d+)?)\s*%?\s*", seleccionada["text"]
        )
        if (
            seleccionada["value"] == opcion_menor["value"]
            and tasa_visible is not None
            and Decimal(tasa_visible.group(1).replace(",", ".")) == tasa_menor
        ):
            return tasa_menor
    raise RuntimeError(
        f"No se pudo mantener seleccionada la tasa minima {tasa_menor} en #TASA."
    )


async def dnis_con_filtro_correcto(page):
    """Lee los DNI que la tabla Filtros sin registro marca como validos."""
    if "lista_filtros_f.php" not in page.url.lower():
        await abrir_pagina(page, FILTER_LIST_URL)
    if "lista_filtros_f.php" not in page.url.lower():
        raise RuntimeError(
            f"Fuvex no dejo abrir la lista de filtros. URL final: {page.url}; "
            f"titulo: {await page.title()}"
        )
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
            fila = filas.nth(indice_fila)
            datos_fila = await fila.evaluate(
                "e => ({className: e.className || '', background: getComputedStyle(e).backgroundColor})"
            )
            celdas = await fila.locator("td").evaluate_all(
                "els => els.map(e => ({text: (e.innerText || '').trim(), className: e.className || '', aria: e.getAttribute('aria-label') || '', title: e.getAttribute('title') || '', background: getComputedStyle(e).backgroundColor}))"
            )
            if max(indice_dni, indice_valido, indice_incorrecto) >= len(celdas):
                continue

            celda_valida = celdas[indice_valido]
            celda_incorrecta = celdas[indice_incorrecto]
            valor_valido = _normalizar(celda_valida["text"]).strip()
            valor_incorrecto = _normalizar(celda_incorrecta["text"]).strip()
            metadatos_validos = _normalizar(" ".join(
                celda_valida[k] or "" for k in ("className", "aria", "title")
            ))
            metadatos_incorrectos = _normalizar(" ".join(
                celda_incorrecta[k] or "" for k in ("className", "aria", "title")
            ))
            fondo_valido = celda_valida["background"].lower()
            fondo_fila = datos_fila["background"].lower()
            clase_fila = _normalizar(datos_fila["className"])
            valores_positivos = {
                "1", "SI", "S", "TRUE", "VERDADERO", "VALIDO", "CORRECTO",
                "APLICA", "YES", "OK", "✓", "✔",
            }
            marcado_valido = (
                valor_valido in valores_positivos
                or any(token in metadatos_validos for token in ("SUCCESS", "VALID", "CORRECT"))
                or any(color in fondo_valido for color in ("198, 239, 206", "212, 237, 218"))
                or any(color in fondo_fila for color in ("198, 239, 206", "212, 237, 218"))
                or any(token in clase_fila for token in ("SUCCESS", "TABLE-SUCCESS", "BG-SUCCESS"))
            )
            marcado_incorrecto = (
                valor_incorrecto not in {"", "0", "NO", "FALSE", "NULL", "NONE", "-"}
                or any(token in metadatos_incorrectos for token in ("DANGER", "ERROR", "INCORRECT"))
            )
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


async def filtrar_dni(page, dni, antes_de_enviar=None):
    dni = str(dni).strip()
    if not re.fullmatch(r"\d{8}", dni):
        raise ValueError("El registro no contiene un DNI valido de 8 digitos.")

    if "ingresar_filtro.php" not in page.url.lower():
        await abrir_pagina(page, FILTER_URL)
    if "ingresar_filtro.php" not in page.url.lower():
        raise RuntimeError(
            f"Fuvex redirigio fuera de la pagina de filtros. URL final: {page.url}; "
            f"titulo: {await page.title()}"
        )

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

    avisos_anteriores = set(await _avisos_visibles(page))
    try:
        if antes_de_enviar is not None:
            await antes_de_enviar()
        await page.locator(selector_botones).nth(accion["index"]).click()
    except PlaywrightError as exc:
        raise FiltroSinConfirmarError(
            f"No se pudo confirmar si Fuvex recibio el clic. URL: {page.url}"
        ) from exc
    confirmacion = page.get_by_text(
        re.compile(r"Se registr.{0,3} con .{0,3}xito", re.I)
    ).first
    aviso_cola_llena = page.get_by_text(_COLA_ESPERA_LLENA_RE).first
    for _ in range(80):
        if await confirmacion.count() and await confirmacion.is_visible():
            break
        if await aviso_cola_llena.count() and await aviso_cola_llena.is_visible():
            mensaje = re.sub(r"\s+", " ", await aviso_cola_llena.inner_text()).strip()
            raise ColaEsperaLlenaError(mensaje)
        avisos_nuevos = [
            aviso for aviso in await _avisos_visibles(page)
            if aviso not in avisos_anteriores
        ]
        mensaje = next((aviso for aviso in avisos_nuevos if _cliente_omitido(aviso)), None)
        if mensaje:
            raise ClienteOmitidoError(re.sub(r"\s+", " ", mensaje).strip())
        mensaje = next(
            (aviso for aviso in avisos_nuevos if _sin_filtros_disponibles(aviso)),
            None,
        )
        if mensaje:
            raise FiltrosNoDisponiblesError(re.sub(r"\s+", " ", mensaje).strip())
        mensaje = next(
            (aviso for aviso in avisos_nuevos if _cola_espera_llena(aviso)),
            None,
        )
        if mensaje:
            raise ColaEsperaLlenaError(re.sub(r"\s+", " ", mensaje).strip())
        await page.wait_for_timeout(250)
    else:
        texto = await page.locator("body").inner_text()
        if _cliente_omitido(texto):
            raise ClienteOmitidoError(
                "Fuvex indico que el DNI ya fue filtrado/registrado o no esta disponible."
            )
        if _sin_filtros_disponibles(texto):
            raise FiltrosNoDisponiblesError(
                "Fuvex indico que ya no hay filtros disponibles."
            )
        if _cola_espera_llena(texto):
            raise ColaEsperaLlenaError(
                "Fuvex indico que la cola de espera alcanzo su limite."
            )
        detalle = re.sub(r"\s+", " ", texto)[:400]
        raise FiltroSinConfirmarError(
            f"Se pulso el boton, pero Fuvex no confirmo el filtro. "
            f"URL: {page.url}; respuesta: {detalle or 'sin mensaje visible'}"
        )

    boton_ok = page.get_by_role("button", name="OK", exact=True).last
    if await boton_ok.count():
        await boton_ok.click(timeout=5000)
    return True


async def preparar_registro(page, dni, nombre, celular, operador):
    if "registro_prospecto_bn.php" not in page.url.lower():
        await abrir_pagina(page, REGISTRO_URL)
    if "registro_prospecto_bn.php" not in page.url.lower():
        raise RuntimeError("Fuvex redirigio fuera del formulario de registro.")

    campo_dni = await _buscar_campo(page, r"dni|documento|identidad")
    campo_nombre = await _buscar_campo(page, r"nombre")
    # El formulario Fuvex identifica el primer telefono como TEL1_NUM.
    campo_celular = await _buscar_campo(
        page, r"celular|tel.?fono|movil|phone|tel[12]_num"
    )
    selector_operador = await _selector_por_id(page, "TEL1_OP")
    await campo_dni.fill(str(dni).strip())
    await campo_nombre.fill(str(nombre or "").strip())
    telefono = str(celular or "").strip()
    if telefono.endswith(".0"):
        telefono = telefono[:-2]
    await campo_celular.fill(telefono)
    await selector_operador.select_option(label=operador)

    valores_fijos = {
        "PRODUCTO": "PRÉSTAMO NUEVO",
        "OFICINA": "ANCASH - NUEVO CHIMBOTE",
        "ESTADO_REGISTRO": "INTERESADO",
        "MONTO": "90000",
        "PLAZO": "60",
        "CANAL": "PRESENCIAL",
    }
    for identificador, valor in valores_fijos.items():
        await _establecer_valor_por_id(page, identificador, valor)
    interaccion = await _selector_por_id(page, "TITULO_M")
    opciones_interaccion = await interaccion.locator("option").evaluate_all(
        "els => els.map(e => ({value: e.value, text: (e.textContent || '').trim()}))"
    )
    opcion_pantallazo = next(
        (
            opcion
            for opcion in opciones_interaccion
            if "Pantallazo Llamada" in opcion["text"]
        ),
        None,
    )
    if opcion_pantallazo is None:
        raise RuntimeError(
            "El formulario no ofrece la interaccion 'Pantallazo Llamada'."
        )
    await interaccion.select_option(value=opcion_pantallazo["value"])

    fecha_visita = await _selector_por_id(page, "FECHA_VISITA")
    await fecha_visita.fill(fecha_estimada_visita().isoformat())

    if not EVIDENCIA_REQUERIDA.is_file():
        raise FileNotFoundError(
            f"No se encontro la evidencia requerida: {EVIDENCIA_REQUERIDA}"
        )
    campo_archivo = page.locator("input[type='file']").first
    if not await campo_archivo.count():
        raise RuntimeError("El formulario no muestra un campo para adjuntar evidencias.")
    await campo_archivo.set_input_files(str(EVIDENCIA_REQUERIDA))
    archivos_adjuntos = await campo_archivo.evaluate(
        "el => Array.from(el.files || []).map(file => file.name)"
    )
    if EVIDENCIA_REQUERIDA.name not in archivos_adjuntos:
        raise RuntimeError(
            f"No se confirmo el adjunto de {EVIDENCIA_REQUERIDA.name}."
        )


async def registrar_prospecto(
    page, dni, nombre, celular, operador, antes_de_enviar=None
):
    await preparar_registro(page, dni, nombre, celular, operador)
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

    tasa_seleccionada = await _seleccionar_menor_tasa(page)
    selector_tasa = await _selector_por_id(page, "TASA")
    etiqueta_tasa = await selector_tasa.locator("option:checked").inner_text()
    if Decimal(etiqueta_tasa.strip().replace(",", ".")) != tasa_seleccionada:
        raise RuntimeError(
            f"La tasa visible {etiqueta_tasa} no coincide con la minima "
            f"{tasa_seleccionada}; no se enviara el registro."
        )
    print(f"[*] Tasa menor seleccionada y verificada: {etiqueta_tasa}.")
    if antes_de_enviar is not None:
        await antes_de_enviar()

    dialogos = []

    async def aceptar_dialogo(dialogo):
        dialogos.append(dialogo.message)
        await dialogo.accept()

    page.on("dialog", aceptar_dialogo)
    try:
        await page.locator(selector).nth(accion["index"]).click()
        await page.wait_for_timeout(1500)
    finally:
        page.remove_listener("dialog", aceptar_dialogo)

    host = (urlsplit(page.url).hostname or "").lower()
    ruta = urlsplit(page.url).path.lower()
    campo_login = page.locator("#USUARIO")
    if (
        host == "accounts.google.com"
        or (host == "fuvexbn.a365.com.pe" and ruta.endswith("/index.php"))
        or (await campo_login.count() and await campo_login.is_visible())
    ):
        raise SesionFuvexExpiradaError(
            f"Fuvex devolvio al inicio de sesion al intentar registrar {dni}; "
            "el envio no se puede confirmar."
        )

    texto = _normalizar(await page.locator("body").inner_text())
    avisos = await _avisos_visibles(page)
    respuesta = _normalizar(" ".join([*dialogos, *avisos, texto]))
    mensaje = next(
        (aviso for aviso in avisos if _cliente_omitido(aviso)),
        None,
    )
    if mensaje or (_cliente_omitido(respuesta) and not any(
        indicador in respuesta
        for indicador in ("REGISTRADO", "GUARDADO", "EXITO", "EXITOSAMENTE")
    )):
        raise ClienteOmitidoError(
            re.sub(
                r"\s+",
                " ",
                mensaje or "Fuvex indico que el DNI ya fue registrado o no esta disponible.",
            ).strip()
        )

    if any(
        indicador in respuesta
        for indicador in ("REGISTRADO", "GUARDADO", "EXITO", "EXITOSAMENTE")
    ):
        if await _aceptar_confirmacion_exito(page):
            print("[*] Confirmacion de registro aceptada; continuando con el siguiente.")
        return True

    detalle = re.sub(
        r"\s+",
        " ",
        " | ".join([*dialogos, *avisos]) or texto,
    ).strip()[:600]
    if any(
        indicador in respuesta
        for indicador in ("ERROR", "DEBE ADJUNTAR", "OBLIGATORIO", "NO SE PUDO")
    ):
        raise RegistroRechazadoError(
            f"Fuvex rechazo el registro de {dni}: {detalle or 'sin detalle visible'}"
        )
    raise RegistroSinConfirmarError(
        f"Fuvex no confirmo el registro de {dni}; respuesta visible: "
        f"{detalle or 'ninguna'}; URL: {page.url}"
    )
