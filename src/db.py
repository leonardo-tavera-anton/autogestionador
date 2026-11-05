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


def obtener_pendientes(excluir_dnis=None, cantidad=2):
    """Lee filas sin estado y con monto entre 20 000 y 99 999, mayor primero."""
    excluir_dnis = excluir_dnis or set()
    filas_por_dni = {}
    estados_sin_procesar = (None, "None", "pendiente")

    for estado in estados_sin_procesar:
        inicio = 0
        while True:
            consulta = (
                supabase.table("clientes")
                .select("dni,nombre,celular,monto,estado")
                .gte("monto", 20000)
                .lte("monto", 99999)
                .order("monto", desc=True)
                .order("dni")
            )
            if estado is None:
                consulta = consulta.is_("estado", "null")
            else:
                consulta = consulta.eq("estado", estado)

            respuesta = consulta.range(inicio, inicio + 99).execute()
            pagina = respuesta.data or []
            for fila in pagina:
                dni = str(fila.get("dni", "")).strip()
                celular = str(fila.get("celular", "")).strip()
                nombre = str(fila.get("nombre", "")).strip()
                if (
                    re.fullmatch(r"\d{8}", dni)
                    and celular.lower() not in {"", "none", "null"}
                    and nombre.lower() not in {"", "none", "null"}
                    and dni not in excluir_dnis
                ):
                    filas_por_dni[dni] = fila
            if len(pagina) < 100:
                break
            inicio += 100

    filas = sorted(
        filas_por_dni.values(),
        key=lambda fila: float(fila.get("monto") or 0),
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
            .gte("monto", 20000)
            .lte("monto", 99999)
        )
        if estado is None:
            consulta = consulta.is_("estado", "null")
        else:
            consulta = consulta.eq("estado", estado)
        respuesta = consulta.limit(1).execute()
        if respuesta.data:
            fila = respuesta.data[0]
            nombre = str(fila.get("nombre", "")).strip().lower()
            celular = str(fila.get("celular", "")).strip().lower()
            if nombre not in {"", "none", "null"} and celular not in {"", "none", "null"}:
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
