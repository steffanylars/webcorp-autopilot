"""
============================================================
WEBCORP AUTOPILOT — Tools (Action Layer)
============================================================
Envuelve el backend FastAPI ya existente (main.py) como tools de
function calling para Qwen. Nada de lógica de negocio nueva aquí:
las reglas ya viven en el backend (Wilson LCB, umbrales de alerta).
Este archivo solo define el contrato (schema) y el adaptador (handler).

Principio: deterministic core, LLM como orquestación.
El LLM nunca decide el número — solo interpreta el intent,
llama la tool correcta, y redacta la respuesta sobre el resultado.
"""
import os
import csv
import json
import time
import hashlib
import requests
from datetime import datetime, timezone

BACKEND_URL = os.getenv("WEBCORP_BACKEND_URL", "http://localhost:8000")
ESTIMADOR_CSV = os.getenv(
    "ESTIMADOR_CSV_PATH",
    os.path.join(os.path.dirname(__file__), "estimador_municipio_mensajeria.csv"),
)
NOTIFY_WEBHOOK_URL = os.getenv("NOTIFY_WEBHOOK_URL", "")  # Slack/DingTalk incoming webhook
DEDUPE_WINDOW_SECONDS = int(os.getenv("DEDUPE_WINDOW_SECONDS", str(6 * 3600)))  # 6h

# Paises con evidencia confiable en el estimador Wilson HOY.
# Se declara explicito porque el CSV solo cubre GT (38.7K filas) y
# SV (153 filas, 8 municipios); HND/PAN/CR/NIC/ARG no tienen cobertura
# en este artefacto aunque existan en la operacion real.
# Cambia esto SOLO cuando el estimador se regenere con esos paises.
PAISES_CON_COBERTURA_WILSON = {"GT", "SV"}

DEDUPE_DB = os.getenv("DEDUPE_DB_PATH", os.path.join(os.path.dirname(__file__), "dedupe.sqlite"))
# Dedupe persistente en SQLite: sobrevive reinicios del proceso y funciona
# entre multiples workers en la misma maquina (SQLite serializa escrituras).
# Limitacion conocida y documentada: para multiples instancias detras de un
# load balancer se necesita un store compartido (Redis) — ver LIMITACIONES en README.

def _dedupe_conn():
    import sqlite3
    conn = sqlite3.connect(DEDUPE_DB, timeout=5)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS envios (clave TEXT PRIMARY KEY, ts REAL NOT NULL)"
    )
    return conn


def _snapshot_estimador() -> str:
    """
    Fecha del snapshot del estimador (es un artefacto batch, no tiempo real).
    ESTIMADOR_SNAPSHOT_DATE manda si esta definida — el fallback al mtime
    del CSV miente despues de un git clone (git no preserva mtimes).
    """
    explicita = os.getenv("ESTIMADOR_SNAPSHOT_DATE", "")
    if explicita:
        return explicita
    try:
        ts = os.path.getmtime(ESTIMADOR_CSV)
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except OSError:
        return "desconocido"

# ------------------------------------------------------------------
# 1) estimador_wilson — municipio × carrier, ranqueado por Wilson LCB
#    Fuente: estimador_municipio_mensajeria.csv (ya generado por
#    estimador_efectividad.py, que solo hace SELECT sobre la DB).
# ------------------------------------------------------------------

_estimador_cache = {"mtime": None, "rows": None}

def _cargar_estimador():
    """
    Carga el CSV una sola vez y lo cachea en memoria. Se recarga
    automaticamente solo si el archivo cambio (mtime distinto) —
    p.ej. cuando estimador_efectividad.py regenera el snapshot.
    Evita releer disco en cada query del agente.
    """
    mtime = os.path.getmtime(ESTIMADOR_CSV)
    if _estimador_cache["mtime"] != mtime:
        with open(ESTIMADOR_CSV, newline="", encoding="utf-8") as f:
            _estimador_cache["rows"] = list(csv.DictReader(f))
        _estimador_cache["mtime"] = mtime
    return _estimador_cache["rows"]


