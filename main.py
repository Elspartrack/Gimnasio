import os
import json
import hashlib
import secrets
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
    password: Optional[str] = None
    country_code: Optional[str] = None

class ClaimLegacyRequest(BaseModel):
    telefono: str
    legacy_username: str
    country_code: Optional[str] = None
    password: Optional[str] = None

class VerifyPhoneRequest(BaseModel):
    telefono: str
    password: Optional[str] = None

class SetPasswordRequest(BaseModel):
    telefono: str
    password: str

class SetCountryCodeRequest(BaseModel):
    telefono: str
    country_code: str

class AdminResetPasswordRequest(BaseModel):
    telefono: str
    admin_key: str

class PreviousRecordsRequest(BaseModel):
    exercise_names: List[str]
    before_date: str
    days_back: int = 14

class PrivacySettingsRequest(BaseModel):
    show_body_weight: bool = True
    show_notes: bool = True
    show_exercise_details: bool = True

class ChangePasswordRequest(BaseModel):
    telefono: str
    old_password: str
    new_password: str

# --- SEGURIDAD ---

def verify_key(x_api_key: str):
    env_key = os.getenv("API_KEY")
    if not env_key:
        raise HTTPException(status_code=500, detail="Error de config: API_KEY no establecida en el servidor")
    if x_api_key != env_key:
        raise HTTPException(status_code=403, detail="Acceso denegado: Clave incorrecta")

# --- PASSWORD HELPERS ---

def hash_password(password: str) -> str:
    """Hash password with PBKDF2-SHA256 + random salt"""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000).hex()
    return f"{salt}${h}"

def verify_password(password: str, stored_hash: str) -> bool:
    """Verify password against stored PBKDF2 hash"""
    try:
        salt, expected = stored_hash.split('$', 1)
        computed = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000).hex()
        return secrets.compare_digest(computed, expected)
    except Exception:
        return False

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
    
    user_data = {
        "nombre": nombre,
        "telefono": phone,
        "data_path": phone
    }
    
    if payload.password:
        user_data["password_hash"] = hash_password(payload.password)
    
    if payload.country_code:
        user_data["country_code"] = payload.country_code
    
    reg_ref.set(user_data)
    
    db.reference(f'/usuarios/{phone}').set({
        "rutina_actual": {},
        "historial": {}
    })
    
    return {
        "status": "success",
        "usuario_id": phone,
        "data_path": phone,
        "nombre": nombre,
        "has_password": bool(payload.password),
        "country_code": payload.country_code or ""
    }

@app.post("/verificar_telefono")
async def verify_phone(payload: VerifyPhoneRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    phone = sanitize_phone(payload.telefono)
    
    if not phone:
        raise HTTPException(status_code=400, detail="Teléfono es obligatorio")
    
    reg_ref = db.reference(f'/users_registry/{phone}')
    user_data = reg_ref.get()
    
    if user_data:
        has_password = bool(user_data.get("password_hash"))
        has_country_code = bool(user_data.get("country_code"))
        
        # If client sent password and user has one, verify it
        if payload.password and has_password:
            if not verify_password(payload.password, user_data["password_hash"]):
                raise HTTPException(status_code=401, detail="Contraseña incorrecta")
        
        return {
            "status": "found",
            "nombre": user_data.get("nombre", ""),
            "data_path": user_data.get("data_path", phone),
            "telefono": phone,
            "has_password": has_password,
            "needs_password_setup": not has_password,
            "country_code": user_data.get("country_code", ""),
            "needs_country_code": not has_country_code
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
        "legacy_username": legacy,
        **({
            "country_code": payload.country_code
        } if payload.country_code else {}),
        **({
            "password_hash": hash_password(payload.password)
        } if payload.password else {})
    })
    
    return {
        "status": "success",
        "usuario_id": phone,
        "data_path": legacy,
        "nombre": legacy.capitalize(),
        "has_password": bool(payload.password),
        "country_code": payload.country_code or ""
    }

# --- SINCRONIZACIÓN ---

