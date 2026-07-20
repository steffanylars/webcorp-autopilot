"""
============================================================
WEBCORP AUTOPILOT — Orquestador (Reasoning Layer)
============================================================
El loop central del agente: pregunta ambigua -> Qwen decide que
tools llamar (function calling) -> las tools determinsticas
ejecutan -> Qwen redacta sobre los resultados -> respuesta con
evidencia.

Principios (no negociables, ver README):
- Qwen NUNCA calcula un numero de negocio: los numeros salen de
  las tools. Qwen orquesta e interpreta.
- Toda accion con efecto externo queda PENDIENTE hasta aprobacion
  humana explicita (HITL bloqueante en codigo, no en UI).
- Cada paso queda en el audit log con su rationale.
- Si Qwen Cloud falla, hay fallback a reglas puras (la tool de
  alertas responde sin LLM).

Modos:
- Reactivo: responder(pregunta) — un humano pregunta.
- Proactivo: escaneo_programado() — un scheduler dispara el mismo
  loop sin humano; las notificaciones quedan en cola de aprobacion.
"""
import os
import json
import time
import uuid
import sqlite3
from datetime import datetime, timezone

from agent_tools import (
    TOOLS,
    TOOLS_QUE_REQUIEREN_CONFIRMACION,
    TOOLS_EVIDENCIA_INTERNA,
    ejecutar_tool,
    alertas,
    estimador_wilson,
)

# ------------------------------------------------------------------
# Configuracion
# ------------------------------------------------------------------
QWEN_BASE_URL = os.getenv(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-plus")
API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
MAX_RONDAS = int(os.getenv("MAX_RONDAS_TOOLS", "6"))

AUDIT_LOG_PATH = os.getenv(
    "AUDIT_LOG_PATH", os.path.join(os.path.dirname(__file__), "audit_log.jsonl")
)
PENDIENTES_DB = os.getenv(
    "PENDIENTES_DB_PATH", os.path.join(os.path.dirname(__file__), "pendientes.sqlite")
)

SYSTEM_PROMPT = """Eres el agente de decision operativa de WebCorp, una empresa \
de logistica (3PL) con operacion en 7 paises de Latinoamerica.

Tu trabajo: responder preguntas de negocio sobre la operacion usando SOLO la \
evidencia que devuelven tus herramientas. Reglas estrictas:

1. NUNCA inventes un numero. Todo numero que cites debe venir de una tool.
2. Cita siempre el tamano de muestra (n) junto a cada tasa o porcentaje.
3. Si el estimador declara paises sin cobertura, dilo explicitamente — nunca \
finjas que tienes evidencia de un pais que no esta en los datos.
4. Menciona la fecha de snapshot cuando uses el estimador (es un artefacto \
batch, no tiempo real).
5. Si la accion apropiada es notificar a operaciones, llama la tool de \
notificacion — quedara pendiente de aprobacion humana; dilo en tu respuesta.
6. Responde en {IDIOMA}, directo, sin relleno. Estructura: hallazgo principal \
-> evidencia con numeros -> recomendacion (si aplica).
7. El contexto externo (noticias, bloqueos, clima, protestas) SOLO puede venir \
del resultado de la tool buscar_contexto_externo en esta misma conversacion. \
Si no la llamaste, NO existe contexto externo: no escribas esa seccion, no \
cites medios ni fechas de noticias — eso seria inventar evidencia, la falta \
mas grave. Cuando si la llamaste, presenta su resultado en una seccion aparte \
etiquetada "Posible contexto externo (NO VERIFICADO)", citando las fuentes \
(urls) que la tool devolvio — nunca otras. No lo mezcles con los numeros \
internos ni bases una recomendacion solo en el.
"""


# ------------------------------------------------------------------
# Audit log — append-only, un JSON por linea
# ------------------------------------------------------------------
def _audit(tipo: str, detalle: str, origen_id: str = "", extra: dict = None):
    entrada = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tipo": tipo,
        "detalle": detalle,
        "origen_id": origen_id,
    }
    if extra:
        entrada["extra"] = extra
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entrada, ensure_ascii=False) + "\n")
    return entrada