def estimador_wilson(depto: str = "", municipio: str = "", carrier: str = "",
                      min_n: int = 30, top: int = 5) -> dict:
    """
    Rankea pares municipio×carrier por Wilson LCB (no por tasa cruda).

    Declara explicitamente que el estimador SOLO tiene cobertura confiable
    en GT y SV — el resto de la operacion (HND, PAN, CR, NIC, ARG) no tiene
    filas en este CSV, y el agente debe decirlo, no fingir cobertura pareja.
    Tambien declara el snapshot date: este es un artefacto batch, no
    tiempo real.

    Returns:
        dict con 'resultados', 'cobertura' (paises con/sin evidencia),
        'snapshot_date', y 'nota_metodologica'.
    """
    rows = _cargar_estimador()
    if depto:
        rows = [r for r in rows if r["depto"].strip().lower() == depto.strip().lower()]
    if municipio:
        rows = [r for r in rows if r["municipio"].strip().lower() == municipio.strip().lower()]
    if carrier:
        rows = [r for r in rows if r["mensajeria"].strip().upper() == carrier.strip().upper()]

    paises_en_query = {r["pais"].strip().upper() for r in rows} or PAISES_CON_COBERTURA_WILSON
    paises_sin_cobertura = sorted(paises_en_query - PAISES_CON_COBERTURA_WILSON)

    confiables = [r for r in rows if int(r["n"]) >= min_n]
    confiables.sort(key=lambda r: float(r["wilson_lcb"]))

    resultados = [{
        "pais": r["pais"], "depto": r["depto"], "municipio": r["municipio"],
        "carrier": r["mensajeria"], "n": int(r["n"]),
        "tasa_cruda_pct": float(r["tasa_cruda"]), "wilson_lcb_pct": float(r["wilson_lcb"]),
    } for r in confiables[:top]]

    return {
        "resultados": resultados,
        "n_total_pares_confiables": len(confiables),
        "n_total_pares_descartados_por_muestra_chica": len(rows) - len(confiables),
        "cobertura": {
            "paises_con_evidencia": sorted(PAISES_CON_COBERTURA_WILSON),
            "paises_sin_evidencia_en_este_artefacto": paises_sin_cobertura,
        },
        "snapshot_date": _snapshot_estimador(),
        "nota_metodologica": (
            f"Se descartaron pares con n<{min_n} porque la tasa cruda con muestra "
            f"chica no es evidencia confiable. Se ordena por el limite inferior del "
            f"intervalo de Wilson (95% confianza). Este artefacto es un snapshot "
            f"batch, no tiempo real."
        ),
    }


def _validar_comparacion_paises(paises: list) -> None:
    """
    Invariante: nunca comparar paises entre si si alguno no tiene cobertura
    confiable en el estimador Wilson. Lanza ValueError — debe fallar
    ruidoso, no devolver un numero que parece valido pero no lo es.
    """
    sin_cobertura = [p for p in paises if p.upper() not in PAISES_CON_COBERTURA_WILSON]
    if len(paises) > 1 and sin_cobertura:
        raise ValueError(
            f"No se puede comparar {paises}: {sin_cobertura} no tiene cobertura "
            f"confiable en el estimador (solo {sorted(PAISES_CON_COBERTURA_WILSON)})."
        )


TOOL_SCHEMA_ESTIMADOR_WILSON = {
    "type": "function",
    "function": {
        "name": "estimador_wilson",
        "description": (
            "Encuentra los pares municipio-carrier con peor (o mejor) efectividad "
            "real, usando el limite inferior de Wilson en vez de la tasa cruda para "
            "evitar falsos positivos por muestra chica. Usar cuando la pregunta sea "
            "sobre 'donde', 'que zona', 'que carrier' esta fallando o funcionando bien."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "depto": {"type": "string", "description": "Departamento a filtrar, ej. 'Escuintla'. Vacio para todos."},
                "municipio": {"type": "string", "description": "Municipio a filtrar. Vacio para todos."},
                "carrier": {"type": "string", "description": "Mensajeria a filtrar, ej. 'FORZA', 'CARGO', 'MEG'. Vacio para todas."},
                "min_n": {"type": "integer", "description": "Tamano minimo de muestra confiable. Default 30."},
                "top": {"type": "integer", "description": "Cuantos resultados devolver. Default 5."},
            },
            "required": [],
        },
    },
}

# ------------------------------------------------------------------
# 2) efectividad_semanal — serie temporal real, vía backend FastAPI
# ------------------------------------------------------------------

