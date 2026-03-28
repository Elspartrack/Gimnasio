import os
import json
from datetime import datetime, timedelta
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
    firebase_json_content = os.getenv("FIREBASE_CREDENTIALS")
    
    if firebase_json_content:
        try:
            cred_dict = json.loads(firebase_json_content)
            cred = credentials.Certificate(cred_dict)
        except json.JSONDecodeError:
            print("Error: La variable FIREBASE_CREDENTIALS no es un JSON válido.")
            raise
    else:
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
    tipo_medida: Optional[str] = "reps"

class DiaRutina(BaseModel):
    dia_semana: str 
    ejercicios: List[EjercicioPlantilla]

class UpdateRutinaRequest(BaseModel):
    dias: Dict[str, DiaRutina]

class LogDiaRequest(BaseModel):
    fecha: str
    datos: Dict[str, Any]

class RegisterUserRequest(BaseModel):
    telefono: str
    nombre: str

class ClaimLegacyRequest(BaseModel):
    telefono: str
    legacy_username: str

class VerifyPhoneRequest(BaseModel):
    telefono: str

class PreviousRecordsRequest(BaseModel):
    exercise_names: List[str]
    before_date: str
    days_back: int = 14

# --- SEGURIDAD ---

def verify_key(x_api_key: str):
    env_key = os.getenv("API_KEY")
    if not env_key:
        raise HTTPException(status_code=500, detail="Error de config: API_KEY no establecida en el servidor")
    if x_api_key != env_key:
        raise HTTPException(status_code=403, detail="Acceso denegado: Clave incorrecta")

# --- HELPERS ---

def sanitize_phone(phone: str) -> str:
    """Sanitiza un teléfono dejando solo dígitos"""
    return ''.join(c for c in phone.strip() if c.isdigit())

def resolve_data_path(usuario: str) -> str:
    """Resuelve un ID de usuario (teléfono o nombre legacy) a su ruta de datos en Firebase"""
    reg = db.reference(f'/users_registry/{usuario}').get()
    if reg:
        return reg.get('data_path', usuario)
    if usuario in ['enrique', 'oscar']:
        return usuario
    return usuario

def get_all_registered_users() -> list:
    """Obtiene todos los usuarios registrados + usuarios legacy sin reclamar"""
    registry = db.reference('/users_registry').get() or {}
    
    users = []
    claimed_legacy = set()
    
    for phone, data in registry.items():
        users.append({
            "telefono": data.get("telefono", phone),
            "nombre": data.get("nombre", ""),
            "data_path": data.get("data_path", phone),
            "necesita_telefono": False,
            "legacy_username": data.get("legacy_username", "")
        })
        if data.get("legacy_username"):
            claimed_legacy.add(data["legacy_username"])
    
    for legacy_user in ["enrique", "oscar"]:
        if legacy_user not in claimed_legacy:
            ref = db.reference(f'/usuarios/{legacy_user}')
            if ref.get() is not None:
                users.append({
                    "telefono": "",
                    "nombre": legacy_user.capitalize(),
                    "data_path": legacy_user,
                    "necesita_telefono": True,
                    "legacy_username": legacy_user
                })
    
    return users

# --- RUTAS DE LA API ---

@app.get("/")
def home():
    return {"status": "online", "mode": "Gym Bros API v2"}

@app.get("/ping")
def keep_alive():
    return {"status": "alive", "message": "Server is running"}

# --- GESTIÓN DE USUARIOS ---

@app.get("/usuarios_registrados")
async def list_users(x_api_key: str = Header(None)):
    verify_key(x_api_key)
    users = get_all_registered_users()
    return {"status": "success", "usuarios": users}