@app.get("/{usuario}/privacy_settings")
async def get_privacy_settings(usuario: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    phone = sanitize_phone(usuario)
    reg = db.reference(f'/users_registry/{phone}').get()
    if not reg:
        return {"status": "success", "settings": {"show_body_weight": True, "show_notes": True, "show_exercise_details": True}}
    settings = reg.get("privacy_settings", {"show_body_weight": True, "show_notes": True, "show_exercise_details": True})
    return {"status": "success", "settings": settings}

@app.post("/{usuario}/privacy_settings")
async def set_privacy_settings(usuario: str, payload: PrivacySettingsRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    phone = sanitize_phone(usuario)
    reg_ref = db.reference(f'/users_registry/{phone}')
    reg = reg_ref.get()
    if not reg:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    reg_ref.child("privacy_settings").set({
        "show_body_weight": payload.show_body_weight,
        "show_notes": payload.show_notes,
        "show_exercise_details": payload.show_exercise_details
    })
    return {"status": "success", "message": "Ajustes de privacidad actualizados"}

def apply_privacy_filter(historial: dict, privacy: dict) -> dict:
    """Filtra el historial de un amigo según sus ajustes de privacidad"""
    if not historial or not privacy:
        return historial
    
    show_body = privacy.get("show_body_weight", True)
    show_notes = privacy.get("show_notes", True)
    show_details = privacy.get("show_exercise_details", True)
    
    if show_body and show_notes and show_details:
        return historial
    
    filtered = {}
    for fecha, datos in historial.items():
        if not isinstance(datos, dict):
            filtered[fecha] = datos
            continue
        day = dict(datos)
        resumen = day.get("resumen_dia", {})
        if isinstance(resumen, dict):
            resumen = dict(resumen)
            if not show_body:
                resumen.pop("peso_corporal", None)
            if not show_notes:
                resumen.pop("notas_generales", None)
            day["resumen_dia"] = resumen
        if not show_details:
            day.pop("rutina_realizada", None)
        filtered[fecha] = day
    return filtered

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
    registry = db.reference('/users_registry').get() or {}
    
    for user_info in all_users:
        friend_path = user_info["data_path"]
        if friend_path != data_path:
            ref_amigo = db.reference(f'/usuarios/{friend_path}')
            datos_amigo = ref_amigo.get() or {}
            
            # Buscar ajustes de privacidad del amigo
            friend_phone = user_info.get("telefono", "")
            friend_privacy = {}
            if friend_phone and friend_phone in registry:
                friend_privacy = registry[friend_phone].get("privacy_settings", {})
            
            friend_historial = datos_amigo.get("historial", {})
            filtered_historial = apply_privacy_filter(friend_historial, friend_privacy)
            
            amigos[user_info["nombre"]] = {
                "rutina_actual": datos_amigo.get("rutina_actual", {}),
                "historial": filtered_historial
            }
    
    first_friend = next(iter(amigos.values()), {"rutina_actual": {}, "historial": {}})
    
    # Build user metadata for v3 clients
    user_reg = db.reference(f'/users_registry/{usuario}').get() or {}
    user_meta = {
        "has_password": bool(user_reg.get("password_hash")),
        "needs_password_setup": not bool(user_reg.get("password_hash")),
        "country_code": user_reg.get("country_code", ""),
        "needs_country_code": not bool(user_reg.get("country_code"))
    }
    
    return {
        "mi_perfil": mi_perfil,
        "amigos": amigos,
        "perfil_amigo": first_friend,
        "user_meta": user_meta
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

# --- SEGURIDAD DE CUENTAS (v3) ---

@app.post("/establecer_password")
async def set_password(payload: SetPasswordRequest, x_api_key: str = Header(None)):
    """Establecer contraseña para usuario existente"""
    verify_key(x_api_key)
    phone = sanitize_phone(payload.telefono)
    
    if not phone or not payload.password:
        raise HTTPException(status_code=400, detail="Teléfono y contraseña son obligatorios")
    
    if len(payload.password) < 4:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 4 caracteres")
    
    reg_ref = db.reference(f'/users_registry/{phone}')
    reg = reg_ref.get()
    
    if not reg:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    reg_ref.child("password_hash").set(hash_password(payload.password))
    
    return {"status": "success", "message": "Contraseña establecida"}

@app.post("/cambiar_password")
async def change_password(payload: ChangePasswordRequest, x_api_key: str = Header(None)):
    """Cambiar contraseña (requiere contraseña actual)"""
    verify_key(x_api_key)
    phone = sanitize_phone(payload.telefono)
    
    if not phone:
        raise HTTPException(status_code=400, detail="Teléfono es obligatorio")
    
    if len(payload.new_password) < 4:
        raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 4 caracteres")
    
    reg_ref = db.reference(f'/users_registry/{phone}')
    reg = reg_ref.get()
    
    if not reg:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    stored_hash = reg.get("password_hash")
    if not stored_hash:
        raise HTTPException(status_code=400, detail="No tienes contraseña configurada. Usa establecer_password.")
    
    if not verify_password(payload.old_password, stored_hash):
        raise HTTPException(status_code=401, detail="Contraseña actual incorrecta")
    
    reg_ref.child("password_hash").set(hash_password(payload.new_password))
    
    return {"status": "success", "message": "Contraseña cambiada exitosamente"}

@app.post("/establecer_prefijo")
async def set_country_code(payload: SetCountryCodeRequest, x_api_key: str = Header(None)):
    """Establecer prefijo telefónico para usuario existente"""
    verify_key(x_api_key)
    phone = sanitize_phone(payload.telefono)
    
    if not phone or not payload.country_code:
        raise HTTPException(status_code=400, detail="Teléfono y prefijo son obligatorios")
    
    reg_ref = db.reference(f'/users_registry/{phone}')
    reg = reg_ref.get()
    
    if not reg:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    reg_ref.child("country_code").set(payload.country_code)
    
    return {"status": "success", "message": "Prefijo actualizado"}

@app.post("/admin/reset_password")
async def admin_reset_password(payload: AdminResetPasswordRequest, x_api_key: str = Header(None)):
    """Admin: resetear contraseña de un usuario (contactar con Enrique)"""
    verify_key(x_api_key)
    
    admin_key = os.getenv("ADMIN_KEY", "gymbros_admin_enrique_2024")
    if payload.admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Clave de administrador incorrecta")
    
    phone = sanitize_phone(payload.telefono)
    reg_ref = db.reference(f'/users_registry/{phone}')
    reg = reg_ref.get()
    
    if not reg:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    reg_ref.child("password_hash").delete()
    
    return {"status": "success", "message": "Contraseña reseteada. El usuario deberá establecer una nueva."}
