"""
============================================================
WEBCORP AUTOPILOT — Artefactos de remediacion
============================================================
Genera los artefactos que cierran el ciclo "from system alerts to
automated remediation" (texto literal del Track 4), SOLO despues de
la aprobacion humana (se invoca desde notificar_operaciones, que ya
esta detras del checkpoint HITL).

Reglas deterministicas — las decide ESTE codigo, no el LLM:
  - Caso puntual (municipio + carrier)  -> 1 PDF con marca dirigido a
    esa mensajeria, + borrador de correo a la mensajeria.
  - Ordenes no entregadas con clientes  -> 1 Excel para call center,
    + borrador de correo a call center.
  - Ambos pueden coexistir (PDF responsabiliza al carrier; Excel
    recupera clientes) — las dos unicas areas soportadas por ahora.

Todo el contenido es deterministico: los numeros vienen del estimador
Wilson y del endpoint de ordenes, la accion sugerida viene del sistema
de alertas. El LLM no redacta ni calcula nada aqui.

El correo NUNCA se envia automaticamente: se genera el borrador (.txt)
y un link mailto: para abrirlo en el cliente de correo del humano.

PII: el Excel contiene telefonos de clientes (uso interno de call
center). El directorio de artefactos esta en .gitignore y nunca se
publica. La PII no pasa por el LLM en ningun momento.

Dependencias: weasyprint (PDF; en macOS requiere
DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib para hallar pango; en
Ubuntu, libpango-1.0-0). Si weasyprint no esta disponible, cae a un
HTML imprimible con la misma plantilla — el flujo nunca se rompe.
"""
import os
import json
import urllib.parse
from datetime import datetime, timezone

EMAIL_CALLCENTER = os.getenv("EMAIL_CALLCENTER", "callcenter@webcorp.example")
# Dominio .example (RFC 2606): placeholder deliberado hasta tener los reales.
EMAIL_CARRIER_FMT = os.getenv("EMAIL_CARRIER_FMT", "operaciones@{carrier}.example")


def _dir_artefactos() -> str:
    d = os.getenv("ARTEFACTOS_DIR", os.path.join(os.path.dirname(__file__), "artefactos"))
    os.makedirs(d, exist_ok=True)
    return d


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (s or "").lower()).strip("-")[:40]


# ------------------------------------------------------------------
# Plantillas (deterministicas — solo se llenan espacios con datos
# ya calculados por las tools)
# ------------------------------------------------------------------

_CSS_MARCA = """
  @page { size: letter; margin: 2cm;
    @bottom-center { content: "WebCorp Autopilot · generado tras aprobación humana · " string(origen);
                     font-size: 8pt; color: #64748B; } }
  body { font-family: Helvetica, Arial, sans-serif; color: #0F172A; font-size: 11pt; }
  .header { background: linear-gradient(135deg, #2563EB 0%, #14B8A6 100%);
            color: #fff; padding: 18px 22px; border-radius: 10px; }
  .header h1 { margin: 0; font-size: 17pt; }
  .header p { margin: 4px 0 0; font-size: 9.5pt; opacity: .9; }
  h2 { font-size: 12pt; color: #1D4ED8; margin: 22px 0 8px; }
  table { border-collapse: collapse; width: 100%; font-size: 10pt; }
  th { text-align: left; background: #F1F5F9; color: #334155; padding: 7px 10px;
       border-bottom: 2px solid #CBD5E1; }
  td { padding: 6px 10px; border-bottom: 1px solid #E2E8F0; }
  .rojo { color: #B91C1C; font-weight: bold; }
  .accion { background: #FEF3C7; border-left: 4px solid #F59E0B; padding: 12px 15px;
            border-radius: 6px; margin-top: 8px; }
  .meta { font-size: 8.5pt; color: #64748B; margin-top: 26px;
          border-top: 1px solid #E2E8F0; padding-top: 8px; }
"""