@app.post("/registrar")
async def register_user(payload: RegisterUserRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    phone = sanitize_phone(payload.telefono)
    nombre = payload.nombre.strip()
    
    if not phone or not nombre:
        raise HTTPException(status_code=400, detail="Teléfono y nombre son obligatorios")
    
    reg_ref = db.reference(f'/users_registry/{phone}')
    if reg_ref.get():
        raise HTTPException(status_code=400, detail="Este teléfono ya está registrado")
    
    reg_ref.set({
        "nombre": nombre,
        "telefono": phone,
        "data_path": phone
    })
    
    db.reference(f'/usuarios/{phone}').set({
        "rutina_actual": {},
        "historial": {}
    })
    
    return {"status": "success", "usuario_id": phone, "data_path": phone, "nombre": nombre}

@app.post("/verificar_telefono")
async def verify_phone(payload: VerifyPhoneRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    phone = sanitize_phone(payload.telefono)
    
    if not phone:
        raise HTTPException(status_code=400, detail="Teléfono es obligatorio")
    
    reg_ref = db.reference(f'/users_registry/{phone}')
    user_data = reg_ref.get()
    
    if user_data:
        return {
            "status": "found",
            "nombre": user_data.get("nombre", ""),
            "data_path": user_data.get("data_path", phone),
            "telefono": phone
        }
    else:
        return {"status": "not_found"}

@app.post("/reclamar_legacy")
async def claim_legacy(payload: ClaimLegacyRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    phone = sanitize_phone(payload.telefono)
    legacy = payload.legacy_username.strip().lower()
    
    if not phone:
        raise HTTPException(status_code=400, detail="Teléfono es obligatorio")
    
    if legacy not in ["enrique", "oscar"]:
        raise HTTPException(status_code=400, detail="Usuario legacy no válido")
    
    reg_ref = db.reference(f'/users_registry/{phone}')
    if reg_ref.get():
        raise HTTPException(status_code=400, detail="Este teléfono ya está registrado")
    
    registry = db.reference('/users_registry').get() or {}
    for p, data in registry.items():
        if data.get("legacy_username") == legacy:
            raise HTTPException(status_code=400, detail=f"El usuario {legacy} ya fue reclamado por otro teléfono")
    
    reg_ref.set({
        "nombre": legacy.capitalize(),
        "telefono": phone,
        "data_path": legacy,
        "legacy_username": legacy
    })
    
    return {"status": "success", "usuario_id": phone, "data_path": legacy, "nombre": legacy.capitalize()}

# --- SINCRONIZACIÓN ---

@app.get("/sync/{usuario}")
async def sync_all(usuario: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    
    data_path = resolve_data_path(usuario)
    
    ref_usuario = db.reference(f'/usuarios/{data_path}')
    datos_usuario = ref_usuario.get() or {}
    
    mi_perfil = {
        "rutina_actual": datos_usuario.get("rutina_actual", {}),
        "historial": datos_usuario.get("historial", {})
    }
    
    amigos = {}
    all_users = get_all_registered_users()
    
    for user_info in all_users:
        friend_path = user_info["data_path"]
        if friend_path != data_path:
            ref_amigo = db.reference(f'/usuarios/{friend_path}')
            datos_amigo = ref_amigo.get() or {}
            amigos[user_info["nombre"]] = {
                "rutina_actual": datos_amigo.get("rutina_actual", {}),
                "historial": datos_amigo.get("historial", {})
            }
    
    first_friend = next(iter(amigos.values()), {"rutina_actual": {}, "historial": {}})
    
    return {
        "mi_perfil": mi_perfil,
        "amigos": amigos,
        "perfil_amigo": first_friend
    }

# --- RUTINA ---

@app.post("/{usuario}/definir_rutina")
async def set_routine(usuario: str, payload: UpdateRutinaRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    data_path = resolve_data_path(usuario)
    
    ref = db.reference(f'/usuarios/{data_path}/rutina_actual')
    datos_dict = {k: v.dict() for k, v in payload.dias.items()}
    ref.update(datos_dict)

    return {"status": "success", "message": "Plantilla de rutina actualizada"}

# --- GUARDAR DÍA ---

@app.post("/{usuario}/guardar_dia")
async def save_day(usuario: str, payload: LogDiaRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    data_path = resolve_data_path(usuario)

    ref = db.reference(f'/usuarios/{data_path}/historial/{payload.fecha}')
    ref.set(payload.datos)

    return {
        "status": "success", 
        "message": f"Día {payload.fecha} guardado exitosamente"
    }

# --- BORRAR ---

@app.delete("/{usuario}/historial/{fecha}")
async def delete_history_day(usuario: str, fecha: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    data_path = resolve_data_path(usuario)
    
    ref = db.reference(f'/usuarios/{data_path}/historial/{fecha}')
    ref.delete()
    
    return {"status": "deleted", "target": f"Historial {fecha}"}

@app.delete("/{usuario}/rutina/{dia_semana}")
async def delete_routine_day(usuario: str, dia_semana: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    data_path = resolve_data_path(usuario)
    
    ref = db.reference(f'/usuarios/{data_path}/rutina_actual/{dia_semana}')
    ref.delete()
    
    return {"status": "deleted", "target": f"Plantilla {dia_semana}"}

# --- REGISTROS PREVIOS ---

@app.post("/{usuario}/registros_previos")
async def get_previous_records(usuario: str, payload: PreviousRecordsRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    data_path = resolve_data_path(usuario)

    ref = db.reference(f'/usuarios/{data_path}/historial')
    historial = ref.get() or {}

    try:
        target_date = datetime.strptime(payload.before_date, "%Y-%m-%d")
        start_date = target_date - timedelta(days=payload.days_back)
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha inválido (YYYY-MM-DD)")

    results = {}
    for name in payload.exercise_names:
        normalized = name.strip().lower()
        best_weight = 0.0
        best_info = ""
        best_date = ""

        for fecha, datos in historial.items():
            try:
                workout_date = datetime.strptime(fecha, "%Y-%m-%d")
                if workout_date < start_date or workout_date >= target_date:
                    continue
            except ValueError:
                continue

            rutina = datos.get("rutina_realizada", [])
            if isinstance(rutina, list):
                for ej in rutina:
                    if not isinstance(ej, dict):
                        continue
                    ej_name = ej.get("ejercicio", "").strip().lower()
                    if ej_name == normalized:
                        series = ej.get("series_realizadas", [])
                        if isinstance(series, list):
                            for s in series:
                                if not isinstance(s, dict):
                                    continue
                                kg = float(s.get("kg", 0))
                                if kg > best_weight:
                                    best_weight = kg
                                    unidad = s.get("unidad", "reps")
                                    valor = s.get("valor", "")
                                    reps = s.get("reps", 0)
                                    if unidad != "reps" and valor:
                                        best_info = f"{kg}kg × {valor} {unidad}"
                                    else:
                                        best_info = f"{kg}kg × {reps} reps"
                                    best_date = fecha

                        peso_max = float(ej.get("peso_max", 0))
                        if peso_max > best_weight:
                            best_weight = peso_max
                            best_info = f"{peso_max}kg (máx registrado)"
                            best_date = fecha

        if best_weight > 0:
            results[name] = {
                "max_weight": best_weight,
                "best_series_info": best_info,
                "date": best_date
            }

    return {"status": "success", "records": results}