def efectividad_semanal(pais: str = "", cliente: str = "", mensajeria: str = "") -> dict:
    """
    Llama al endpoint real /api/efectividad_semanal del backend WebCorp.
    Devuelve la serie semanal de efectividad, ya calculada con las
    reglas de negocio existentes (EXITO = ENTREGADO / ENTREGADO LIQUIDADO).
    Sin backend, cae al dataset sintetico y lo declara en 'fuente'.
    """
    try:
        r = requests.get(
            f"{BACKEND_URL}/api/efectividad_semanal",
            params={"pais": pais, "cliente": cliente, "mensajeria": mensajeria},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        data["fuente"] = "backend_real"
        return data
    except requests.RequestException:
        data = _demo_data("efectividad_semanal")
        data["fuente"] = "datos_sinteticos_demo"
        data["nota"] = NOTA_SINTETICO
        return data


TOOL_SCHEMA_EFECTIVIDAD_SEMANAL = {
    "type": "function",
    "function": {
        "name": "efectividad_semanal",
        "description": (
            "Devuelve la serie de efectividad semana a semana (ISO) para un pais, "
            "cliente o mensajeria. Usar para preguntas de tendencia: 'como vamos', "
            "'ha mejorado o empeorado', 'que paso esta semana vs la anterior'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pais": {"type": "string", "description": "Codigo de pais: GT, SV, HND, PAN, CR, NIC. Vacio para todos."},
                "cliente": {"type": "string", "description": "Nombre de empresa cliente. Vacio para todos."},
                "mensajeria": {"type": "string", "description": "Carrier a filtrar. Vacio para todos."},
            },
            "required": [],
        },
    },
}

# ------------------------------------------------------------------
# 3) alertas — anomalías por umbral, vía backend FastAPI
#    Las reglas (meta 60%, critico <50%, caida >=3pts mes a mes) ya
#    estan escritas en main.py con su propio rationale de texto.
# ------------------------------------------------------------------

def alertas() -> dict:
    """
    Llama al endpoint real /api/alertas. Cada alerta ya viene con
    severidad, titulo, detalle numerico y accion sugerida — el LLM
    solo redacta sobre esto, no inventa el diagnostico.
    Sin backend, cae al dataset sintetico y lo declara en 'fuente'.
    """
    try:
        r = requests.get(f"{BACKEND_URL}/api/alertas", timeout=10)
        r.raise_for_status()
        data = r.json()
        data["fuente"] = "backend_real"
        return data
    except requests.RequestException:
        data = _demo_data("alertas")
        data["fuente"] = "datos_sinteticos_demo"
        data["nota"] = NOTA_SINTETICO
        return data