def _html_pdf(datos: dict) -> str:
    c, f = datos["caso"], datos.get("wilson") or {}
    filas_wilson = ""
    if f:
        filas_wilson = f"""
        <tr><th>Muestra (n)</th><td>{f.get('n', '—')}</td></tr>
        <tr><th>Tasa cruda</th><td>{f.get('tasa_cruda_pct', '—')}%</td></tr>
        <tr><th>Efectividad Wilson LCB 95%</th><td class="rojo">{f.get('wilson_lcb_pct', '—')}%</td></tr>"""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_CSS_MARCA}</style></head>
<body>
  <div class="header">
    <h1>Solicitud de remediación operativa</h1>
    <p>WebCorp Autopilot · dirigida a: {datos['carrier']} · {datos['fecha_generacion']}</p>
  </div>
  <h2>Caso detectado</h2>
  <table>
    <tr><th style="width:40%">Título</th><td>{datos['asunto']}</td></tr>
    <tr><th>Zona</th><td>{c.get('municipio', '—')}, {c.get('depto', '—')} ({c.get('pais', 'GT')})</td></tr>
    <tr><th>Mensajería</th><td>{datos['carrier']}</td></tr>
    {filas_wilson}
    <tr><th>Snapshot de datos</th><td>{datos['snapshot']}</td></tr>
    <tr><th>Órdenes no entregadas detectadas</th><td>{datos['n_ordenes']}</td></tr>
  </table>
  <h2>Acción sugerida</h2>
  <div class="accion">{datos['accion']}</div>
  <div class="meta">
    Trazabilidad: {datos['origen_id']} · Metodología: ranking por límite inferior del intervalo
    de Wilson (95%), nunca por tasa cruda · Este documento fue generado automáticamente por
    WebCorp Autopilot <b>después de aprobación humana explícita</b>; los números provienen de
    herramientas determinísticas, no de un modelo de lenguaje.
  </div>
</body></html>"""


def _generar_pdf(datos: dict, base: str, outdir: str) -> dict:
    html = _html_pdf(datos)
    try:
        from weasyprint import HTML  # import perezoso: si falta pango, caemos a HTML
        ruta = os.path.join(outdir, base + ".pdf")
        HTML(string=html).write_pdf(ruta)
        return {"tipo": "pdf", "archivo": os.path.basename(ruta)}
    except Exception as e:
        ruta = os.path.join(outdir, base + ".html")
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write(html)
        return {"tipo": "html_imprimible", "archivo": os.path.basename(ruta),
                "nota": f"weasyprint no disponible ({type(e).__name__}); HTML listo para imprimir"}


def _generar_excel(datos: dict, base: str, outdir: str) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Ordenes no entregadas"
    c = datos["caso"]
    ws.append([f"WebCorp Autopilot — {datos['asunto']}"])
    ws.append([f"Zona: {c.get('municipio','—')}, {c.get('depto','—')} · Carrier: {datos['carrier']}"
               f" · Snapshot: {datos['snapshot']} · Trazabilidad: {datos['origen_id']}"])
    ws.append([])
    encabezado = ["Orden", "Teléfono", "Municipio", "Depto", "Carrier",
                  "Estatus", "Subestatus", "COD", "Fecha"]
    ws.append(encabezado)
    fila_h = ws.max_row
    for col in range(1, len(encabezado) + 1):
        cell = ws.cell(row=fila_h, column=col)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2563EB")
    for o in datos["ordenes"]:
        ws.append([o.get("orden"), o.get("telefono"), o.get("municipio"), o.get("depto"),
                   o.get("carrier"), o.get("estatus"), o.get("subestatus"),
                   o.get("cod"), o.get("fecha")])
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 13
    for col in "CDEFG":
        ws.column_dimensions[col].width = 20
    ws.cell(row=1, column=1).font = Font(bold=True, size=13, color="1D4ED8")

    ruta = os.path.join(outdir, base + ".xlsx")
    wb.save(ruta)
    return {"tipo": "excel", "archivo": os.path.basename(ruta),
            "ordenes_incluidas": len(datos["ordenes"])}


def _borrador_correo(datos: dict, area: str, base: str, outdir: str) -> dict:
    """area: 'mensajeria' | 'callcenter' — las dos unicas soportadas."""
    c, f = datos["caso"], datos.get("wilson") or {}
    if area == "callcenter":
        para = EMAIL_CALLCENTER
        asunto = f"[Autopilot] Gestión call center — {datos['n_ordenes']} órdenes no entregadas · {c.get('municipio','')} × {datos['carrier']}"
        extra = (f"Se adjunta el Excel con {datos['n_ordenes']} órdenes no entregadas "
                 f"(orden, teléfono, estatus, COD) para gestión de recuperación.")
    else:
        para = EMAIL_CARRIER_FMT.format(carrier=_slug(datos["carrier"]) or "carrier")
        asunto = f"[Autopilot] Remediación requerida — {datos['carrier']} en {c.get('municipio','')}, {c.get('depto','')}"
        extra = "Se adjunta el PDF con el detalle del caso y la metodología."

    wilson_txt = (f"n={f.get('n','—')} · Wilson LCB 95%: {f.get('wilson_lcb_pct','—')}% "
                  f"(cruda {f.get('tasa_cruda_pct','—')}%)") if f else "sin fila en el estimador"
    cuerpo = f"""Estimado equipo,

