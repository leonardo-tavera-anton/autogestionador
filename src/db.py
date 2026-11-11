import os
import re

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
if not supabase_url or not supabase_key:
    raise RuntimeError("Configura SUPABASE_URL y SUPABASE_KEY en el archivo .env.")

supabase = create_client(supabase_url, supabase_key)

try:
    MONTO_MINIMO = float(os.getenv("FUVEX_MIN_AMOUNT", "5000"))
except ValueError as exc:
    raise RuntimeError("FUVEX_MIN_AMOUNT debe ser un monto numerico.") from exc
if MONTO_MINIMO < 0:
    raise RuntimeError("FUVEX_MIN_AMOUNT no puede ser negativo.")


def _monto_numerico(valor):
    try:
        return float(str(valor or "0").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _celular_peruano(valor):
    """Devuelve el celular peruano en nueve digitos o None si no es valido."""
    texto = str(valor or "").strip()
    if texto.endswith(".0"):
        texto = texto[:-2]
    digitos = re.sub(r"\D", "", texto)
    if len(digitos) == 11 and digitos.startswith("51"):
        digitos = digitos[2:]
    if len(digitos) == 9 and digitos.startswith("9"):
        return digitos
    return None


def obtener_pendientes(excluir_dnis=None, cantidad=2):
    """Lee hasta los mejores pendientes válidos de cada estado, mayor monto primero."""
    if cantidad <= 0:
        return []
    excluir_dnis = excluir_dnis or set()
    filas_por_dni = {}
    estados_sin_procesar = (None, "None", "pendiente")

    for estado in estados_sin_procesar:
        inicio = 0
        validos_estado = 0
        while True:
            consulta = (
                supabase.table("clientes")
                .select("dni,nombre,celular,monto,estado")
                .gte("monto", MONTO_MINIMO)
                .lte("monto", 99999)
                .order("monto", desc=True)
                .order("dni")
            )
            if estado is None:
                consulta = consulta.is_("estado", "null")
            else:
                consulta = consulta.eq("estado", estado)

            respuesta = consulta.range(inicio, inicio + 99).execute()
            datos = respuesta.data
            pagina = datos if isinstance(datos, list) else []
            for fila in pagina:
                if not isinstance(fila, dict):
                    continue
                dni = str(fila.get("dni", "")).strip()
                celular = _celular_peruano(fila.get("celular"))
                nombre = str(fila.get("nombre", "")).strip()
                if (
                    re.fullmatch(r"\d{8}", dni)
                    and celular is not None
                    and nombre.lower() not in {"", "none", "null"}
                    and dni not in excluir_dnis
                ):
                    if dni not in filas_por_dni:
                        fila = dict(fila)
                        fila["celular"] = celular
                        filas_por_dni[dni] = fila
                        validos_estado += 1
            if validos_estado >= cantidad:
                break
            if len(pagina) < 100:
                break
            inicio += 100

    filas = sorted(
        filas_por_dni.values(),
        key=lambda fila: _monto_numerico(fila.get("monto")),
        reverse=True,
    )
    return filas[:cantidad]


def obtener_pendiente_por_dni(dni):
    """Busca la fila aun no procesada asociada a un filtro verde existente."""
    for estado in (None, "None", "pendiente", "filtrado"):
        consulta = (
            supabase.table("clientes")
            .select("dni,nombre,celular,monto,estado")
            .eq("dni", str(dni).strip())
            .gte("monto", MONTO_MINIMO)
            .lte("monto", 99999)
        )
        if estado is None:
            consulta = consulta.is_("estado", "null")
        else:
            consulta = consulta.eq("estado", estado)
        respuesta = consulta.limit(1).execute()
        datos = respuesta.data
        if isinstance(datos, list) and datos:
            fila = datos[0]
            if not isinstance(fila, dict):
                continue
            nombre = str(fila.get("nombre", "")).strip().lower()
            celular = _celular_peruano(fila.get("celular"))
            if nombre not in {"", "none", "null"} and celular is not None:
                fila = dict(fila)
                fila["celular"] = celular
                return fila
    return None


def actualizar_estado_dni(dni, estado):
    respuesta = (
        supabase.table("clientes")
        .update({"estado": estado})
        .eq("dni", str(dni).strip())
        .select("dni")
        .execute()
    )
    if not respuesta.data:
        raise RuntimeError("No se actualizo la fila; confirma que el DNI exista en Supabase.")
