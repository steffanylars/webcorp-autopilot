"""
============================================================
WEBCORP AUTOPILOT — Servidor del agente (puente HTTP)
============================================================
Expone agente.py como API HTTP para que el frontend (el mockup
v3, o cualquier UI) pueda:
  - Hacer una pregunta y recibir la respuesta del agente
  - Ver que acciones estan esperando aprobacion humana
  - Aprobar o rechazar esas acciones (el boton "Aprobar" real)
  - Ver el audit log en vivo

Correr:
    uvicorn server:app --reload --port 8001

Endpoints:
    POST /preguntar         {"pregunta": "..."}
    GET  /pendientes
    POST /pendientes/{id}/aprobar
    POST /pendientes/{id}/rechazar
    GET  /audit?n=20
    POST /escaneo           dispara el modo proactivo manualmente
"""
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agente
import agent_tools

app = FastAPI(title="WebCorp Autopilot — Agent API")

# CORS abierto para desarrollo/demo. En produccion, restringir a
# los dominios reales del frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class PreguntaIn(BaseModel):
    pregunta: str
    idioma: str = "es"   # es | en | fr — el panel manda su idioma activo


class AprobarIn(BaseModel):
    idioma: str = "es"   # idioma de los artefactos de remediacion


@app.get("/")
def salud():
    return {"status": "ok", "servicio": "WebCorp Autopilot Agent API"}


@app.post("/preguntar")
def preguntar(body: PreguntaIn):
    """El agente responde. Si propone acciones con efecto externo,
    quedan en /pendientes esperando aprobacion — no se ejecutan aqui."""
    if not body.pregunta or not body.pregunta.strip():
        raise HTTPException(400, "La pregunta no puede venir vacia")
    return agente.responder(body.pregunta.strip(), idioma=body.idioma)


@app.get("/pendientes")
def pendientes():
    """Lo que el panel HITL del frontend debe pintar como
    'esperando tu aprobacion'."""
    return {"pendientes": agente.listar_pendientes()}


@app.post("/pendientes/{pid}/aprobar")
def aprobar(pid: str, body: AprobarIn = None):
    idioma = body.idioma if body else "es"
    res = agente.resolver_pendiente(pid, aprobado=True, idioma=idioma)
    if not res["ok"]:
        raise HTTPException(404, res.get("razon", "no encontrado"))
    return res


@app.post("/pendientes/{pid}/rechazar")
def rechazar(pid: str):
    res = agente.resolver_pendiente(pid, aprobado=False)
    if not res["ok"]:
        raise HTTPException(404, res.get("razon", "no encontrado"))
    return res


@app.get("/audit")
def audit(n: int = 20):
    return {"entradas": agente.leer_audit(n)}


@app.post("/escaneo")
def escaneo():
    """Dispara el modo proactivo manualmente (para demo/testing).
    En produccion esto lo llama un cron/scheduler, no un click."""
    return agente.escaneo_programado()


@app.get("/tools/courier_zona")
def tools_courier_zona(pais: str = "GT", desde: str = "", hasta: str = "", couriers: str = ""):
    """Passthrough de SOLO LECTURA para el mapa del panel: la misma tool
    que usa el agente (con su fallback sintetico automatico), un solo origen."""
    return agent_tools.courier_zona(pais=pais, desde=desde, hasta=hasta, couriers=couriers)


@app.get("/tools/operacion_paises")
def tools_operacion_paises():
    """Passthrough de SOLO LECTURA para el mapa regional de Centroamerica."""
    return agent_tools.operacion_paises()


@app.get("/tools/optimizar_asignacion")
def tools_optimizar(min_n: int = 30, margen_capacidad: float = 0.25, top: int = 10):
    """Plan optimo de reasignacion zona-carrier (MILP sobre Wilson LCB).
    Solo recomienda y genera descargables — no ejecuta ningun cambio."""
    return agent_tools.optimizar_asignacion(min_n=min_n, margen_capacidad=margen_capacidad, top=top)


# El panel HITL se sirve desde el mismo proceso: http://host/panel
_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/panel", StaticFiles(directory=_FRONTEND_DIR, html=True), name="panel")

# Artefactos de remediacion (PDF/Excel/borradores) — generados tras
# aprobacion humana; el directorio esta en .gitignore (contiene PII).
_ARTEFACTOS_DIR = os.getenv(
    "ARTEFACTOS_DIR", os.path.join(os.path.dirname(__file__), "artefactos")
)
os.makedirs(_ARTEFACTOS_DIR, exist_ok=True)
app.mount("/artefactos", StaticFiles(directory=_ARTEFACTOS_DIR), name="artefactos")
