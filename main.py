import os
import json
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, Header, Body
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv

# 1. Cargar variables de entorno
load_dotenv()

# 2. Configuración de Firebase (Compatible con Render y Local)
if not firebase_admin._apps:
    # Intentamos leer el contenido del JSON desde la variable de entorno (Render)
    firebase_json_content = os.getenv("FIREBASE_CREDENTIALS")
    
    if firebase_json_content:
        try:
            # Parseamos el string JSON a un diccionario
            cred_dict = json.loads(firebase_json_content)
            cred = credentials.Certificate(cred_dict)
        except json.JSONDecodeError:
            print("Error: La variable FIREBASE_CREDENTIALS no es un JSON válido.")
            raise
    else:
        # Si no hay variable, buscamos el archivo físico (Local)
        if os.path.exists("serviceAccountKey.json"):
            cred = credentials.Certificate("serviceAccountKey.json")
        else:
            raise Exception("No se encontraron credenciales de Firebase (ni variable ni archivo).")

    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://gimnasio-e1554-default-rtdb.europe-west1.firebasedatabase.app'
    })

app = FastAPI()

# --- MODELOS DE DATOS (PYDANTIC) ---

class EjercicioPlantilla(BaseModel):
    nombre: str
    series_objetivo: int
    reps_objetivo: str
    descanso_objetivo: Optional[str] = ""
    notas_default: Optional[str] = ""

class DiaRutina(BaseModel):
    dia_semana: str 
    ejercicios: List[EjercicioPlantilla]

class UpdateRutinaRequest(BaseModel):
    dias: Dict[str, DiaRutina]

class LogDiaRequest(BaseModel):
    fecha: str
    datos: Dict[str, Any]

# --- SEGURIDAD ---

def verify_key(x_api_key: str):
    # La clave debe estar en las variables de entorno de Render
    env_key = os.getenv("API_KEY")
    if not env_key:
        raise HTTPException(status_code=500, detail="Error de config: API_KEY no establecida en el servidor")
        
    if x_api_key != env_key:
        raise HTTPException(status_code=403, detail="Acceso denegado: Clave incorrecta")

# --- RUTAS DE LA API ---

@app.get("/")
def home():
    return {"status": "online", "mode": "Gym Bros API"}

# Endpoint para Cronjob (Evita que Render se duerma)
@app.get("/ping")
def keep_alive():
    return {"status": "alive", "message": "Server is running"}

# 1. Sincronización Total (GET)
@app.get("/sync/{usuario}")
async def sync_all(usuario: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    
    if usuario not in ["enrique", "oscar"]:
        raise HTTPException(status_code=400, detail="Usuario desconocido")
    
    amigo = "oscar" if usuario == "enrique" else "enrique"

    # Referencias a la base de datos
    ref_usuario = db.reference(f'/usuarios/{usuario}')
    ref_amigo = db.reference(f'/usuarios/{amigo}')

    # Obtenemos los datos (o diccionarios vacíos si no existen)
    datos_usuario = ref_usuario.get() or {}
    datos_amigo = ref_amigo.get() or {}

    return {
        "mi_perfil": {
            "rutina_actual": datos_usuario.get("rutina_actual", {}),
            "historial": datos_usuario.get("historial", {})
        },
        "perfil_amigo": {
            "rutina_actual": datos_amigo.get("rutina_actual", {}),
            "historial": datos_amigo.get("historial", {})
        }
    }

# 2. Definir Rutina / Plantilla (POST)
@app.post("/{usuario}/definir_rutina")
async def set_routine(usuario: str, payload: UpdateRutinaRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    
    ref = db.reference(f'/usuarios/{usuario}/rutina_actual')
    # Convertimos los modelos a diccionario para que Firebase los acepte
    datos_dict = {k: v.dict() for k, v in payload.dias.items()}
    ref.update(datos_dict)

    return {"status": "success", "message": "Plantilla de rutina actualizada"}

# 3. Guardar Día / Historial (POST)
@app.post("/{usuario}/guardar_dia")
async def save_day(usuario: str, payload: LogDiaRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)

    # Guardamos en el nodo 'historial' bajo la fecha específica
    ref = db.reference(f'/usuarios/{usuario}/historial/{payload.fecha}')
    ref.set(payload.datos)

    # Opcional: Devolver datos del amigo actualizados
    amigo = "oscar" if usuario == "enrique" else "enrique"
    datos_amigo_historial = db.reference(f'/usuarios/{amigo}/historial').get()

    return {
        "status": "success", 
        "message": f"Día {payload.fecha} guardado exitosamente",
        "friend_updates": datos_amigo_historial
    }

# 4. Borrar Día del Historial (DELETE)
@app.delete("/{usuario}/historial/{fecha}")
async def delete_history_day(usuario: str, fecha: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    
    ref = db.reference(f'/usuarios/{usuario}/historial/{fecha}')
    ref.delete()
    
    return {"status": "deleted", "target": f"Historial {fecha}"}

# 5. Borrar Día de la Plantilla (DELETE)
@app.delete("/{usuario}/rutina/{dia_semana}")
async def delete_routine_day(usuario: str, dia_semana: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    
    ref = db.reference(f'/usuarios/{usuario}/rutina_actual/{dia_semana}')
    ref.delete()
    
    return {"status": "deleted", "target": f"Plantilla {dia_semana}"}