async def procesar_en_fuvex(page, dni, imagen_ruta):
    try:
        await page.goto("https://url-fuvex.com/consulta")
        await page.fill("#input-dni", dni)
        await page.click("#btn-consultar")

        # Espera con manejo de lentitud
        await page.wait_for_selector("#resultado-container", timeout=10000)
        estado_texto = await page.locator("#resultado-estado").inner_text()

        if "verde" in estado_texto.lower() or "habil" in estado_texto.lower():
            monto = await page.locator("#monto-maximo").inner_text()
            tea = await page.locator("#tea").inner_text()

            # Registro y subida de imagen
            await page.click("#btn-ir-registro")
            await page.fill("#form-dni", dni)
            await page.fill("#form-monto", monto)
            await page.set_input_files("#input-subir-imagen", imagen_ruta)
            await page.click("#btn-guardar-registro")

            return {"apto": True, "monto": monto, "tea": tea}
        
        return {"apto": False}
    except Exception as e:
        print(f"Error en scraper: {e}")
        return None