El sistema de monitoreo de WebCorp detectó y un operador humano aprobó escalar el siguiente caso:

{datos['asunto']}

Datos del caso (snapshot {datos['snapshot']}):
- Zona: {c.get('municipio','—')}, {c.get('depto','—')} ({c.get('pais','GT')})
- Mensajería: {datos['carrier']}
- Evidencia: {wilson_txt}
- Órdenes no entregadas detectadas: {datos['n_ordenes']}

Acción sugerida: {datos['accion']}

{extra}

Trazabilidad: {datos['origen_id']}
--
Borrador generado por WebCorp Autopilot tras aprobación humana.
Este correo NO se envía automáticamente: requiere revisión y envío manual.
"""
    ruta = os.path.join(outdir, f"{base}_correo_{area}.txt")
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(f"Para: {para}\nAsunto: {asunto}\n\n{cuerpo}")
    mailto = f"mailto:{para}?subject={urllib.parse.quote(asunto)}&body={urllib.parse.quote(cuerpo)}"
    return {"tipo": "correo_borrador", "area": area, "para": para,
            "archivo": os.path.basename(ruta), "mailto": mailto}


# ------------------------------------------------------------------
# Punto de entrada — lo llama notificar_operaciones DESPUES de la
# aprobacion humana. Recibe todo ya calculado; aqui solo se renderiza.
# ------------------------------------------------------------------
def generar(asunto: str, accion: str, origen_id: str, caso: dict,
            wilson: dict = None, snapshot: str = "desconocido",
            ordenes: list = None) -> dict:
    caso = caso or {}
    carrier = (caso.get("carrier") or "").upper()
    if not (caso.get("municipio") and carrier):
        return {"generado": False,
                "razon": "sin caso estructurado (municipio+carrier); no aplica artefacto"}

    ordenes = ordenes or []
    outdir = _dir_artefactos()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"remediacion_{_slug(caso['municipio'])}_{_slug(carrier)}_{ts}"
    datos = {
        "asunto": asunto, "accion": accion or "Revisión operativa del par municipio-carrier.",
        "origen_id": origen_id, "caso": caso, "carrier": carrier, "wilson": wilson,
        "snapshot": snapshot, "ordenes": ordenes, "n_ordenes": len(ordenes),
        "fecha_generacion": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    artefactos = []
    # Regla 1 (codigo, no LLM): caso puntual -> PDF dirigido a la mensajeria.
    artefactos.append(_generar_pdf(datos, base, outdir))
    correos = [_borrador_correo(datos, "mensajeria", base, outdir)]
    # Regla 2: hay clientes afectados -> Excel para call center.
    if ordenes:
        artefactos.append(_generar_excel(datos, base, outdir))
        correos.append(_borrador_correo(datos, "callcenter", base, outdir))

    return {
        "generado": True,
        "artefactos": artefactos,
        "correos": correos,
        "nota": ("Artefactos deterministas generados tras aprobación humana. "
                 "Los correos son BORRADORES: nunca se envían automáticamente."),
    }
