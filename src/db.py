import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def obtener_pendiente():
    res = supabase.table("clientes").select("*").eq("estado", "pendiente").limit(1).execute()
    return res.data[0] if res.data else None

def actualizar_cliente(cliente_id, estado, monto=None, tea=None):
    data = {"estado": estado}
    if monto: data["monto_maximo"] = monto
    if tea: data["tea"] = tea
    supabase.table("clientes").update(data).eq("id", cliente_id).execute()