TOOL_SCHEMA_ALERTAS = {
    "type": "function",
    "function": {
        "name": "alertas",
        "description": (
            "Devuelve anomalias ya detectadas por reglas de umbral: efectividad "
            "global bajo meta, paises en nivel critico (<50%), y caidas mes a mes "
            ">=3 puntos. Usar para preguntas abiertas tipo 'que esta pasando', "
            "'hay algo raro', 'dame un resumen de problemas'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# ------------------------------------------------------------------
# 4) notificar_operaciones — cierra el ciclo end-to-end.
#    Requiere confirmacion humana (Trust layer la exige antes de llamarla).
#    Dedupe: no manda el mismo aviso 2 veces en la ventana de tiempo.
#    Amarra la notificacion al origen_id de la alerta que la disparo,
#    para trazabilidad de ida y vuelta en el audit log.
# ------------------------------------------------------------------

def _clave_dedupe(asunto: str, destino: str) -> str:
    return hashlib.sha256(f"{asunto}|{destino}".encode()).hexdigest()


def _ordenes_no_entregadas(municipio: str, carrier: str, pais: str = "GT",
                           limit: int = 200) -> list:
    """Ordenes no entregadas del par (para el Excel de call center).
    Backend real primero; sin backend cae al dataset sintetico declarado.
    Uso interno de la remediacion — la PII (telefonos) no pasa por el LLM."""
    try:
        r = requests.get(
            f"{BACKEND_URL}/api/ordenes_no_entregadas",
            params={"municipio": municipio, "carrier": carrier, "pais": pais, "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("ordenes", [])
    except requests.RequestException:
        demo = _demo_data("ordenes_no_entregadas")
        return [o for o in demo
                if o["carrier"].upper() == (carrier or "").upper()
                and o["municipio"].lower() == (municipio or "").lower()] or demo[:10]


def notificar_operaciones(asunto: str, mensaje: str, origen_id: str,
                           destino: str = "operaciones", canal: str = "webhook",
                           caso: dict = None, idioma: str = "es") -> dict:
    """
    Manda una notificacion real a operaciones sobre una excepcion detectada.
    SOLO se llama despues de aprobacion humana explicita (accion con efecto
    externo — ver matriz de riesgo HITL).

    Args:
        asunto: titulo corto de la notificacion.
        mensaje: cuerpo — debe incluir el rationale (por que se manda esto).
        origen_id: identificador de la alerta/deteccion que origino esta
                   notificacion (ej. "estimador_wilson#Escuintla-FORZA-2026-07-02").
                   Se usa para amarrar notificacion <-> causa en el audit log.
        destino: a quien va (equipo, canal, o direccion).
        canal: "webhook" (Slack/DingTalk) o "log" (modo demo sin webhook real).

    Returns:
        dict con 'enviado' (bool), 'deduplicado' (bool si ya se mando
        este mismo aviso en la ventana de tiempo), 'origen_id', y 'timestamp'.
    """
    clave = _clave_dedupe(asunto, destino)
    ahora = time.time()
    conn = _dedupe_conn()
    row = conn.execute("SELECT ts FROM envios WHERE clave = ?", (clave,)).fetchone()
    ultimo_envio = row[0] if row else None

    if ultimo_envio and (ahora - ultimo_envio) < DEDUPE_WINDOW_SECONDS:
        conn.close()
        return {
            "enviado": False,
            "deduplicado": True,
            "razon": (
                f"Mismo aviso ({asunto} -> {destino}) ya enviado hace "
                f"{int((ahora - ultimo_envio) / 60)} min. Ventana de dedupe: "
                f"{DEDUPE_WINDOW_SECONDS // 3600}h. No se reenvia."
            ),
            "origen_id": origen_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    payload = {"asunto": asunto, "mensaje": mensaje, "origen_id": origen_id, "destino": destino}

    if canal == "webhook" and NOTIFY_WEBHOOK_URL:
        r = requests.post(NOTIFY_WEBHOOK_URL, json={"text": f"*{asunto}*\n{mensaje}"}, timeout=10)
        r.raise_for_status()
        enviado = True
    else:
        # Modo demo: no hay webhook configurado — se registra igual,
        # transparente sobre que no salio de verdad (toy data honesto).
        print(f"[NOTIFY-DEMO] {asunto} -> {destino}: {mensaje}")
        enviado = canal == "log"

    conn.execute(
        "INSERT INTO envios (clave, ts) VALUES (?, ?) "
        "ON CONFLICT(clave) DO UPDATE SET ts = excluded.ts",
        (clave, ahora),
    )
    conn.commit()
    conn.close()

    # ── Remediacion: artefactos deterministas, SOLO tras aprobacion ──
    # (esta funcion ya corre detras del checkpoint HITL). Que generar lo
    # decide codigo segun el caso — ver remediacion.py. Un fallo aqui
    # nunca rompe la notificacion ya ejecutada.
    remediacion_info = None
    if caso and caso.get("municipio") and caso.get("carrier"):
        try:
            import remediacion
            est = estimador_wilson(municipio=caso["municipio"],
                                   carrier=caso["carrier"], min_n=1, top=1)
            fila = est["resultados"][0] if est["resultados"] else None
            ordenes = _ordenes_no_entregadas(caso["municipio"], caso["carrier"],
                                             caso.get("pais", "GT"))
            remediacion_info = remediacion.generar(
                asunto=asunto, accion=mensaje, origen_id=origen_id,
                caso=caso, wilson=fila, snapshot=_snapshot_estimador(),
                ordenes=ordenes, idioma=idioma,
            )
        except Exception as e:
            remediacion_info = {"generado": False,
                                "error": f"{type(e).__name__}: {e}"}
    elif caso is not None:
        remediacion_info = {"generado": False,
                            "razon": "caso sin municipio+carrier; no aplica artefacto"}

    return {
        "enviado": enviado,
        "deduplicado": False,
        "origen_id": origen_id,
        "destino": destino,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "remediacion": remediacion_info,
    }


TOOL_SCHEMA_NOTIFICAR = {
    "type": "function",
    "function": {
        "name": "notificar_operaciones",
        "description": (
            "Envia una notificacion real a operaciones sobre una excepcion "
            "detectada. Requiere confirmacion humana antes de llamarse — es "
            "una accion con efecto externo, no una consulta. Deduplica avisos "
            "repetidos en una ventana de tiempo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "asunto": {"type": "string", "description": "Titulo corto de la notificacion."},
                "mensaje": {"type": "string", "description": "Cuerpo del mensaje, debe incluir el rationale."},
                "origen_id": {"type": "string", "description": "ID de la alerta/deteccion que origino esta notificacion, para trazabilidad."},
                "destino": {"type": "string", "description": "A quien va dirigida. Default 'operaciones'."},
                "canal": {"type": "string", "description": "'webhook' o 'log'. Default 'webhook'."},
                "caso": {
                    "type": "object",
                    "description": (
                        "OBLIGATORIO cuando la alerta es de un par municipio-carrier "
                        "especifico: tras la aprobacion humana se generan artefactos de "
                        "remediacion deterministas (PDF a la mensajeria, Excel para call "
                        "center). Solo identifica el caso — los numeros los pone el sistema."
                    ),
                    "properties": {
                        "municipio": {"type": "string"},
                        "depto": {"type": "string"},
                        "carrier": {"type": "string"},
                        "pais": {"type": "string", "description": "Codigo, ej. GT. Default GT."},
                    },
                },
            },
            "required": ["asunto", "mensaje", "origen_id"],
        },
        # requiere_confirmacion: flag de la capa Trust, no de Qwen —
        # se lee en ejecutar_tool() antes de invocar el handler.
        "requiere_confirmacion": True,
    },
}

# ------------------------------------------------------------------
# 4b) productos_real y courier_zona — wrappers HTTP al backend, con
#     fallback AUTOMATICO a datos sinteticos (data/demo_sintetico.json).
#     La instancia publica de Alibaba no tiene acceso a la DB de
#     produccion A PROPOSITO (decision de privacidad de datos): ahi
#     estas tools caen solas al JSON sintetico y lo DECLARAN en el
#     campo 'fuente' para que el agente lo diga, nunca lo esconda.
# ------------------------------------------------------------------

DEMO_DATA_PATH = os.getenv(
    "DEMO_DATA_PATH", os.path.join(os.path.dirname(__file__), "data", "demo_sintetico.json")
)

_demo_cache = {"mtime": None, "data": None}

def _demo_data(clave: str) -> dict:
    """Carga (con cache por mtime) el dataset sintetico de demostracion."""
    mtime = os.path.getmtime(DEMO_DATA_PATH)
    if _demo_cache["mtime"] != mtime:
        with open(DEMO_DATA_PATH, encoding="utf-8") as f:
            _demo_cache["data"] = json.load(f)
        _demo_cache["mtime"] = mtime
    return json.loads(json.dumps(_demo_cache["data"][clave]))  # copia defensiva


NOTA_SINTETICO = (
    "Backend real no disponible: estos son DATOS SINTETICOS de demostracion "
    "(la instancia publica no accede a la base de produccion a proposito). "
    "Declaralo explicitamente en tu respuesta."
)


def productos_real(pais: str = "", desde: str = "", hasta: str = "") -> dict:
    """
    Volumen, efectividad y precio promedio por producto (top 15, n>100),
    desde el endpoint real /api/productos_real (JOIN tbl_orden x tbl_orden_det).
    Si el backend no responde, cae automaticamente al dataset sintetico
    y lo declara en 'fuente'.
    """
    try:
        r = requests.get(
            f"{BACKEND_URL}/api/productos_real",
            params={"pais": pais, "desde": desde, "hasta": hasta},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        data["fuente"] = "backend_real"
        return data
    except requests.RequestException:
        data = _demo_data("productos_real")
        data["fuente"] = "datos_sinteticos_demo"
        data["nota"] = NOTA_SINTETICO
        return data


def courier_zona(pais: str = "GT", desde: str = "", hasta: str = "",
                 couriers: str = "") -> dict:
    """
    Efectividad mensajeria x departamento (alimenta el mapa del panel):
    por_depto, matriz por courier, y plan de accion por gap de efectividad.
    Envuelve GET /api/courier_zona; con backend caido cae automaticamente
    al dataset sintetico (solo GT) y lo declara en 'fuente'.
    """
    try:
        r = requests.get(
            f"{BACKEND_URL}/api/courier_zona",
            params={"pais": pais, "desde": desde, "hasta": hasta, "couriers": couriers},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        data["fuente"] = "backend_real"
        return data
    except requests.RequestException:
        data = _demo_data("courier_zona")
        sel = {c.strip().upper() for c in couriers.split(",") if c.strip()}
        usar = (sel & set(data["couriers"])) if sel else set(data["couriers"])
        if usar != set(data["couriers"]):
            # replica el filtro de couriers del backend sobre el sintetico
            agg = {}
            for depto, celdas in data["matriz"].items():
                o = sum(c["ordenes"] for c in celdas if c["courier"] in usar)
                e = sum(round(c["ordenes"] * c["efectividad"] / 100)
                        for c in celdas if c["courier"] in usar)
                if o:
                    agg[depto] = {"depto": depto, "ordenes": o,
                                  "efectividad": round(100.0 * e / o, 1)}
            data["por_depto"] = sorted(agg.values(), key=lambda x: -x["ordenes"])
            data["seleccionados"] = sorted(usar)
        data["fuente"] = "datos_sinteticos_demo"
        data["nota"] = NOTA_SINTETICO
        return data


def operacion_paises() -> dict:
    """
    Vista regional: ordenes y efectividad por pais (toda Centroamerica)
    y por mensajeria. Envuelve GET /api/operacion; con backend caido cae
    automaticamente al dataset sintetico y lo declara en 'fuente'.
    Alimenta el mapa de Centroamerica del panel.
    """
    try:
        r = requests.get(f"{BACKEND_URL}/api/operacion", timeout=15)
        r.raise_for_status()
        data = r.json()
        data["fuente"] = "backend_real"
        return data
    except requests.RequestException:
        data = _demo_data("operacion")
        data["fuente"] = "datos_sinteticos_demo"
        data["nota"] = NOTA_SINTETICO
        return data


TOOL_SCHEMA_OPERACION_PAISES = {
    "type": "function",
    "function": {
        "name": "operacion_paises",
        "description": (
            "Vista regional de toda Centroamerica: ordenes y efectividad por "
            "pais (GT, SV, HND, PAN, CR, NIC) y por mensajeria. Usar para "
            "preguntas de 'como va cada pais', 'que pais esta peor', "
            "'comparar paises', 'vista regional'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

def optimizar_asignacion(min_n: int = 30, margen_capacidad: float = 0.25,
                          top: int = 10) -> dict:
    """
    Plan OPTIMO de reasignacion zona-carrier para maximizar entregas
    esperadas (Wilson LCB), con restriccion de capacidad. MILP exacto
    (PuLP+CBC) con fallback metaheuristico declarado. Determinisico:
    solo recomienda — ningun cambio se ejecuta sin humanos. Genera
    Excel + PDF descargables en artefactos/.
    """
    import optimizador
    return optimizador.optimizar(min_n=min_n, margen_capacidad=margen_capacidad, top=top)


TOOL_SCHEMA_OPTIMIZAR = {
    "type": "function",
    "function": {
        "name": "optimizar_asignacion",
        "description": (
            "Calcula el plan OPTIMO de reasignacion de mensajerias por zona para "
            "maximizar entregas esperadas (limite inferior de Wilson), con "
            "restriccion de capacidad por carrier. Usar para preguntas tipo 'que "
            "cambios de mensajeria recomiendas', 'como maximizar la efectividad', "
            "'plan de optimizacion'. Solo recomienda: nada se ejecuta."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "min_n": {"type": "integer", "description": "Muestra minima por par para participar. Default 30."},
                "margen_capacidad": {"type": "number", "description": "Crecimiento maximo permitido por carrier (0.25 = +25%). Default 0.25."},
                "top": {"type": "integer", "description": "Cuantas recomendaciones devolver. Default 10."},
            },
            "required": [],
        },
    },
}

TOOL_SCHEMA_PRODUCTOS_REAL = {
    "type": "function",
    "function": {
        "name": "productos_real",
        "description": (
            "Volumen de ordenes, efectividad de entrega y precio promedio por "
            "producto (top 15 con n>100). Usar para preguntas sobre 'que "
            "productos', 'que se vende', 'que producto entrega mejor o peor'. "
            "Solo datos historicos tal cual — no predice ni recomienda."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pais": {"type": "string", "description": "Codigo de pais (GT, SV, ...). Vacio para todos."},
                "desde": {"type": "string", "description": "Fecha inicio YYYY-MM-DD. Vacio para default."},
                "hasta": {"type": "string", "description": "Fecha fin YYYY-MM-DD. Vacio para default."},
            },
            "required": [],
        },
    },
}

TOOL_SCHEMA_COURIER_ZONA = {
    "type": "function",
    "function": {
        "name": "courier_zona",
        "description": (
            "Cruce mensajeria x departamento: efectividad y volumen por zona "
            "(el mapa del panel), desglose por courier, y plan de accion donde "
            "cambiar de mensajeria da el mayor salto. Usar para preguntas de "
            "'que zona', 'que departamento', 'donde falla cada carrier'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pais": {"type": "string", "description": "Codigo de pais. Default GT."},
                "desde": {"type": "string", "description": "Fecha inicio YYYY-MM-DD. Vacio para default."},
                "hasta": {"type": "string", "description": "Fecha fin YYYY-MM-DD. Vacio para default."},
                "couriers": {"type": "string", "description": "Filtro de mensajerias separado por comas, ej. 'FORZA,CARGO'. Vacio para todas."},
            },
            "required": [],
        },
    },
}

# ------------------------------------------------------------------
# 5) buscar_contexto_externo — explica una anomalia YA confirmada.
#    Usa enable_search de Qwen (busqueda web nativa) con enable_source
#    para citar de donde salio cada cosa.
#
#    Reglas de diseño (se cumplen en codigo, no solo en prompt):
#    1. NUNCA es el punto de entrada — el orquestador la bloquea si no
#       se ejecuto antes una tool de evidencia interna (ver agente.py).
#    2. NUNCA se presenta con la confianza de los datos internos — el
#       resultado va etiquetado 'NO VERIFICADO', es hipotesis.
#    3. SIEMPRE cita fuente (enable_source: true).
#
#    Gotcha verificado empiricamente (2026-07-04): el modo
#    OpenAI-compatible NO devuelve search_info aunque se pida
#    enable_source — solo el endpoint NATIVO de DashScope lo expone.
#    Por eso esta tool llama el endpoint nativo via requests.
#    Limitacion honesta: el buscador detras de enable_search es debil
#    en noticias hiperlocales de Centroamerica; "sin hallazgos" es una
#    respuesta frecuente y valida.
# ------------------------------------------------------------------

QWEN_NATIVE_URL = os.getenv(
    "QWEN_NATIVE_URL",
    "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
)

ETIQUETA_NO_VERIFICADO = (
    "NO VERIFICADO — contexto externo de busqueda web. Es una hipotesis "
    "para investigar, no forma parte del calculo interno de efectividad."
)


def buscar_contexto_externo(zona: str, periodo: str, alerta_confirmada: str) -> dict:
    """
    Busca posibles causas externas (bloqueos, clima, protestas) de una
    anomalia que las tools internas YA confirmaron con datos propios.

    Args:
        zona: municipio/departamento/pais de la alerta, ej. "Escuintla, Guatemala".
        periodo: ventana temporal de la anomalia, ej. "junio 2026".
        alerta_confirmada: que detectaron las tools internas (para amarrar
                           la busqueda a su evidencia en el audit log).

    Returns:
        dict con 'hallazgo' (resumen), 'fuentes' (lista con titulo+url),
        'confianza' (siempre la etiqueta NO VERIFICADO), y 'alerta_origen'.
    """
    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if not api_key:
        return {"hallazgo": None, "fuentes": [],
                "error": "DASHSCOPE_API_KEY no definida — no se pudo buscar contexto externo."}

    prompt = (
        f"Busca eventos externos en {zona} durante {periodo} que puedan afectar "
        f"entregas de paqueteria: bloqueos de carretera, protestas, clima extremo, "
        f"accidentes viales, disturbios. Contexto: nuestros datos internos ya "
        f"confirmaron esta anomalia: {alerta_confirmada}. "
        f"Responde en espanol, maximo 4 lineas. Si no encuentras nada relevante "
        f"y especifico de esa zona y periodo, di explicitamente que no hay hallazgos."
    )
    r = requests.post(
        QWEN_NATIVE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": os.getenv("QWEN_MODEL", "qwen-plus"),
            "input": {"messages": [{"role": "user", "content": prompt}]},
            "parameters": {
                "result_format": "message",
                "enable_search": True,
                "search_options": {
                    "enable_source": True,      # regla 3: siempre con fuente
                    "enable_citation": True,
                    "forced_search": True,
                },
            },
        },
        timeout=60,
    )
    r.raise_for_status()
    output = r.json().get("output", {})

    fuentes = [
        {"titulo": s.get("title", ""), "url": s.get("url", ""), "sitio": s.get("site_name", "")}
        for s in output.get("search_info", {}).get("search_results", [])
    ]
    hallazgo = (
        output.get("choices", [{}])[0].get("message", {}).get("content", "")
        or "(sin respuesta del buscador)"
    )
    return {
        "hallazgo": hallazgo,
        "fuentes": fuentes[:5],
        "confianza": ETIQUETA_NO_VERIFICADO,  # regla 2: nunca al nivel del dato interno
        "alerta_origen": alerta_confirmada,
    }


TOOL_SCHEMA_CONTEXTO_EXTERNO = {
    "type": "function",
    "function": {
        "name": "buscar_contexto_externo",
        "description": (
            "Busca en la web posibles causas externas (bloqueos, protestas, clima) "
            "de una anomalia que las tools internas YA confirmaron. SOLO usar "
            "despues de estimador_wilson, alertas o efectividad_semanal — nunca "
            "como primera tool (el orquestador lo bloquea). El resultado es una "
            "hipotesis NO VERIFICADA con fuentes citadas, no un dato interno."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "zona": {"type": "string", "description": "Zona de la alerta, ej. 'Escuintla, Guatemala'."},
                "periodo": {"type": "string", "description": "Ventana temporal, ej. 'junio 2026'."},
                "alerta_confirmada": {"type": "string", "description": "Que confirmaron las tools internas (numeros incluidos), para trazabilidad."},
            },
            "required": ["zona", "periodo", "alerta_confirmada"],
        },
    },
}

# ------------------------------------------------------------------
# Registro para el orquestador Qwen
# ------------------------------------------------------------------

TOOLS = [
    TOOL_SCHEMA_ESTIMADOR_WILSON,
    TOOL_SCHEMA_EFECTIVIDAD_SEMANAL,
    TOOL_SCHEMA_ALERTAS,
    TOOL_SCHEMA_NOTIFICAR,
    TOOL_SCHEMA_PRODUCTOS_REAL,
    TOOL_SCHEMA_COURIER_ZONA,
    TOOL_SCHEMA_OPERACION_PAISES,
    TOOL_SCHEMA_OPTIMIZAR,
    TOOL_SCHEMA_CONTEXTO_EXTERNO,
]

TOOL_HANDLERS = {
    "estimador_wilson": estimador_wilson,
    "efectividad_semanal": efectividad_semanal,
    "alertas": alertas,
    "notificar_operaciones": notificar_operaciones,
    "productos_real": productos_real,
    "courier_zona": courier_zona,
    "operacion_paises": operacion_paises,
    "optimizar_asignacion": optimizar_asignacion,
    "buscar_contexto_externo": buscar_contexto_externo,
}

# Tools cuyo resultado ES evidencia interna. buscar_contexto_externo solo
# puede dispararse si al menos una de estas ya corrio en el mismo loop
# (regla 1 — el orquestador la hace cumplir en codigo).
TOOLS_EVIDENCIA_INTERNA = {
    "estimador_wilson", "efectividad_semanal", "alertas",
    "productos_real", "courier_zona", "operacion_paises",
    "optimizar_asignacion",
}

TOOLS_QUE_REQUIEREN_CONFIRMACION = {
    t["function"]["name"] for t in TOOLS if t["function"].get("requiere_confirmacion")
}


def ejecutar_tool(nombre: str, argumentos: dict, aprobado_por_humano: bool = False) -> dict:
    """
    Adaptador único que llama al handler correcto — este es el punto
    de entrada de la capa Trust.

    Si la tool requiere confirmacion (ver requiere_confirmacion en su
    schema) y no viene aprobado_por_humano=True, se rechaza sin ejecutar.
    Esto es el checkpoint HITL hecho codigo, no solo UI.
    """
    if nombre not in TOOL_HANDLERS:
        raise ValueError(f"Tool desconocida: {nombre}")

    if nombre in TOOLS_QUE_REQUIEREN_CONFIRMACION and not aprobado_por_humano:
        return {
            "ejecutado": False,
            "razon": (
                f"'{nombre}' requiere confirmacion humana antes de ejecutarse "
                f"(accion con efecto externo). No se ejecuto."
            ),
        }

    return TOOL_HANDLERS[nombre](**argumentos)


if __name__ == "__main__":
    # Prueba rápida sin necesitar el backend levantado (solo la tool 1,
    # que lee el CSV local directo).
    print(json.dumps(
        estimador_wilson(carrier="FORZA", min_n=30, top=5),
        indent=2, ensure_ascii=False,
    ))