def leer_audit(ultimos: int = 20) -> list:
    """Para el panel de auditoria del frontend."""
    try:
        with open(AUDIT_LOG_PATH, encoding="utf-8") as f:
            lineas = f.readlines()
        return [json.loads(l) for l in lineas[-ultimos:]]
    except FileNotFoundError:
        return []


# ------------------------------------------------------------------
# Cola de acciones pendientes de aprobacion (HITL persistente)
# ------------------------------------------------------------------
def _pendientes_conn():
    conn = sqlite3.connect(PENDIENTES_DB, timeout=5)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pendientes (
            id TEXT PRIMARY KEY,
            tool TEXT NOT NULL,
            argumentos TEXT NOT NULL,
            rationale TEXT,
            estado TEXT NOT NULL DEFAULT 'pendiente',
            creado REAL NOT NULL,
            resuelto REAL
        )"""
    )
    return conn


def encolar_pendiente(tool: str, argumentos: dict, rationale: str) -> str:
    pid = str(uuid.uuid4())[:8]
    conn = _pendientes_conn()
    conn.execute(
        "INSERT INTO pendientes (id, tool, argumentos, rationale, creado) VALUES (?,?,?,?,?)",
        (pid, tool, json.dumps(argumentos, ensure_ascii=False), rationale, time.time()),
    )
    conn.commit()
    conn.close()
    _audit("hitl_pendiente", f"{tool} en cola de aprobacion: {rationale[:120]}", pid)
    return pid


def listar_pendientes() -> list:
    conn = _pendientes_conn()
    rows = conn.execute(
        "SELECT id, tool, argumentos, rationale, creado FROM pendientes WHERE estado='pendiente' ORDER BY creado"
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "tool": r[1], "argumentos": json.loads(r[2]), "rationale": r[3], "creado": r[4]}
        for r in rows
    ]


def resolver_pendiente(pid: str, aprobado: bool) -> dict:
    """El puente del boton Aprobar/Descartar del frontend."""
    conn = _pendientes_conn()
    row = conn.execute(
        "SELECT tool, argumentos, rationale FROM pendientes WHERE id=? AND estado='pendiente'",
        (pid,),
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "razon": f"pendiente {pid} no existe o ya fue resuelto"}

    tool, argumentos, rationale = row[0], json.loads(row[1]), row[2]
    nuevo_estado = "aprobado" if aprobado else "descartado"
    conn.execute(
        "UPDATE pendientes SET estado=?, resuelto=? WHERE id=?",
        (nuevo_estado, time.time(), pid),
    )
    conn.commit()
    conn.close()

    if not aprobado:
        _audit("hitl_descartado", f"{tool} descartado por humano", pid)
        return {"ok": True, "ejecutado": False, "estado": "descartado"}

    resultado = ejecutar_tool(tool, argumentos, aprobado_por_humano=True)
    _audit(
        "hitl_aprobado_ejecutado",
        f"{tool} aprobado por humano y ejecutado",
        pid,
        extra={"resultado": resultado},
    )
    return {"ok": True, "ejecutado": True, "resultado": resultado}


# ------------------------------------------------------------------
# Cliente Qwen (OpenAI-compatible). Inyectable para testing.
# ------------------------------------------------------------------
def _cliente_real():
    from openai import OpenAI

    if not API_KEY:
        raise RuntimeError(
            "DASHSCOPE_API_KEY no esta definida. "
            "export DASHSCOPE_API_KEY='sk-...' antes de correr."
        )
    return OpenAI(api_key=API_KEY, base_url=QWEN_BASE_URL)


# ------------------------------------------------------------------
# El loop del agente
# ------------------------------------------------------------------
IDIOMAS = {
    "es": "espanol",
    "en": "ingles (English) — reply entirely in English",
    "fr": "frances (francais) — reponds entierement en francais",
}


def responder(pregunta: str, client=None, model: str = None, idioma: str = "es") -> dict:
    """
    Modo reactivo. Devuelve:
      {
        "respuesta": str,            # redaccion final de Qwen
        "tools_usadas": [...],       # que se ejecuto, con args
        "pendientes": [...],         # acciones esperando aprobacion humana
        "fallback": bool,            # True si Qwen fallo y respondieron las reglas
      }
    """
    model = model or QWEN_MODEL
    origen = str(uuid.uuid4())[:8]
    _audit("pregunta", pregunta, origen)

    try:
        client = client or _cliente_real()
        return _loop_qwen(pregunta, client, model, origen, idioma)
    except Exception as e:
        # FALLBACK: Qwen no disponible -> reglas puras responden.
        _audit("fallback_activado", f"Qwen no disponible ({type(e).__name__}: {e})", origen)
        try:
            data = alertas()
            resumen = json.dumps(data, ensure_ascii=False)[:800]
        except Exception:
            data = estimador_wilson(min_n=30, top=5)
            resumen = json.dumps(data["resultados"], ensure_ascii=False)[:800]
        return {
            "respuesta": (
                "[MODO FALLBACK — sin LLM] El motor de reglas responde directo. "
                f"Estado actual segun umbrales: {resumen}"
            ),
            "tools_usadas": [{"tool": "alertas", "via": "fallback"}],
            "pendientes": [],
            "fallback": True,
        }


def _loop_qwen(pregunta: str, client, model: str, origen: str, idioma: str = "es") -> dict:
    mensajes = [
        {"role": "system", "content": SYSTEM_PROMPT.replace("{IDIOMA}", IDIOMAS.get(idioma, IDIOMAS["es"]))},
        {"role": "user", "content": pregunta},
    ]
    tools_usadas = []
    pendientes_creados = []

    for ronda in range(MAX_RONDAS):
        resp = client.chat.completions.create(
            model=model,
            messages=mensajes,
            tools=TOOLS,
            extra_body={"enable_thinking": False},  # gotcha: thinking no es
            # compatible con tool/structured output en Qwen — documentado.
        )
        msg = resp.choices[0].message

        if not getattr(msg, "tool_calls", None):
            # Qwen termino de razonar -> respuesta final
            _audit("respuesta", (msg.content or "")[:200], origen)
            return {
                "respuesta": msg.content or "",
                "tools_usadas": tools_usadas,
                "pendientes": pendientes_creados,
                "fallback": False,
            }

        # Qwen pidio tools: ejecutarlas y devolverle los resultados
        mensajes.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            nombre = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
                _audit("tool_args_invalidos", f"{nombre}: argumentos no parseables", origen)

            if nombre == "buscar_contexto_externo" and not any(
                t["tool"] in TOOLS_EVIDENCIA_INTERNA for t in tools_usadas
            ):
                # Regla de diseño en codigo: el contexto externo nunca es el
                # punto de entrada — solo explica una alerta ya confirmada
                # por evidencia interna en este mismo loop.
                _audit(
                    "trust_bloqueado",
                    "buscar_contexto_externo bloqueado: no hay evidencia interna previa en este loop",
                    origen,
                )
                resultado = {
                    "ejecutado": False,
                    "razon": (
                        "Bloqueado por la capa Trust: buscar_contexto_externo solo "
                        "se dispara sobre una anomalia ya confirmada. Llama primero "
                        "estimador_wilson, alertas o efectividad_semanal y confirma "
                        "la anomalia con datos internos."
                    ),
                }
            elif nombre in TOOLS_QUE_REQUIEREN_CONFIRMACION:
                # HITL: NO se ejecuta. Se encola y se le informa a Qwen.
                rationale = args.get("mensaje", "")[:300]
                pid = encolar_pendiente(nombre, args, rationale)
                pendientes_creados.append(
                    {"id": pid, "tool": nombre, "argumentos": args}
                )
                resultado = {
                    "ejecutado": False,
                    "estado": "pendiente_aprobacion_humana",
                    "id_pendiente": pid,
                    "nota": (
                        "Accion con efecto externo: encolada para aprobacion "
                        "humana. Informa al usuario que quedo pendiente."
                    ),
                }
            else:
                t0 = time.time()
                try:
                    resultado = ejecutar_tool(nombre, args)
                    _audit(
                        "tool_ejecutada",
                        f"{nombre}({json.dumps(args, ensure_ascii=False)[:150]}) "
                        f"en {(time.time()-t0)*1000:.0f}ms",
                        origen,
                    )
                    tools_usadas.append({"tool": nombre, "argumentos": args})
                except Exception as e:
                    # Una tool caida no tumba el loop: se le informa a Qwen
                    # para que use otra evidencia o declare el hueco.
                    resultado = {
                        "ejecutado": False,
                        "error": f"{type(e).__name__}: {e}",
                        "nota": (
                            "Esta tool fallo. Usa la evidencia de las demas tools "
                            "o declara explicitamente que ese dato no esta disponible."
                        ),
                    }
                    _audit("tool_error", f"{nombre}: {type(e).__name__}: {e}", origen)

            mensajes.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                }
            )

    _audit("max_rondas", f"se alcanzo MAX_RONDAS={MAX_RONDAS}", origen)
    return {
        "respuesta": "No pude cerrar la respuesta en el limite de rondas.",
        "tools_usadas": tools_usadas,
        "pendientes": pendientes_creados,
        "fallback": False,
    }


# ------------------------------------------------------------------
# Modo proactivo — el mismo loop, disparado por un scheduler.
# Sin Batch API (decision documentada: a esta escala, la inferencia
# en tiempo real dentro de la cuota gratis es mas economica; a escala
# de produccion, esto migra a Batch API con 50% de descuento).
# ------------------------------------------------------------------
PREGUNTAS_ESCANEO = [
    "Revisa las alertas activas y resume los problemas mas urgentes de la operacion.",
    "Cuales son los 3 peores pares municipio-carrier con evidencia suficiente y que deberiamos hacer?",
]


def escaneo_programado(client=None) -> dict:
    """
    Corre el escaneo proactivo completo. Disenado para dispararse
    desde cron/systemd timer en la instancia de Alibaba:
        0 4 * * 2  cd /app && python -c "import agente; agente.escaneo_programado()"
    Las notificaciones que el agente proponga quedan en la cola HITL —
    el operador las encuentra en la manana esperando aprobacion.
    """
    inicio = time.time()
    _audit("escaneo_inicio", f"escaneo proactivo con {len(PREGUNTAS_ESCANEO)} preguntas")
    resultados = []
    for p in PREGUNTAS_ESCANEO:
        resultados.append(responder(p, client=client))
    resumen = {
        "preguntas": len(PREGUNTAS_ESCANEO),
        "pendientes_generados": sum(len(r["pendientes"]) for r in resultados),
        "fallbacks": sum(1 for r in resultados if r["fallback"]),
        "duracion_s": round(time.time() - inicio, 1),
        "resultados": resultados,
    }
    _audit(
        "escaneo_fin",
        f"{resumen['pendientes_generados']} acciones en cola de aprobacion, "
        f"{resumen['duracion_s']}s",
    )
    return resumen


# ------------------------------------------------------------------
# CLI de prueba: python agente.py "tu pregunta"
# ------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    pregunta = (
        " ".join(sys.argv[1:])
        or "Por que FORZA esta tan mal en Escuintla y que deberiamos hacer?"
    )
    print(f"\n>>> {pregunta}\n")
    r = responder(pregunta)
    print(r["respuesta"])
    print(f"\n--- tools usadas: {[t['tool'] for t in r['tools_usadas']]}")
    if r["pendientes"]:
        print(f"--- acciones esperando tu aprobacion: {len(r['pendientes'])}")
        for p in r["pendientes"]:
            print(f"    [{p['id']}] {p['tool']}: {p['argumentos'].get('asunto','')}")
        print("    (aprobar con: python -c \"import agente; print(agente.resolver_pendiente('ID', True))\")")
    if r["fallback"]:
        print("--- ADVERTENCIA: respondio el fallback de reglas, no Qwen")
