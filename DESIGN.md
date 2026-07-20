# Design

Sistema visual del panel WebCorp Autopilot (`frontend/index.html`, single-file HTML/CSS/JS servido por FastAPI). Hereda los tokens del dashboard corporativo WebCorp (`webcorp-dashboard/src/App.jsx`, objeto `C`).

## Theme

Claro. Fondo `#F8FAFC` con mesh sutil de radiales azul/teal (chroma bajísimo). Un solo tema; sin modo oscuro (anti-referencia explícita: terminal hacker).

## Color

Tokens (CSS custom properties en `:root`):

| Rol | Token | Valor |
|---|---|---|
| Primario (marca, acciones) | `--primary` / `--primary700` / `--primary300` / `--primary50` | `#2563EB` / `#1D4ED8` / `#93C5FD` / `#EFF6FF` |
| Acento datos/actividad | `--teal` / `--teal600` / `--teal50` | `#14B8A6` / `#0D9488` / `#F0FDFA` |
| Fondos | `--bg` / `--bg-sec` / `--surface` | `#F8FAFC` / `#F1F5F9` / `#FFFFFF` |
| Éxito | `--success` / `--success-l` / `--success-d` | `#22C55E` / `#DCFCE7` / `#15803D` |
| Advertencia (HITL) | `--warning` / `--warning-l` / `--warning-d` | `#F59E0B` / `#FEF3C7` / `#B45309` |
| Error | `--error` / `--error-l` / `--error-d` | `#EF4444` / `#FEE2E2` / `#B91C1C` |
| Tinta (texto) | `--ink` / `--ink-sec` / `--ink-ter` / `--ink-dis` | `#0F172A` / `#334155` / `#64748B` / `#94A3B8` |
| Bordes | `--border` / `--border-strong` / `--border-subtle` | `#E2E8F0` / `#CBD5E1` / `#F1F5F9` |

Estrategia: **restrained** — neutrales + azul primario, con roles semánticos fijos: ámbar = requiere decisión humana (HITL), teal = actividad/datos calculados, verde = ejecutado, rojo = crítico. El semáforo del mapa (`<60` rojo, `60–70` ámbar, `>70` verde, gris sin datos) es contrato de significado, no decoración.

## Typography

| Rol | Familia | Uso |
|---|---|---|
| Display | **Bricolage Grotesque** (`--display`) | Títulos de página, wordmark, headers de tarjeta, nav, asuntos de pendientes |
| Cuerpo | **Instrument Sans** (`--sans`) | Todo el texto de UI y respuestas |
| Datos | **IBM Plex Mono** (`--mono`) | Todo número calculado, KPIs, IDs, timestamps, eventos técnicos, marca `ƒ calculado` |

Jerarquía de 3 niveles (pantalla Agente): **hero** = respuesta del agente (15.5px/1.7, ink pleno); **body** = UI normal (12–14px); **caption** = audit/técnico (9–11.5px, grises apagados). Regla dura: si un número salió de una tool, va en mono con la marca `ƒ calculado`.

## Components

- **Sidebar** 230px (clon del dashboard): logo, nav con item activo en gradiente primary, botón de escaneo, estado de conexión con dot.
- **Cards** blancas, radio 16px, borde 1px `--border`, sombra suave. Variantes con identidad: `conversacion` (tag azul), `actividad` (borde izq. teal 4px + dot "en vivo").
- **Pendiente HITL**: tarjeta ámbar degradada — el único bloque de advertencia con peso completo. Botones: aprobar (verde sólido), descartar (outline).
- **Chips**: sugerencias (bg-sec, sin borde), tools (`fn nombre`, teal50, mono), couriers del mapa (pill toggle primary).
- **Audit log**: dot de color por tipo de evento + título humano (12.5px/600) + detalle caption + subtítulo técnico mono 9px.
- **Mapa SVG** dos niveles (Centroamérica → deptos GT) con tooltip fijo oscuro y leyenda semáforo.
- **Toast** oscuro centrado abajo; variante error.

## Layout & Motion

- Grid 2 columnas `7fr/5fr` (colapsa a 1 en <1060px); gaps 22–28px; `card-body` 20–24px.
- Animaciones: reveal (fade+translateY, cubic-bezier suave) con stagger corto en KPIs y chips de tools; pulse en dots de estado; spinner en cargas. `@media (prefers-reduced-motion: reduce)` desactiva todas globalmente (el contenido nunca depende de la animación para ser visible).
- Tokens añadidos post-audit: `--teal800 #115E59` (texto sobre teal50, 7.27:1), `--brd-teal`/`--brd-warn` (bordes), escala z semántica `--z-toast:60 / --z-tooltip:70`. El tier caption usa `--ink-ter` (4.76:1), nunca `--ink-dis` (reservado para elementos no textuales).

## Voice

Español directo. Etiquetas humanas primero, término técnico como subtítulo. Sin emojis (decisión explícita). Los avisos declaran ("datos sintéticos de demostración"), no alarman.
