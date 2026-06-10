"""
Monitor paralelo de estado y observaciones de actividades TOA.

Objetivo:
- Tomar desde SharePoint las citas ya creadas en TOA.
- Buscar la actividad por el ID TOA / AID en la grilla de Oracle Field Service.
- Buscar primero la actividad leyendo la grilla TOA por Orden/Tipo/Estado, sin abrir cada fila.
- Validar Orden de trabajo y Tipo de actividad contra SharePoint antes de leer el estado.
- Verificar el AID/ID_TOA de la casilla encontrada para no actualizar un registro incorrecto.
- Extraer el estado visible de la actividad: pendiente, iniciada, finalizada, etc.
- Intentar extraer la observación de la actividad ya creada.
- Actualizar SharePoint en las columnas:
    Estado_actividad
    Observacion_actividad

Variables .env esperadas:
- TOA_URL o TOA_BASE_URL
- TOA_USER
- TOA_PASSWORD
- POWER_AUTOMATE_MONITOR_URL              -> flujo HTTP que devuelve registros a monitorear
- POWER_AUTOMATE_UPDATE_ACTIVIDAD_URL    -> flujo HTTP que actualiza Estado_actividad y Observacion_actividad
- MONITOR_INTERVAL_SECONDS               -> intervalo del bucle. Default: 120
- MONITOR_HEADLESS                       -> true/false. Default: true
- MONITOR_MAX_SCROLLS                    -> intentos de scroll en la grilla. Default: 30
- MONITOR_VISTA_LISTA_AL_CAMBIAR_FECHA    -> selecciona Vista de lista al cambiar fecha. Default: true
- MONITOR_VISTA_LISTA_SIEMPRE             -> fuerza Vista de lista aunque no cambie fecha. Default: false
- MONITOR_APLICAR_VER_SOLO_UNA_VEZ        -> aplica Ver + Aplicar jerárquicamente solo una vez por ejecución. Default: true
- MONITOR_CONFIAR_EN_AID_SI_COINCIDE       -> si el AID coincide con ID_TOA, acepta la cita aunque el popup venga con etiquetas desordenadas. Default: true
- MONITOR_SOLO_ABRIR_CITA                  -> busca la cita por AID/ID_TOA y detiene el proceso sin extraer ni actualizar SharePoint. Default: false
- MONITOR_INGRESAR_A_DETALLE_CITA           -> después de ubicar la barra, intenta entrar al detalle de la actividad. Default: true
- MONITOR_EXTRAER_ESTADO_DESDE_DETALLE      -> al ingresar al detalle, lee data-label="astatus" y actualiza Estado_actividad. Default: true
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

# Helpers propios del repositorio de monitoreo.
from toa_monitor_helpers import (  # noqa: E402
    STATE_FILE,
    TOA_URL,
    abrir_pagina_inicial,
    cargar_pids_desde_power_automate,
    filtrar_providerid_por_ruta,
    iniciar_sesion_toa,
    normalizar_texto,
    obtener_inputexcel,
    reingresar_password_toa_si_aparece,
    convertir_fecha_chile,
    seleccionar_zona_y_ruta_toa,
)
from power_automate_utils import extraer_registros, limpiar_valor, obtener_valor  # noqa: E402

load_dotenv()

POWER_AUTOMATE_MONITOR_URL = (
    os.getenv("POWER_AUTOMATE_MONITOR_URL")
    or os.getenv("POWER_AUTOMATE_URL")
    or os.getenv("PA_WEBHOOK_URL")
)
POWER_AUTOMATE_UPDATE_ACTIVIDAD_URL = (
    os.getenv("POWER_AUTOMATE_UPDATE_ACTIVIDAD_URL")
    or os.getenv("POWER_AUTOMATE_UPDATE_URL")
)
DATASET_BUSQUEDA = os.getenv("DATASET_BUSQUEDA", "")
NAME_RPA = os.getenv("NameRPA", "rpa-toa-monitor")

MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "120"))
MONITOR_HEADLESS = os.getenv("MONITOR_HEADLESS", "true").strip().lower() not in ["false", "0", "no"]
MONITOR_MAX_SCROLLS = int(os.getenv("MONITOR_MAX_SCROLLS", "30"))
MONITOR_VALIDAR_TIPO = os.getenv("MONITOR_VALIDAR_TIPO", "true").strip().lower() not in ["false", "0", "no"]
MONITOR_MAX_ACTIVIDADES_VISIBLES = int(os.getenv("MONITOR_MAX_ACTIVIDADES_VISIBLES", "80"))
MONITOR_LOG_FILAS_GRILLA = os.getenv("MONITOR_LOG_FILAS_GRILLA", "false").strip().lower() in ["true", "1", "yes", "si", "sí"]
MONITOR_ACTUALIZAR_DESDE_GRILLA = os.getenv("MONITOR_ACTUALIZAR_DESDE_GRILLA", "false").strip().lower() in ["true", "1", "yes", "si", "sí"]
# Si la validación Orden/Tipo no encuentra coincidencia, igual intenta ingresar por ID_TOA/AID.
MONITOR_INGRESAR_POR_AID_SI_FALLA_VALIDACION = os.getenv("MONITOR_INGRESAR_POR_AID_SI_FALLA_VALIDACION", "false").strip().lower() not in ["false", "0", "no"]
# Scroll más fino para no saltarse filas virtualizadas del Oracle DataGrid.
MONITOR_SCROLL_GRID_DELTA = int(os.getenv("MONITOR_SCROLL_GRID_DELTA", "250") or "250")
MONITOR_VALIDAR_ID_TOA = os.getenv("MONITOR_VALIDAR_ID_TOA", "true").strip().lower() not in ["false", "0", "no"]
# Al finalizar cada actividad, vuelve a la fecha actual antes de continuar con la siguiente.
MONITOR_VOLVER_FECHA_ACTUAL_AL_FINAL = os.getenv("MONITOR_VOLVER_FECHA_ACTUAL_AL_FINAL", "true").strip().lower() not in ["false", "0", "no"]
# Reabrir TOA entre actividades es más lento y puede ocultar el movimiento de fecha; se deja desactivado por defecto.
MONITOR_REABRIR_TOA_CADA_ACTIVIDAD = os.getenv("MONITOR_REABRIR_TOA_CADA_ACTIVIDAD", "false").strip().lower() in ["true", "1", "yes", "si", "sí"]
MONITOR_APLICAR_VER_AL_CAMBIAR_FECHA = os.getenv("MONITOR_APLICAR_VER_AL_CAMBIAR_FECHA", "true").strip().lower() not in ["false", "0", "no"]
MONITOR_APLICAR_VER_SIEMPRE = os.getenv("MONITOR_APLICAR_VER_SIEMPRE", "false").strip().lower() in ["true", "1", "yes", "si", "sí"]
# Evita abrir Ver -> Aplicar jerárquicamente -> Aplicar en cada actividad.
# En TOA esa opción puede comportarse como interruptor si se presiona repetidamente.
MONITOR_APLICAR_VER_SOLO_UNA_VEZ = os.getenv("MONITOR_APLICAR_VER_SOLO_UNA_VEZ", "true").strip().lower() not in ["false", "0", "no"]
MONITOR_VISTA_LISTA_AL_CAMBIAR_FECHA = os.getenv("MONITOR_VISTA_LISTA_AL_CAMBIAR_FECHA", "true").strip().lower() not in ["false", "0", "no"]
MONITOR_VISTA_LISTA_SIEMPRE = os.getenv("MONITOR_VISTA_LISTA_SIEMPRE", "false").strip().lower() in ["true", "1", "yes", "si", "sí"]
MONITOR_CONFIAR_EN_AID_SI_COINCIDE = os.getenv("MONITOR_CONFIAR_EN_AID_SI_COINCIDE", "true").strip().lower() not in ["false", "0", "no"]
# En esta versión el objetivo es ingresar al detalle, extraer Estado de actividad y actualizar SharePoint.
MONITOR_SOLO_ABRIR_CITA = os.getenv("MONITOR_SOLO_ABRIR_CITA", "false").strip().lower() not in ["false", "0", "no"]
MONITOR_INGRESAR_A_DETALLE_CITA = os.getenv("MONITOR_INGRESAR_A_DETALLE_CITA", "true").strip().lower() not in ["false", "0", "no"]
MONITOR_EXTRAER_ESTADO_DESDE_DETALLE = os.getenv("MONITOR_EXTRAER_ESTADO_DESDE_DETALLE", "true").strip().lower() not in ["false", "0", "no"]
# Si la cita no aparece en TOA por Orden + Tipo, se marca en SharePoint como ELIMINADO.
MONITOR_MARCAR_ELIMINADO_SI_NO_ENCUENTRA = os.getenv("MONITOR_MARCAR_ELIMINADO_SI_NO_ENCUENTRA", "true").strip().lower() not in ["false", "0", "no"]
MONITOR_ESTADO_SI_NO_ENCUENTRA = os.getenv("MONITOR_ESTADO_SI_NO_ENCUENTRA", "ELIMINADO").strip() or "ELIMINADO"
# Estados que ya no se deben tomar desde la lista SharePoint.
# Por defecto se omiten las actividades canceladas y finalizadas para no volver a procesarlas.
MONITOR_IGNORAR_ESTADOS_ACTIVIDAD = [
    normalizar_texto(estado).lower().strip()
    for estado in os.getenv("MONITOR_IGNORAR_ESTADOS_ACTIVIDAD", "cancelada,finalizada,eliminado").split(",")
    if estado.strip()
]

# Estados visibles desde el HTML entregado y equivalencias desde atributos internos TOA.
MAPA_ESTADOS_TOA = {
    "pending": "pendiente",
    "started": "iniciada",
    "complete": "finalizada",
    "completed": "finalizada",
    "cancelled": "cancelada",
    "canceled": "cancelada",
    "suspended": "suspendida",
    "notdone": "no realizada",
    "not_done": "no realizada",
}

ESTADOS_VALIDOS_CANONICOS = {
    "pendiente",
    "iniciada",
    "iniciado",
    "finalizada",
    "finalizado",
    "cancelada",
    "cancelado",
    "suspendida",
    "suspendido",
    "no realizada",
    "no realizado",
    "eliminado",
}

VALORES_QUE_NO_SON_ESTADO = {
    "categoria del cliente",
    "nombre",
    "numero de cuenta",
    "número de cuenta",
    "comuna",
    "zona de trabajo",
    "telefono",
    "teléfono",
    "id de ticket",
    "tipo de actividad",
    "intervalo de tiempo",
    "orden de trabajo",
    "estado de actividad",
    "inicio de sla",
    "inicio – fin",
    "inicio - fin",
    "finalizacion",
    "finalización",
    "duracion",
    "duración",
}

POSIBLES_CAMPOS_ID_TOA = [
    "ID_TOA",
    "IdActividadTOA",
    "Id Actividad TOA",
    "ID Actividad TOA",
    "ActividadTOA",
    "aid",
]

POSIBLES_CAMPOS_ESTADO_CREACION = [
    "Estado_Creacion",
    "Estado Creacion",
    "Estado Creación",
    "EstadoCreacion",
]

POSIBLES_CAMPOS_ORDEN_VENTA = [
    "OrdenVenta",
    "Orden Venta",
    "Orden de venta",
    "Orden_de_venta",
    "Orden_x0020_Venta",
    "Orden_x0020_de_x0020_venta",
    "OrdenVentas",
    "OrdenesVenta",
    "Ordenes de ventas",
    "Órdenes de ventas",
    "NroOrdenVenta",
    "Nro Orden Venta",
    "OV",
    "Orden",
]

POSIBLES_CAMPOS_ORDEN_TRABAJO = [
    "OrdenTrabajo",
    "Orden Trabajo",
    "Orden de trabajo",
    "Orden_de_trabajo",
    "Orden_x0020_Trabajo",
    "Orden_x0020_de_x0020_trabajo",
    "OrdenCliente",
    "Orden Cliente",
    "Orden del cliente",
]

POSIBLES_CAMPOS_TIPO_ACTIVIDAD = [
    "TipoActividad",
    "Tipo Actividad",
    "Tipo de actividad",
    "TipoServicio",
    "Tipo Servicio",
    "Tipo_x0020_Servicio",
    "Tipo_x0020_Actividad",
    "Tipo_x0020_de_x0020_actividad",
]



# Estado en memoria de la ejecución actual.
# Sirve para no volver a presionar Ver -> Aplicar jerárquicamente -> Aplicar en cada registro.
VISTA_JERARQUICA_APLICADA_EN_ESTA_EJECUCION = False

def log(mensaje: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {mensaje}")


def normalizar_estado(valor: Any) -> Optional[str]:
    if valor in [None, ""]:
        return None

    texto = str(limpiar_valor(valor)).strip()
    if not texto:
        return None

    texto_limpio = re.sub(r"\s+", " ", texto).strip()
    texto_norm = normalizar_texto(texto_limpio).lower().strip()
    clave = texto_norm.replace(" ", "_")

    if texto_norm in VALORES_QUE_NO_SON_ESTADO:
        return None

    estado = MAPA_ESTADOS_TOA.get(clave, texto_norm)
    if estado in ESTADOS_VALIDOS_CANONICOS:
        # Se estandariza a femenino porque la columna es Estado_actividad.
        if estado == "iniciado":
            return "iniciada"
        if estado == "finalizado":
            return "finalizada"
        if estado == "cancelado":
            return "cancelada"
        if estado == "suspendido":
            return "suspendida"
        if estado == "no realizado":
            return "no realizada"
        return estado

    # Si el texto no es un estado real, no lo usamos. Esto evita guardar valores
    # erróneos como "Tipo de actividad" cuando el popup viene ordenado por columnas.
    return None


def solo_digitos(valor: Any) -> Optional[str]:
    if valor in [None, ""]:
        return None

    texto = str(limpiar_valor(valor)).strip()
    encontrado = re.search(r"\d+", texto)
    return encontrado.group(0) if encontrado else None


def todos_los_digitos(valor: Any) -> str:
    """Devuelve todos los dígitos de un valor. Útil para comparar Orden del cliente #000..."""
    if valor in [None, ""]:
        return ""
    return "".join(re.findall(r"\d+", str(limpiar_valor(valor))))


def normalizar_digitos_orden(valor: Any) -> str:
    digitos = todos_los_digitos(valor)
    return digitos.lstrip("0") or digitos

def normalizar_para_match(valor: Any) -> str:
    """Normaliza textos para comparar datos de SharePoint contra el resumen de TOA."""
    if valor in [None, ""]:
        return ""

    texto = str(limpiar_valor(valor)).strip()
    if not texto:
        return ""

    texto = normalizar_texto(texto).upper()
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def normalizar_orden_trabajo(valor: Any) -> str:
    """Deja la orden comparable, tolerando espacios, tildes y textos extra."""
    texto = normalizar_para_match(valor)
    if not texto:
        return ""

    # TOA suele mostrar valores como "Orden del cliente#0001341563".
    # Se conserva # y guiones, pero se quitan espacios y puntuación secundaria.
    texto = texto.replace("ORDEN DE TRABAJO", "")
    texto = texto.replace("ORDEN DEL CLIENTE", "ORDENDELCLIENTE")
    texto = re.sub(r"[^A-Z0-9#\-]", "", texto)
    return texto


def ordenes_coinciden(orden_sp: Any, orden_toa: Any) -> bool:
    """Compara ordenes tolerando textos como 'Orden del cliente #0005121853'."""
    a = normalizar_orden_trabajo(orden_sp)
    b = normalizar_orden_trabajo(orden_toa)

    if a and b and (a == b or a in b or b in a):
        return True

    da = normalizar_digitos_orden(orden_sp)
    db = normalizar_digitos_orden(orden_toa)
    if da and db and (da == db or da in db or db in da):
        return True

    return False


def tipos_coinciden(tipo_sp: Any, tipo_toa: Any) -> bool:
    a = normalizar_para_match(tipo_sp)
    b = normalizar_para_match(tipo_toa)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def parsear_valor_por_label(texto: Optional[str], labels: Iterable[str]) -> Optional[str]:
    """Extrae el valor visible que aparece al lado o debajo de una etiqueta del popup TOA."""
    if not texto:
        return None

    lineas = [linea.strip() for linea in str(texto).splitlines() if linea and linea.strip()]
    if not lineas:
        return None

    labels_norm = [(label, normalizar_para_match(label).rstrip(":")) for label in labels]

    for idx, linea in enumerate(lineas):
        linea_norm = normalizar_para_match(linea).rstrip(":")

        for label_original, label_norm in labels_norm:
            if not label_norm:
                continue

            # Caso 1: etiqueta en una línea y valor en la línea siguiente.
            if linea_norm == label_norm:
                for siguiente in lineas[idx + 1 : idx + 4]:
                    sig_norm = normalizar_para_match(siguiente).rstrip(":")
                    if sig_norm and sig_norm not in [l[1] for l in labels_norm]:
                        return siguiente.strip()

            # Caso 2: etiqueta y valor en la misma línea.
            if linea_norm.startswith(label_norm) and len(linea_norm) > len(label_norm):
                valor = linea[len(label_original) :].strip(" :-\t")
                if valor:
                    return valor

    return None



def llamar_power_automate_monitor() -> Any:
    if not POWER_AUTOMATE_MONITOR_URL:
        raise ValueError("Falta POWER_AUTOMATE_MONITOR_URL, POWER_AUTOMATE_URL o PA_WEBHOOK_URL en el archivo .env")

    payload = {
        "dataset": str(DATASET_BUSQUEDA),
        "NameRPA": str(NAME_RPA),
        "modo": "monitor_estado_actividad",
    }

    log("📥 Solicitando actividades a monitorear desde Power Automate...")
    response = requests.post(
        POWER_AUTOMATE_MONITOR_URL,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )

    log(f"📥 Status monitor: {response.status_code}")
    if response.status_code >= 400:
        raise RuntimeError(f"Power Automate monitor falló: {response.status_code} - {response.text}")

    try:
        return response.json()
    except ValueError:
        raise ValueError("Power Automate monitor respondió, pero no devolvió JSON válido.")


def normalizar_registro_monitor(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normaliza un registro recibido desde Power Automate/SharePoint.

    SharePoint suele devolver columnas Choice como objetos, por ejemplo:
    {"@odata.type": "...SPListExpandedReference", "Id": 0, "Value": "Creada"}

    Por eso todos los campos de texto pasan por limpiar_valor() antes de comparar,
    para que Estado_Creacion quede como "Creada" y no como el diccionario completo.
    """
    id_sp = solo_digitos(obtener_valor(item, "ID", "Id", "id"))
    id_toa = solo_digitos(obtener_valor(item, *POSIBLES_CAMPOS_ID_TOA))

    estado_creacion = limpiar_valor(obtener_valor(item, *POSIBLES_CAMPOS_ESTADO_CREACION))
    estado_actividad_actual = limpiar_valor(obtener_valor(item, "Estado_actividad", "Estado actividad"))
    observacion_actividad_actual = limpiar_valor(obtener_valor(item, "Observacion_actividad", "Observación actividad"))
    ruta_toa = limpiar_valor(obtener_valor(
        item,
        "RutaTOACompleta",
        "Ruta TOA Completa",
        "RutaCompleta",
        "Ruta TOA",
        "ZonaTOA",
    ))
    inicio_sla = limpiar_valor(obtener_valor(
        item,
        "InicioSLA",
        "Inicio SLA",
        "Inicio de SLA",
        "Inicio_x0020_SLA",
    ))
    orden_venta = limpiar_valor(obtener_valor(item, *POSIBLES_CAMPOS_ORDEN_VENTA))
    orden_trabajo = limpiar_valor(obtener_valor(item, *POSIBLES_CAMPOS_ORDEN_TRABAJO))
    tipo_actividad = limpiar_valor(obtener_valor(item, *POSIBLES_CAMPOS_TIPO_ACTIVIDAD))

    # La grilla TOA muestra la orden en data-field="appt_number".
    # En algunos casos ese valor viene como "Orden del cliente #..." y en otros como número de OV.
    # Por eso se prioriza OrdenVenta si existe, y se usa OrdenTrabajo/OrdenCliente como respaldo.
    orden_validacion = orden_venta or orden_trabajo

    return {
        "ID": int(id_sp) if id_sp else None,
        "ID_TOA": id_toa,
        "Estado_Creacion": estado_creacion,
        "Estado_actividad_actual": estado_actividad_actual,
        "Observacion_actividad_actual": observacion_actividad_actual,
        "RutaTOACompleta": ruta_toa,
        "InicioSLA": inicio_sla,
        "OrdenVenta": orden_venta,
        "OrdenTrabajoOriginal": orden_trabajo,
        "OrdenTrabajo": orden_validacion,
        "TipoActividad": tipo_actividad,
    }


def obtener_actividades_a_monitorear() -> List[Dict[str, Any]]:
    respuesta = llamar_power_automate_monitor()
    registros = extraer_registros(respuesta)

    if not registros:
        log("⚠️ Power Automate respondió 200, pero no se encontraron registros parseables. Revisa que el flujo responda con el Body del paso Seleccionar o con una lista JSON.")

    actividades: List[Dict[str, Any]] = []
    omitidos_sin_id = 0

    for registro in registros:
        actividad = normalizar_registro_monitor(registro)

        if not actividad.get("ID") or not actividad.get("ID_TOA"):
            omitidos_sin_id += 1
            continue

        estado_creacion = normalizar_texto(actividad.get("Estado_Creacion") or "")
        estado_actividad_actual = normalizar_estado(actividad.get("Estado_actividad_actual"))

        # Se monitorean solo registros creados. Si el flujo ya viene filtrado, igual pasa.
        if estado_creacion and estado_creacion not in ["CREADA", "CREADO"]:
            log(
                f"⏭️ Se omite SP ID {actividad.get('ID')} / TOA {actividad.get('ID_TOA')} "
                f"porque Estado_Creacion='{actividad.get('Estado_Creacion')}'"
            )
            continue

        # No se toman actividades que ya estén canceladas o finalizadas en SharePoint.
        # Se omiten de forma silenciosa para no ensuciar el log.
        if estado_actividad_actual and estado_actividad_actual in MONITOR_IGNORAR_ESTADOS_ACTIVIDAD:
            continue

        actividades.append(actividad)

    if omitidos_sin_id and not actividades:
        log(f"⚠️ Se recibieron {omitidos_sin_id} registros, pero ninguno trae ID de SharePoint e ID_TOA válidos. Revisa el paso Seleccionar del flujo monitor.")

    log(f"✅ Actividades a monitorear: {len(actividades)}")
    return actividades


def actualizar_sharepoint_estado_actividad(
    id_sharepoint: int,
    id_toa: str,
    estado_actividad: Optional[str],
    observacion_actividad: Optional[str],
) -> bool:
    if not POWER_AUTOMATE_UPDATE_ACTIVIDAD_URL:
        raise ValueError("Falta POWER_AUTOMATE_UPDATE_ACTIVIDAD_URL o POWER_AUTOMATE_UPDATE_URL en el archivo .env")

    payload = {
        "ID": int(id_sharepoint),
        "ID_TOA": str(id_toa),
        "IdActividadTOA": str(id_toa),
        "Estado_actividad": estado_actividad or "",
        "Observacion_actividad": observacion_actividad or "",
    }

    log("📤 Actualizando SharePoint con estado/observación:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    response = requests.post(
        POWER_AUTOMATE_UPDATE_ACTIVIDAD_URL,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )

    log(f"📤 Status update actividad: {response.status_code}")
    if response.text:
        log(f"📤 Respuesta update actividad: {response.text[:500]}")

    return response.status_code in [200, 201, 202]




def marcar_sharepoint_como_eliminado(actividad: Dict[str, Any], motivo: str) -> bool:
    """Marca en SharePoint la actividad como ELIMINADO cuando no se encuentra en TOA."""
    if not MONITOR_MARCAR_ELIMINADO_SI_NO_ENCUENTRA:
        log(f"ℹ️ Cita no encontrada, pero MONITOR_MARCAR_ELIMINADO_SI_NO_ENCUENTRA=false. Motivo: {motivo}")
        return False

    id_sp = actividad.get("ID")
    id_toa = actividad.get("ID_TOA")
    if not id_sp or not id_toa:
        log("⚠️ No se puede marcar ELIMINADO porque falta ID SharePoint o ID_TOA.")
        return False

    estado_actual = normalizar_texto(actividad.get("Estado_actividad_actual") or "").lower().strip()
    estado_eliminado_norm = normalizar_texto(MONITOR_ESTADO_SI_NO_ENCUENTRA).lower().strip()
    if estado_actual == estado_eliminado_norm:
        return True

    log(
        f"🗑️ Cita no encontrada en TOA. Se actualizará SharePoint como {MONITOR_ESTADO_SI_NO_ENCUENTRA}. "
        f"SP ID={id_sp} | TOA ID={id_toa} | Motivo: {motivo}"
    )
    return actualizar_sharepoint_estado_actividad(
        id_sharepoint=int(id_sp),
        id_toa=str(id_toa),
        estado_actividad=MONITOR_ESTADO_SI_NO_ENCUENTRA,
        observacion_actividad=actividad.get("Observacion_actividad_actual"),
    )


def leer_texto_js(page, script: str, *args: Any) -> Optional[str]:
    try:
        valor = page.evaluate(script, *args)
    except Exception:
        return None

    if valor in [None, ""]:
        return None

    texto = str(valor).strip()
    return texto if texto else None


def extraer_estado_desde_dom_por_aid(page, id_toa: str) -> Optional[str]:
    """
    Busca el AID en el DOM. En el HTML entregado se observa que:
    - La barra de actividad trae aid="669641" o data-id="a_669641".
    - La columna de estado está en data-field="astatus".
    - Ambas comparten data-row-index en el ojDataGrid.
    """

    script = """
    (aid) => {
      const aidText = String(aid).trim();
      const selectors = [
        `[aid="${aidText}"]`,
        `[data-id="a_${aidText}"]`,
        `.toaGantt-tb[aid="${aidText}"]`,
        `.toaGantt-tb[data-id="a_${aidText}"]`
      ];

      let activity = null;
      for (const selector of selectors) {
        activity = document.querySelector(selector);
        if (activity) break;
      }

      if (!activity) return null;

      const attrStatus = activity.getAttribute('data-activity-status');
      const cell = activity.closest('[data-row-index]');
      const rowIndex = cell ? cell.getAttribute('data-row-index') : null;

      let textStatus = null;
      if (rowIndex !== null) {
        const statusCell = document.querySelector(`[data-row-index="${rowIndex}"][data-field="astatus"]`);
        if (statusCell) textStatus = (statusCell.innerText || statusCell.textContent || '').trim();
      }

      return {
        textStatus,
        attrStatus,
        rowIndex
      };
    }
    """

    try:
        resultado = page.evaluate(script, str(id_toa))
    except Exception:
        return None

    if not resultado:
        return None

    estado = resultado.get("textStatus") or resultado.get("attrStatus")
    return normalizar_estado(estado)


def _scroll_contenedores_grid(page, delta: int) -> bool:
    """Hace scroll sobre los contenedores reales de Oracle JET DataGrid."""
    script = r"""
    (delta) => {
      const esScrollable = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const oy = style.overflowY || '';
        return (oy.includes('auto') || oy.includes('scroll')) && el.scrollHeight > el.clientHeight + 20;
      };
      const preferidos = Array.from(document.querySelectorAll(
        '.oj-datagrid-scroller, .oj-datagrid-scroller-touch, .oj-datagrid, [data-oj-container="ojDataGrid"]'
      ));
      const todos = Array.from(document.querySelectorAll('*')).filter(esScrollable);
      const candidatos = [...preferidos, ...todos].filter((el, idx, arr) => arr.indexOf(el) === idx && esScrollable(el));
      candidatos.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
      let movido = false;
      for (const el of candidatos.slice(0, 8)) {
        const antes = el.scrollTop;
        el.scrollTop = Math.max(0, Math.min(el.scrollTop + delta, el.scrollHeight));
        if (el.scrollTop !== antes) movido = true;
      }
      return movido;
    }
    """
    try:
        return bool(page.evaluate(script, int(delta)))
    except Exception:
        return False


def resetear_grilla_al_inicio(page) -> None:
    """Vuelve la grilla al inicio antes de buscar, para no partir a media lista."""
    script = r"""
    () => {
      const esScrollable = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const oy = style.overflowY || '';
        return (oy.includes('auto') || oy.includes('scroll')) && el.scrollHeight > el.clientHeight + 20;
      };
      Array.from(document.querySelectorAll('*')).filter(esScrollable).forEach(el => { el.scrollTop = 0; });
      window.scrollTo(0, 0);
      return true;
    }
    """
    try:
        page.evaluate(script)
        page.wait_for_timeout(250)
    except Exception:
        pass


def scroll_grilla(page) -> None:
    """Mueve la grilla para que Oracle JET renderice más filas virtualizadas."""
    movido = _scroll_contenedores_grid(page, MONITOR_SCROLL_GRID_DELTA)
    if not movido:
        try:
            page.mouse.wheel(0, MONITOR_SCROLL_GRID_DELTA)
        except Exception:
            pass
    page.wait_for_timeout(250)


def buscar_estado_actividad_en_grilla(page, id_toa: str, max_scrolls: int = MONITOR_MAX_SCROLLS) -> Optional[str]:
    log(f"🔎 Buscando estado de actividad TOA AID/ID: {id_toa}")

    # Primer intento sin scroll.
    estado = extraer_estado_desde_dom_por_aid(page, id_toa)
    if estado:
        log(f"✅ Estado encontrado sin scroll: {estado}")
        return estado

    for intento in range(max_scrolls):
        scroll_grilla(page)
        estado = extraer_estado_desde_dom_por_aid(page, id_toa)
        if estado:
            log(f"✅ Estado encontrado con scroll {intento + 1}: {estado}")
            return estado

    log(f"⚠️ No se encontró el estado en grilla para AID/ID {id_toa}")
    return None


def click_actividad_por_aid(page, id_toa: str) -> bool:
    selectores = [
        f'[aid="{id_toa}"]',
        f'[data-id="a_{id_toa}"]',
        f'.toaGantt-tb[aid="{id_toa}"]',
        f'.toaGantt-tb[data-id="a_{id_toa}"]',
    ]

    for selector in selectores:
        try:
            elemento = page.locator(selector).first
            elemento.wait_for(state="visible", timeout=2000)
            elemento.scroll_into_view_if_needed(timeout=2000)
            elemento.click(force=True)
            page.wait_for_timeout(1500)
            log(f"✅ Actividad abierta con selector: {selector}")
            return True
        except Exception:
            continue

    return False




def detalle_actividad_parece_abierto(page) -> bool:
    """
    Valida de forma flexible si TOA ya entró al detalle/formulario de la actividad.
    No se usa como única condición de éxito, porque Oracle OFSC puede cambiar el DOM
    según la vista, pero ayuda a dejar logs más claros.
    """
    indicadores = [
        'text="Detalles de actividad"',
        'text="Detalle de actividad"',
        '[data-label="astatus"]',
        '[aria-describedby][data-label="astatus"]',
        '[data-label="XA_obse_tecn"]',
        '[data-label*="obse" i]',
        'button:has-text("Iniciar")',
        'button:has-text("Finalizar")',
        'button:has-text("Completar")',
        'button:has-text("Editar")',
    ]
    for selector in indicadores:
        try:
            if page.locator(selector).first.count() > 0:
                return True
        except Exception:
            continue
    return False


def ingresar_detalle_desde_actividad(page, locator, id_toa: str) -> bool:
    """
    Después de ubicar la barra/casilla de la actividad, intenta ENTRAR al detalle.

    En TOA, el primer click normalmente solo abre el resumen flotante. Para ingresar
    realmente a la actividad, esta función prueba varias estrategias seguras:
    1. Doble click sobre la barra de actividad.
    2. Enter sobre la actividad enfocada.
    3. Doble click/click sobre el resumen flotante visible.
    4. Evento dblclick por JavaScript sobre el elemento aid/data-id.
    """
    try:
        locator.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass

    # Primer click: abre o enfoca la cita.
    try:
        locator.click(force=True, timeout=5000)
        page.wait_for_timeout(700)
        log(f"✅ Cita encontrada y seleccionada por AID/ID_TOA={id_toa}. Ahora se intentará ingresar al detalle.")
    except Exception as error:
        log(f"⚠️ No se pudo hacer el primer click sobre la cita {id_toa}: {error}")
        return False

    if not MONITOR_INGRESAR_A_DETALLE_CITA:
        log("⏸️ MONITOR_INGRESAR_A_DETALLE_CITA=false: se deja la cita solo seleccionada/resumen abierto.")
        return True

    # Estrategia 1: doble click directo sobre la barra/casilla.
    try:
        locator.dblclick(force=True, timeout=5000)
        page.wait_for_timeout(1800)
        if detalle_actividad_parece_abierto(page):
            log(f"✅ Ingreso al detalle detectado mediante doble click en la actividad {id_toa}.")
            return True
        log(f"ℹ️ Se ejecutó doble click sobre la actividad {id_toa}, pero aún no se detecta el detalle. Se probará otra estrategia.")
    except Exception as error:
        log(f"ℹ️ Doble click directo no fue suficiente para {id_toa}: {error}")

    # Estrategia 2: Enter sobre el elemento enfocado.
    try:
        locator.focus(timeout=2000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1800)
        if detalle_actividad_parece_abierto(page):
            log(f"✅ Ingreso al detalle detectado mediante Enter en la actividad {id_toa}.")
            return True
    except Exception as error:
        log(f"ℹ️ Enter sobre actividad no funcionó para {id_toa}: {error}")

    # Estrategia 3: doble click o click sobre el resumen flotante que aparece al seleccionar.
    resumen_selectores = [
        'div:has-text("Orden de trabajo"):has-text("Estado de actividad")',
        'div:has-text("Tipo de actividad"):has-text("Inicio de SLA")',
        '[role="dialog"]',
        '.oj-popup',
        '.jbf-popup',
    ]
    for selector in resumen_selectores:
        try:
            resumen = page.locator(selector).last
            resumen.wait_for(state="visible", timeout=1500)
            resumen.dblclick(force=True, timeout=3000)
            page.wait_for_timeout(1800)
            if detalle_actividad_parece_abierto(page):
                log(f"✅ Ingreso al detalle detectado haciendo doble click en el resumen de la cita {id_toa}.")
                return True
            log(f"ℹ️ Se ejecutó doble click sobre el resumen de la cita {id_toa}, pero aún no se detecta el detalle.")
        except Exception:
            continue

    # Estrategia 4: dblclick por JavaScript sobre la barra exacta.
    try:
        page.evaluate(
            """
            (aid) => {
              const el = document.querySelector(`[aid="${aid}"]`) || document.querySelector(`[data-id="a_${aid}"]`);
              if (!el) return false;
              el.scrollIntoView({block: 'center', inline: 'center'});
              const opts = { bubbles: true, cancelable: true, view: window };
              el.dispatchEvent(new MouseEvent('mousedown', opts));
              el.dispatchEvent(new MouseEvent('mouseup', opts));
              el.dispatchEvent(new MouseEvent('click', opts));
              el.dispatchEvent(new MouseEvent('dblclick', opts));
              return true;
            }
            """,
            id_toa,
        )
        page.wait_for_timeout(1800)
        if detalle_actividad_parece_abierto(page):
            log(f"✅ Ingreso al detalle detectado mediante evento JavaScript sobre la actividad {id_toa}.")
            return True
        log(f"ℹ️ Se envió evento dblclick por JavaScript sobre la actividad {id_toa}, pero no se confirmó el detalle.")
    except Exception as error:
        log(f"⚠️ No se pudo ingresar al detalle con JavaScript para {id_toa}: {error}")

    return False


def abrir_cita_por_aid_visible_o_scroll(page, id_toa: str, max_scrolls: int = MONITOR_MAX_SCROLLS) -> bool:
    """
    Busca una cita usando directamente el AID/ID_TOA.

    En esta versión el objetivo NO es extraer ni actualizar SharePoint, sino dejar
    el navegador detenido con la actividad ingresada. Por eso:
    - Busca la barra con aid="ID_TOA" o data-id="a_ID_TOA".
    - Si no está visible, hace scroll en la grilla.
    - Cuando la encuentra, primero la selecciona y luego intenta entrar al detalle.
    """
    id_toa = str(id_toa or "").strip()
    if not id_toa:
        log("⚠️ No llegó ID_TOA/AID para abrir la cita.")
        return False

    log(f"🎯 Intentando ingresar a la actividad directamente por AID/ID_TOA={id_toa}")

    # Importante: si antes se hizo una validación por grilla, la tabla puede quedar a media lista.
    # Reseteamos para escanear desde el inicio y no saltarnos actividades virtualizadas.
    resetear_grilla_al_inicio(page)

    selectores = [
        f'[aid="{id_toa}"]',
        f'[data-id="a_{id_toa}"]',
        f'.toaGantt-tb[aid="{id_toa}"]',
        f'.toaGantt-tb[data-id="a_{id_toa}"]',
        f'[data-field="act_bar"] [aid="{id_toa}"]',
        f'[data-field="act_bar"] [data-id="a_{id_toa}"]',
    ]

    for intento in range(max_scrolls + 1):
        for selector in selectores:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="attached", timeout=1200)
                locator.scroll_into_view_if_needed(timeout=2500)
                locator.wait_for(state="visible", timeout=2500)
                ok = ingresar_detalle_desde_actividad(page, locator, id_toa)
                if ok:
                    log(f"✅ Actividad ingresada o acción de ingreso ejecutada. AID/ID_TOA={id_toa}. Selector usado: {selector}")
                    return True
            except Exception:
                continue

        if intento < max_scrolls:
            log(f"🔄 AID/ID_TOA={id_toa} aún no visible. Scroll {intento + 1}/{max_scrolls}...")
            scroll_grilla(page)

    log(f"⚠️ No se pudo ingresar a la actividad por AID/ID_TOA={id_toa} después de {max_scrolls} scrolls.")
    return False


def mantener_navegador_abierto_hasta_ctrl_c() -> None:
    """Mantiene el navegador abierto para que el usuario pueda revisar la cita abierta."""
    import time

    log("⏸️ Proceso detenido aquí como solicitaste. La actividad debería quedar ingresada/abierta en TOA.")
    log("🛑 Cuando quieras cerrar el navegador/proceso, presiona CTRL+C en la consola.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log("🛑 Cierre manual recibido. Cerrando navegador.")


def esperar_detalle_actividad(page, timeout_ms: int = 15000) -> bool:
    """Espera hasta que se vea el formulario real de detalle de actividad."""
    selectores = [
        '[data-label="astatus"]',
        '[aria-describedby][data-label="astatus"]',
        'label[aria-label="Estado de actividad"]',
        'label:has-text("Estado de actividad")',
        'button[aria-label="Consola de despacho"]',
        '[data-ofsc-role="button-navigate-back"] button',
    ]

    fin = datetime.now().timestamp() + (timeout_ms / 1000)
    while datetime.now().timestamp() < fin:
        for selector in selectores:
            try:
                if page.locator(selector).first.count() > 0:
                    try:
                        page.locator(selector).first.wait_for(state="visible", timeout=700)
                    except Exception:
                        pass
                    return True
            except Exception:
                continue
        page.wait_for_timeout(300)

    return False


def extraer_estado_actividad_desde_detalle(page) -> Optional[str]:
    """
    Lee el valor exacto del detalle de TOA:
    <div class="form-item" data-label="astatus">pendiente</div>
    """
    log("🔎 Extrayendo 'Estado de actividad' desde el detalle de la cita.")

    if not esperar_detalle_actividad(page, timeout_ms=15000):
        log("⚠️ No se detectó el formulario de detalle de actividad antes de extraer el estado.")

    script = """
    () => {
      const leer = (el) => {
        if (!el) return null;
        const value = el.value || el.getAttribute('value') || el.innerText || el.textContent || '';
        const txt = String(value).replace(/\\s+/g, ' ').trim();
        return txt || null;
      };

      // Selector principal entregado desde el HTML del detalle.
      const directo = document.querySelector('[data-label="astatus"]');
      const valorDirecto = leer(directo);
      if (valorDirecto) return valorDirecto;

      // Fallback: buscar el label Estado de actividad y leer el elemento asociado por for/id.
      const labels = Array.from(document.querySelectorAll('label'));
      const label = labels.find((el) => {
        const txt = String(el.innerText || el.textContent || el.getAttribute('aria-label') || '')
          .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
          .replace(/\\s+/g, ' ')
          .trim()
          .toLowerCase();
        return txt === 'estado de actividad' || txt.includes('estado de actividad');
      });

      if (label) {
        const forId = label.getAttribute('for');
        if (forId) {
          const asociado = document.getElementById(forId);
          const valorAsociado = leer(asociado);
          if (valorAsociado) return valorAsociado;
        }

        const contenedor = label.closest('.form-text-field, .form-label-group, div')?.parentElement || label.closest('div');
        if (contenedor) {
          const candidatos = Array.from(contenedor.querySelectorAll('.form-item, [data-label], input, textarea, div'));
          for (const el of candidatos) {
            const txt = leer(el);
            if (!txt) continue;
            const norm = txt.normalize('NFD').replace(/[\u0300-\u036f]/g, '').trim().toLowerCase();
            if (norm && norm !== 'estado de actividad') return txt;
          }
        }
      }

      return null;
    }
    """

    estado_raw = leer_texto_js(page, script)
    estado = normalizar_estado(estado_raw)
    if estado:
        log(f"✅ Estado de actividad extraído desde detalle: {estado}")
        return estado

    log(f"⚠️ No se pudo normalizar el estado extraído desde detalle. Valor bruto: {estado_raw}")
    return None


def volver_a_consola_despacho(page) -> bool:
    """Presiona el botón superior de volver: title/aria-label='Consola de despacho'."""
    log("↩️ Volviendo a Consola de despacho.")

    selectores = [
        '.navigation-back-region button[aria-label="Consola de despacho"]',
        '.navigation-back-region button[title="Consola de despacho"]',
        '[data-ofsc-role="button-navigate-back"] button[aria-label="Consola de despacho"]',
        '[data-ofsc-role="button-navigate-back"] button',
        'button[aria-label="Consola de despacho"]',
        'button[title="Consola de despacho"]',
        'button:has-text("Consola de despacho")',
    ]

    for selector in selectores:
        try:
            boton = page.locator(selector).first
            boton.wait_for(state="visible", timeout=5000)
            boton.click(force=True, timeout=5000)
            page.wait_for_timeout(2000)
            log(f"✅ Botón de volver presionado con selector: {selector}")
            return True
        except Exception:
            continue

    try:
        page.keyboard.press("Alt+Left")
        page.wait_for_timeout(2000)
        log("✅ Se intentó volver con Alt+Left como respaldo.")
        return True
    except Exception as error:
        log(f"⚠️ No se pudo volver a Consola de despacho: {error}")
        return False


def extraer_observacion_desde_detalle(page) -> Optional[str]:
    """
    Intenta leer observaciones desde el detalle de la cita.
    Soporta campos editables y readonly. El formulario de creación usaba
    data-label="XA_obse_tecn", por eso se prioriza ese identificador.
    """

    script = """
    () => {
      const candidatos = [
        '[data-label="XA_obse_tecn"]',
        '[data-label*="obse" i]',
        '[aria-label*="Observ" i]',
        '[aria-label*="Comentario" i]',
        '[aria-label*="Nota" i]',
        'textarea',
        'input'
      ];

      const leerNodo = (el) => {
        if (!el) return null;
        const value = el.value || el.getAttribute('value') || el.innerText || el.textContent || '';
        const txt = String(value).trim();
        return txt || null;
      };

      for (const selector of candidatos) {
        const nodos = Array.from(document.querySelectorAll(selector));
        for (const el of nodos) {
          const label = [
            el.getAttribute('data-label') || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('placeholder') || '',
            el.closest('[aria-label]')?.getAttribute('aria-label') || '',
            el.closest('[data-label]')?.getAttribute('data-label') || ''
          ].join(' ').toLowerCase();

          const txt = leerNodo(el);
          if (!txt) continue;

          if (
            selector === '[data-label="XA_obse_tecn"]' ||
            label.includes('obse') ||
            label.includes('observ') ||
            label.includes('coment') ||
            label.includes('nota')
          ) {
            return txt;
          }
        }
      }

      // Fallback: buscar texto de etiqueta y leer contenido cercano.
      const todos = Array.from(document.querySelectorAll('label, div, span, button'));
      for (const el of todos) {
        const t = (el.innerText || el.textContent || '').trim().toLowerCase();
        if (!t) continue;
        if (t.includes('observ') || t.includes('comentario') || t.includes('nota')) {
          const cont = el.closest('div') || el.parentElement;
          if (!cont) continue;
          const textoCont = (cont.innerText || cont.textContent || '').trim();
          const limpio = textoCont
            .replace(/Observaciones técnicas/ig, '')
            .replace(/Observaciones tecnicas/ig, '')
            .replace(/Observaciones/ig, '')
            .replace(/Comentario/ig, '')
            .replace(/Nota/ig, '')
            .trim();
          if (limpio) return limpio;
        }
      }

      return null;
    }
    """

    observacion = leer_texto_js(page, script)

    if observacion:
        # Evita guardar basura muy genérica de la pantalla.
        observacion = re.sub(r"\s+", " ", observacion).strip()
        if len(observacion) > 2000:
            observacion = observacion[:2000]
        log("✅ Observación detectada.")
        return observacion

    log("⚠️ No se encontró observación visible en el detalle.")
    return None


def volver_a_grilla(page) -> None:
    """Vuelve al panel anterior después de abrir el detalle de actividad."""
    selectores = [
        'button:visible:has-text("Cerrar")',
        'button:visible:has-text("Cancelar")',
        'button:visible:has-text("Volver")',
        'button[aria-label*="Cerrar"]:visible',
        'button[aria-label*="Atrás"]:visible',
        'button[aria-label*="Atras"]:visible',
    ]

    for selector in selectores:
        try:
            boton = page.locator(selector).first
            boton.wait_for(state="visible", timeout=1500)
            boton.click(force=True)
            page.wait_for_timeout(1000)
            return
        except Exception:
            continue

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(700)
    except Exception:
        pass


def cerrar_resumen_actividad(page) -> None:
    """Cierra popups/resúmenes sin afectar la grilla."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass


def extraer_texto_resumen_actividad_visible(page) -> Optional[str]:
    """Lee el popup/ficha visible que aparece al apretar la casilla de actividad."""
    script = """
    () => {
      const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 80 && rect.height > 40;
      };

      const nodos = Array.from(document.querySelectorAll('oj-popup, [role="dialog"], [class*="popup" i], [class*="popover" i], [class*="tooltip" i], [class*="hint" i], div, section, aside'));
      const candidatos = nodos
        .filter(visible)
        .map((el) => {
          const text = (el.innerText || el.textContent || '').trim();
          const rect = el.getBoundingClientRect();
          return {
            text,
            len: text.length,
            area: rect.width * rect.height,
            z: Number(window.getComputedStyle(el).zIndex) || 0
          };
        })
        .filter((x) => x.text.includes('Orden de trabajo') && x.text.includes('Estado de actividad'))
        .sort((a, b) => a.len - b.len || b.z - a.z || a.area - b.area);

      return candidatos.length ? candidatos[0].text : null;
    }
    """
    return leer_texto_js(page, script)


def extraer_info_resumen_actividad_visible(page) -> Dict[str, Optional[str]]:
    """
    Lee el resumen/popup de la actividad.

    Problema detectado:
    TOA renderiza el popup en dos columnas: etiquetas a la izquierda y valores a la derecha.
    Cuando se usa innerText completo, a veces queda primero toda la columna de etiquetas y luego
    toda la columna de valores. Por eso el parser anterior leía mal:
      Orden='Nombre' | Tipo='Orden de trabajo' | Estado='Tipo de actividad'

    Solución:
    Primero intentamos extraer pares etiqueta/valor por coordenadas visuales del DOM
    (misma fila/top). Solo si eso falla usamos el parser textual anterior como fallback.
    """

    script = """
    () => {
      const normalizar = (txt) => String(txt || '')
        .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
        .replace(/\\s+/g, ' ')
        .trim()
        .toLowerCase();

      const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 2 && rect.height > 2;
      };

      const texto = (el) => String(el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();

      const contenedores = Array.from(document.querySelectorAll('oj-popup, [role="dialog"], [class*="popup" i], [class*="popover" i], [class*="tooltip" i], [class*="hint" i], div, section, aside'))
        .filter(visible)
        .map((el) => {
          const t = texto(el);
          const rect = el.getBoundingClientRect();
          return { el, text: t, rect, area: rect.width * rect.height, z: Number(window.getComputedStyle(el).zIndex) || 0 };
        })
        .filter((x) => normalizar(x.text).includes('orden de trabajo') && normalizar(x.text).includes('estado de actividad'))
        .sort((a, b) => a.area - b.area || b.z - a.z || a.text.length - b.text.length);

      const popup = contenedores.length ? contenedores[0].el : null;
      if (!popup) return null;

      const popupText = texto(popup);
      const labels = [
        'Categoria del Cliente', 'Categoría del Cliente', 'Nombre', 'Numero de cuenta', 'Número de cuenta',
        'Comuna', 'Zona de trabajo', 'Telefono', 'Teléfono', 'ID de Ticket', 'Tipo de actividad',
        'Intervalo de tiempo', 'Orden de trabajo', 'Estado de actividad', 'Inicio de SLA',
        'Inicio – Fin', 'Inicio - Fin', 'Finalizacion', 'Finalización', 'Duracion', 'Duración',
        'Observacion de actividad', 'Observación de actividad', 'Observaciones tecnicas',
        'Observaciones técnicas', 'Observaciones', 'Observacion', 'Observación', 'Comentario', 'Nota'
      ];
      const labelNorms = labels.map(normalizar);

      const esLabel = (txt) => labelNorms.includes(normalizar(txt).replace(/:$/, ''));

      // Nodos hoja: evita tomar contenedores grandes que repiten todo el texto.
      const nodos = Array.from(popup.querySelectorAll('*'))
        .filter(visible)
        .map((el) => ({ el, text: texto(el), rect: el.getBoundingClientRect() }))
        .filter((x) => x.text && x.text.length <= 180)
        .filter((x) => {
          const hijosConTexto = Array.from(x.el.children || [])
            .filter(visible)
            .map(texto)
            .filter(Boolean);
          return hijosConTexto.length === 0;
        });

      const filas = [];
      for (const n of nodos) {
        const center = Math.round(n.rect.top + n.rect.height / 2);
        let fila = filas.find((f) => Math.abs(f.center - center) <= 7);
        if (!fila) {
          fila = { center, items: [] };
          filas.push(fila);
        }
        fila.items.push(n);
      }

      const pares = {};
      for (const fila of filas.sort((a, b) => a.center - b.center)) {
        const items = fila.items.sort((a, b) => a.rect.left - b.rect.left);
        if (items.length < 2) continue;

        for (let i = 0; i < items.length - 1; i++) {
          const posibleLabel = items[i].text.replace(/:$/, '').trim();
          if (!esLabel(posibleLabel)) continue;

          const valor = items
            .slice(i + 1)
            .map((x) => x.text)
            .filter((t) => t && !esLabel(t))
            .join(' ')
            .trim();

          if (valor) {
            pares[normalizar(posibleLabel)] = valor;
          }
        }
      }

      const get = (...names) => {
        for (const n of names) {
          const key = normalizar(n);
          if (pares[key]) return pares[key];
        }
        return null;
      };

      return {
        texto: popupText,
        OrdenTrabajo: get('Orden de trabajo'),
        TipoActividad: get('Tipo de actividad'),
        EstadoActividad: get('Estado de actividad'),
        InicioSLA: get('Inicio de SLA'),
        ObservacionActividad: get(
          'Observacion de actividad', 'Observación de actividad', 'Observaciones tecnicas',
          'Observaciones técnicas', 'Observaciones', 'Observacion', 'Observación', 'Comentario', 'Nota'
        ),
        paresDetectados: pares
      };
    }
    """

    try:
        info_dom = page.evaluate(script)
    except Exception:
        info_dom = None

    if isinstance(info_dom, dict) and info_dom.get("texto"):
        # Si la extracción por coordenadas logró al menos uno de los campos críticos, se usa.
        if info_dom.get("OrdenTrabajo") or info_dom.get("TipoActividad") or info_dom.get("EstadoActividad"):
            return {
                "texto": info_dom.get("texto"),
                "OrdenTrabajo": info_dom.get("OrdenTrabajo"),
                "TipoActividad": info_dom.get("TipoActividad"),
                "EstadoActividad": info_dom.get("EstadoActividad"),
                "InicioSLA": info_dom.get("InicioSLA"),
                "ObservacionActividad": info_dom.get("ObservacionActividad"),
            }

    # Fallback textual anterior.
    texto = extraer_texto_resumen_actividad_visible(page)
    if not texto:
        return {}

    return {
        "texto": texto,
        "OrdenTrabajo": parsear_valor_por_label(texto, ["Orden de trabajo", "Orden Trabajo"]),
        "TipoActividad": parsear_valor_por_label(texto, ["Tipo de actividad", "Tipo actividad"]),
        "EstadoActividad": parsear_valor_por_label(texto, ["Estado de actividad", "Estado actividad"]),
        "InicioSLA": parsear_valor_por_label(texto, ["Inicio de SLA", "Inicio SLA"]),
        "ObservacionActividad": parsear_valor_por_label(
            texto,
            [
                "Observación de actividad",
                "Observacion de actividad",
                "Observaciones técnicas",
                "Observaciones tecnicas",
                "Observaciones",
                "Observación",
                "Observacion",
                "Comentario",
                "Nota",
            ],
        ),
    }


def obtener_botones_actividad_visibles(page) -> List[Dict[str, Any]]:
    """
    Obtiene las barras/casillas de actividad actualmente renderizadas en la grilla.

    Además del AID, lee los valores reales de la misma fila desde las columnas de la
    grilla Oracle DataGrid:
    - appt_number  -> Orden de trabajo / orden de venta
    - aworktype    -> Tipo de actividad
    - astatus      -> Estado de actividad

    Esto evita el error donde el resumen flotante se parseaba mal y devolvía
    Orden='Dirección' | Tipo='Dirección' | Estado='Dirección'.
    """
    script = r"""
    () => {
      const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 5 && rect.height > 5 && style.display !== 'none' && style.visibility !== 'hidden';
      };

      const cellText = (rowIndex, field) => {
        if (rowIndex === null || rowIndex === undefined || rowIndex === '') return null;
        const selector = `[data-row-index="${CSS.escape(String(rowIndex))}"][data-field="${CSS.escape(String(field))}"]`;
        const cell = document.querySelector(selector);
        return cell ? clean(cell.innerText || cell.textContent || '') : null;
      };

      return Array.from(document.querySelectorAll('[data-field="act_bar"] .toaGantt-tb, .toaGantt-tb'))
        .filter(visible)
        .map((el, index) => {
          const cell = el.closest('[data-row-index]');
          const rowIndex = cell ? cell.getAttribute('data-row-index') : null;
          const aid = el.getAttribute('aid') || (el.getAttribute('data-id') || '').replace(/^a_/, '');
          return {
            index,
            aid,
            dataId: el.getAttribute('data-id'),
            rowIndex,
            attrStatus: el.getAttribute('data-activity-status'),
            ariaLabel: el.getAttribute('aria-label') || '',
            OrdenTrabajo: cellText(rowIndex, 'appt_number'),
            TipoActividad: cellText(rowIndex, 'aworktype'),
            EstadoActividad: cellText(rowIndex, 'astatus') || el.getAttribute('data-activity-status')
          };
        });
    }
    """
    try:
        resultado = page.evaluate(script)
    except Exception:
        return []

    if not isinstance(resultado, list):
        return []
    return resultado


def obtener_fila_grilla_por_aid(page, id_toa: str) -> Dict[str, Any]:
    """Busca una fila renderizada por AID/ID_TOA y devuelve sus datos de grilla."""
    script = r"""
    (idToa) => {
      const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 5 && rect.height > 5 && style.display !== 'none' && style.visibility !== 'hidden';
      };
      const cellText = (rowIndex, field) => {
        if (rowIndex === null || rowIndex === undefined || rowIndex === '') return null;
        const selector = `[data-row-index="${CSS.escape(String(rowIndex))}"][data-field="${CSS.escape(String(field))}"]`;
        const cell = document.querySelector(selector);
        return cell ? clean(cell.innerText || cell.textContent || '') : null;
      };

      const selector = `.toaGantt-tb[aid="${CSS.escape(String(idToa))}"], .toaGantt-tb[data-id="a_${CSS.escape(String(idToa))}"]`;
      const el = Array.from(document.querySelectorAll(selector)).find(visible);
      if (!el) return null;
      const cell = el.closest('[data-row-index]');
      const rowIndex = cell ? cell.getAttribute('data-row-index') : null;
      const aid = el.getAttribute('aid') || (el.getAttribute('data-id') || '').replace(/^a_/, '');
      return {
        aid,
        dataId: el.getAttribute('data-id'),
        rowIndex,
        attrStatus: el.getAttribute('data-activity-status'),
        ariaLabel: el.getAttribute('aria-label') || '',
        OrdenTrabajo: cellText(rowIndex, 'appt_number'),
        TipoActividad: cellText(rowIndex, 'aworktype'),
        EstadoActividad: cellText(rowIndex, 'astatus') || el.getAttribute('data-activity-status')
      };
    }
    """
    try:
        resultado = page.evaluate(script, str(id_toa))
    except Exception:
        return {}
    return resultado if isinstance(resultado, dict) else {}


def valor_grilla_util(valor: Optional[str]) -> Optional[str]:
    """Limpia valores leídos de la grilla y descarta etiquetas/headers inválidos."""
    txt = str(valor or "").replace("\xa0", " ").strip()
    while "  " in txt:
        txt = txt.replace("  ", " ")
    if not txt:
        return None

    invalidos = {
        "dirección", "direccion", "nombre", "orden de trabajo", "tipo de actividad",
        "estado de actividad", "recurso", "actividad", "inicio", "finalización",
        "finalizacion", "intervalo de tiempo", "comuna",
    }
    if normalizar_texto(txt).lower().strip() in invalidos:
        return None
    return txt


def construir_info_desde_fila_grilla(boton_info: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Construye el mismo diccionario de info usando directamente la fila del DataGrid."""
    return {
        "texto": None,
        "OrdenTrabajo": valor_grilla_util(boton_info.get("OrdenTrabajo")),
        "TipoActividad": valor_grilla_util(boton_info.get("TipoActividad")),
        "EstadoActividad": valor_grilla_util(boton_info.get("EstadoActividad") or boton_info.get("attrStatus")),
        "InicioSLA": None,
        "ObservacionActividad": None,
    }


def combinar_info_fila_y_resumen(
    info_fila: Dict[str, Optional[str]],
    info_resumen: Dict[str, Optional[str]],
) -> Dict[str, Optional[str]]:
    """
    Prefiere los valores de la fila de la grilla para Orden/Tipo/Estado.
    El resumen se mantiene como respaldo para observación u otros campos.
    """
    return {
        "texto": info_resumen.get("texto") or info_fila.get("texto"),
        "OrdenTrabajo": valor_grilla_util(info_fila.get("OrdenTrabajo")) or valor_grilla_util(info_resumen.get("OrdenTrabajo")),
        "TipoActividad": valor_grilla_util(info_fila.get("TipoActividad")) or valor_grilla_util(info_resumen.get("TipoActividad")),
        "EstadoActividad": valor_grilla_util(info_fila.get("EstadoActividad")) or valor_grilla_util(info_resumen.get("EstadoActividad")),
        "InicioSLA": info_resumen.get("InicioSLA") or info_fila.get("InicioSLA"),
        "ObservacionActividad": info_resumen.get("ObservacionActividad") or info_fila.get("ObservacionActividad"),
    }


def actividad_resumen_coincide_con_sharepoint(
    info: Dict[str, Optional[str]],
    orden_sharepoint: Optional[str],
    tipo_sharepoint: Optional[str],
) -> bool:
    orden_toa = info.get("OrdenTrabajo")
    tipo_toa = info.get("TipoActividad")

    if not ordenes_coinciden(orden_sharepoint, orden_toa):
        return False

    if MONITOR_VALIDAR_TIPO and tipo_sharepoint:
        return tipos_coinciden(tipo_sharepoint, tipo_toa)

    return True


def buscar_actividad_por_orden_tipo_en_grilla(
    page,
    id_toa: str,
    orden_trabajo: Optional[str],
    tipo_actividad: Optional[str],
    max_scrolls: int = MONITOR_MAX_SCROLLS,
) -> Dict[str, Optional[str]]:
    """
    Búsqueda rápida:
    1) Primero intenta encontrar el AID/ID_TOA directo en la grilla renderizada.
    2) Si no aparece, busca por OrdenVenta/OrdenTrabajo + TipoActividad leyendo celdas.
    3) Solo después de validar devuelve el AID para ingresar al detalle.

    Esto evita abrir actividad por actividad y acelera el monitoreo.
    """
    log(
        f"🔎 Buscando actividad rápida por grilla. "
        f"Orden='{orden_trabajo}' | Tipo='{tipo_actividad}' | ID_TOA esperado={id_toa}"
    )

    if not orden_trabajo and not id_toa:
        log("⚠️ No llegó OrdenVenta/OrdenTrabajo ni ID_TOA desde SharePoint. No se puede buscar por grilla.")
        return {}

    resetear_grilla_al_inicio(page)
    evaluados: set[str] = set()

    def construir_resultado(boton_info: Dict[str, Any], motivo: str) -> Dict[str, Optional[str]]:
        info = construir_info_desde_fila_grilla(boton_info)
        aid = solo_digitos(boton_info.get("aid")) or solo_digitos(id_toa)
        estado_toa = info.get("EstadoActividad") or boton_info.get("attrStatus")
        estado_normalizado = normalizar_estado(estado_toa) or normalizar_estado(boton_info.get("attrStatus"))
        log(
            f"✅ Actividad correcta encontrada en grilla por {motivo}. "
            f"AID={aid or id_toa} | Orden='{info.get('OrdenTrabajo')}' | "
            f"Tipo='{info.get('TipoActividad')}' | Estado='{estado_normalizado or estado_toa}'"
        )
        return {
            "aid": aid or id_toa,
            "Estado_actividad": estado_normalizado,
            "Observacion_actividad": None,
            "OrdenTrabajo_TOA": info.get("OrdenTrabajo"),
            "TipoActividad_TOA": info.get("TipoActividad"),
            "rowIndex": str(boton_info.get("rowIndex")) if boton_info.get("rowIndex") is not None else None,
        }

    for intento_scroll in range(max_scrolls + 1):
        # 1) Camino más rápido y seguro: AID directo.
        if id_toa:
            fila_aid = obtener_fila_grilla_por_aid(page, id_toa)
            if fila_aid:
                info_aid = construir_info_desde_fila_grilla(fila_aid)
                orden_ok = True
                tipo_ok = True
                if orden_trabajo and info_aid.get("OrdenTrabajo"):
                    orden_ok = ordenes_coinciden(orden_trabajo, info_aid.get("OrdenTrabajo"))
                if MONITOR_VALIDAR_TIPO and tipo_actividad and info_aid.get("TipoActividad"):
                    tipo_ok = tipos_coinciden(tipo_actividad, info_aid.get("TipoActividad"))
                if orden_ok and tipo_ok:
                    return construir_resultado(fila_aid, "AID/ID_TOA")
                log(
                    "⚠️ Se encontró el AID, pero Orden/Tipo no coinciden. "
                    f"SP Orden='{orden_trabajo}' Tipo='{tipo_actividad}' | "
                    f"TOA Orden='{info_aid.get('OrdenTrabajo')}' Tipo='{info_aid.get('TipoActividad')}'"
                )
                return {}

        botones = obtener_botones_actividad_visibles(page)
        if not botones and intento_scroll == 0:
            log("⚠️ No se detectaron filas/casillas de actividad visibles en esta vista.")

        total_a_revisar = min(len(botones), MONITOR_MAX_ACTIVIDADES_VISIBLES)

        for boton_info in botones[:total_a_revisar]:
            aid = solo_digitos(boton_info.get("aid"))
            row_index = boton_info.get("rowIndex")
            clave = aid or f"row-{row_index}-idx-{boton_info.get('index')}"
            if clave in evaluados:
                continue
            evaluados.add(clave)

            info = construir_info_desde_fila_grilla(boton_info)
            orden_toa = info.get("OrdenTrabajo")
            tipo_toa = info.get("TipoActividad")
            estado_toa = info.get("EstadoActividad") or boton_info.get("attrStatus")

            if MONITOR_LOG_FILAS_GRILLA:
                log(
                    "🧩 Fila revisada "
                    f"row={row_index} aid={aid or 'sin-aid'} | "
                    f"Orden='{orden_toa}' | Tipo='{tipo_toa}' | Estado='{estado_toa}'"
                )

            aid_coincide = bool(id_toa and aid and str(aid) == str(id_toa))
            orden_tipo_coincide = actividad_resumen_coincide_con_sharepoint(info, orden_trabajo, tipo_actividad)

            if not orden_tipo_coincide:
                continue

            if MONITOR_VALIDAR_ID_TOA and id_toa and aid and str(aid) != str(id_toa):
                log(
                    f"⚠️ Orden/Tipo coinciden, pero el AID no coincide. "
                    f"SharePoint ID_TOA={id_toa} | TOA aid={aid}. Se omite para no actualizar mal."
                )
                continue

            if aid_coincide or not MONITOR_VALIDAR_ID_TOA:
                return construir_resultado(boton_info, "Orden/Tipo")

        if intento_scroll < max_scrolls:
            scroll_grilla(page)

    log(
        "⚠️ No se encontró actividad coincidente en la grilla. "
        "Si el AID/orden no aparece, revisa que SharePoint tenga la RutaTOACompleta, InicioSLA y OrdenVenta correctos."
    )
    return {}

def estados_coinciden(estado_sp: Any, estado_toa: Any) -> bool:
    """Compara Estado_actividad de SharePoint contra Estado de actividad visible en TOA."""
    a = normalizar_estado(estado_sp)
    b = normalizar_estado(estado_toa)
    if not a or not b:
        return True
    return a == b


def abrir_cita_por_aid_sin_reset(page, id_toa: str) -> bool:
    """Ingresa a la actividad ya visible sin resetear la grilla ni cambiar la posición actual."""
    id_toa = str(id_toa or "").strip()
    if not id_toa:
        return False

    selectores = [
        f'[aid="{id_toa}"]',
        f'[data-id="a_{id_toa}"]',
        f'.toaGantt-tb[aid="{id_toa}"]',
        f'.toaGantt-tb[data-id="a_{id_toa}"]',
        f'[data-field="act_bar"] [aid="{id_toa}"]',
        f'[data-field="act_bar"] [data-id="a_{id_toa}"]',
    ]

    for selector in selectores:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="attached", timeout=1200)
            locator.scroll_into_view_if_needed(timeout=2500)
            locator.wait_for(state="visible", timeout=2500)
            return ingresar_detalle_desde_actividad(page, locator, id_toa)
        except Exception:
            continue
    return False


def extraer_valor_detalle_por_labels(page, labels: List[str]) -> Optional[str]:
    """Lee un valor del detalle usando textos de etiqueta, atributos aria-label o data-label."""
    script = r'''
    (labels) => {
      const norm = (value) => String(value || '')
        .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .toLowerCase();

      const wanted = labels.map(norm).filter(Boolean);
      const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const read = (el) => {
        if (!el) return null;
        const value = el.value || el.getAttribute('value') || el.innerText || el.textContent || '';
        const txt = clean(value);
        return txt || null;
      };

      const labelMatches = (txt) => {
        const n = norm(txt);
        return wanted.some(w => n === w || n.includes(w) || w.includes(n));
      };

      const directos = Array.from(document.querySelectorAll('[data-label], [aria-label], input, textarea'));
      for (const el of directos) {
        const dataLabel = el.getAttribute('data-label') || '';
        const ariaLabel = el.getAttribute('aria-label') || '';
        const placeholder = el.getAttribute('placeholder') || '';
        if (!labelMatches(`${dataLabel} ${ariaLabel} ${placeholder}`)) continue;
        const value = read(el);
        if (value && !labelMatches(value)) return value;
      }

      const labelsDom = Array.from(document.querySelectorAll('label, .form-label, [class*="label" i]'));
      for (const label of labelsDom) {
        const labelText = label.getAttribute('aria-label') || label.innerText || label.textContent || '';
        if (!labelMatches(labelText)) continue;

        const forId = label.getAttribute('for');
        if (forId) {
          const asociado = document.getElementById(forId);
          const value = read(asociado);
          if (value && !labelMatches(value)) return value;
        }

        const campo = label.closest('.form-text-field, .form-textarea-field, .form-field, .form-row, div') || label.parentElement;
        const contenedor = campo?.parentElement || campo;
        if (!contenedor) continue;
        const candidatos = Array.from(contenedor.querySelectorAll('.form-item, [data-label], textarea, input, div, span'));
        for (const el of candidatos) {
          const value = read(el);
          if (!value) continue;
          if (labelMatches(value)) continue;
          return value;
        }
      }

      const bloques = Array.from(document.querySelectorAll('.form-text-field, .form-textarea-field, .form-field, div'));
      for (const bloque of bloques) {
        const txt = clean(bloque.innerText || bloque.textContent || '');
        if (!txt) continue;
        const n = norm(txt);
        const match = wanted.find(w => n.includes(w));
        if (!match) continue;
        const partes = txt.split(/\n+/).map(x => clean(x)).filter(Boolean);
        for (let i = 0; i < partes.length; i++) {
          if (labelMatches(partes[i])) {
            for (const candidato of partes.slice(i + 1, i + 4)) {
              if (candidato && !labelMatches(candidato)) return candidato;
            }
          }
        }
      }
      return null;
    }
    '''
    try:
        valor = page.evaluate(script, labels)
    except Exception:
        return None
    if valor in [None, ""]:
        return None
    texto = re.sub(r"\s+", " ", str(valor)).strip()
    return texto or None


def extraer_id_toa_desde_detalle(page, id_toa_esperado: Optional[str] = None) -> Optional[str]:
    """Extrae el ID de cita/actividad desde el detalle, para confirmar que entramos a la actividad correcta."""
    labels = [
        "ID_TOA", "ID TOA", "IdActividadTOA", "ID Actividad TOA",
        "ID de actividad", "ID Actividad", "ID Cita", "ID de Cita",
        "Identificador de actividad", "Identificador", "AID", "Actividad TOA",
    ]
    texto = extraer_valor_detalle_por_labels(page, labels)
    digitos = solo_digitos(texto)

    if not digitos:
        esperado = solo_digitos(id_toa_esperado)
        if esperado:
            try:
                existe = page.evaluate(
                    """(id) => (document.body.innerText || document.body.textContent || '').includes(String(id))""",
                    esperado,
                )
                if existe:
                    return esperado
            except Exception:
                pass
        return None

    return digitos


def detalle_id_toa_coincide(page, id_toa_esperado: str) -> bool:
    esperado = solo_digitos(id_toa_esperado)
    encontrado = extraer_id_toa_desde_detalle(page, esperado)
    if esperado and encontrado and esperado == encontrado:
        log(f"✅ ID_TOA confirmado dentro del detalle: {encontrado}")
        return True
    if esperado and not encontrado:
        log("⚠️ No se pudo leer ID_TOA dentro del detalle. No se confirma la actividad.")
        return False
    log(f"⚠️ ID_TOA del detalle no coincide. Esperado={esperado} | Encontrado={encontrado}")
    return False


def buscar_ingresar_por_orden_estado_y_validar_id(
    page,
    id_toa: str,
    orden_sharepoint: Optional[str],
    estado_sharepoint: Optional[str],
    tipo_sharepoint: Optional[str],
    max_scrolls: int = MONITOR_MAX_SCROLLS,
) -> Dict[str, Optional[str]]:
    """
    Busca primero en la grilla por las 2 columnas solicitadas:

    - Orden de venta / Orden de trabajo: data-field="appt_number"
    - Tipo de actividad: data-field="aworktype"

    Este modo NO busca por AID al inicio ni usa Estado_actividad para decidir el ingreso.
    La idea es:
    1) Leer la fila visible de TOA.
    2) Confirmar que Orden + Tipo coinciden con SharePoint.
    3) Ingresar a esa actividad.
    4) Dentro del detalle validar que el ID_TOA sea el esperado.
    5) Extraer Estado_actividad y Observacion_actividad.
    """
    log(
        "🔎 Buscando actividad por Orden de venta + Tipo de actividad en la grilla. "
        f"Orden='{orden_sharepoint}' | Tipo='{tipo_sharepoint}'"
    )
    log("ℹ️ El ID_TOA NO se usa para buscar en la grilla; se validará recién dentro del detalle de la actividad.")

    if not orden_sharepoint:
        log("⚠️ SharePoint no entregó OrdenVenta/OrdenTrabajo. No se puede buscar por Orden + Tipo.")
        return {"__datos_validacion_incompletos": True}

    if not tipo_sharepoint:
        log("⚠️ SharePoint no entregó TipoActividad. No se puede buscar por Orden + Tipo.")
        return {"__datos_validacion_incompletos": True}

    # Importante: se parte desde el inicio de la grilla para no quedar en una posición virtualizada anterior.
    resetear_grilla_al_inicio(page)
    evaluados: set[str] = set()

    for intento_scroll in range(max_scrolls + 1):
        botones = obtener_botones_actividad_visibles(page)
        if not botones and intento_scroll == 0:
            log("⚠️ No se detectaron filas/casillas de actividad visibles en esta vista.")
            return {"__no_encontrada_marcar_eliminado": True, "motivo": "sin filas/casillas visibles en la grilla"}

        for boton_info in botones[:MONITOR_MAX_ACTIVIDADES_VISIBLES]:
            aid = solo_digitos(boton_info.get("aid"))
            row_index = boton_info.get("rowIndex")
            clave = aid or f"row-{row_index}-idx-{boton_info.get('index')}"
            if clave in evaluados:
                continue
            evaluados.add(clave)

            info = construir_info_desde_fila_grilla(boton_info)
            orden_toa = info.get("OrdenTrabajo")
            tipo_toa = info.get("TipoActividad")
            estado_toa = normalizar_estado(info.get("EstadoActividad") or boton_info.get("attrStatus"))

            orden_ok = ordenes_coinciden(orden_sharepoint, orden_toa)
            tipo_ok = tipos_coinciden(tipo_sharepoint, tipo_toa)

            # Para este flujo el ingreso se decide SOLO por las columnas visibles:
            # Orden de venta/trabajo + Tipo de actividad.
            if MONITOR_LOG_FILAS_GRILLA:
                log(
                    "🧩 Fila revisada "
                    f"row={row_index} aid={aid or 'sin-aid'} | "
                    f"Orden='{orden_toa}' | Tipo='{tipo_toa}' | Estado='{estado_toa}' | "
                    f"match Orden={orden_ok} Tipo={tipo_ok}"
                )

            if not (orden_ok and tipo_ok):
                continue

            log(
                f"✅ Candidato encontrado por Orden + Tipo. row={row_index} aid={aid or 'sin-aid'} | "
                f"Orden='{orden_toa}' | Tipo='{tipo_toa}' | Estado='{estado_toa}'"
            )

            if not aid:
                log("⚠️ La fila candidata no tiene AID visible. Se omite para evitar ingresar una actividad equivocada.")
                continue

            # Ya encontrado por Orden + Tipo, ahora se usa el AID SOLO para hacer click en esa misma fila.
            if not abrir_cita_por_aid_sin_reset(page, aid):
                log(f"⚠️ No se pudo ingresar al detalle del candidato AID={aid}.")
                continue

            if not esperar_detalle_actividad(page, timeout_ms=15000):
                log(f"⚠️ Se hizo click en AID={aid}, pero no abrió el detalle.")
                volver_a_consola_despacho(page)
                page.wait_for_timeout(1000)
                continue

            # Validación final dentro del detalle: aquí recién se confirma ID_TOA.
            if not detalle_id_toa_coincide(page, id_toa):
                log(f"⚠️ El candidato por Orden + Tipo no corresponde al ID_TOA {id_toa}. Volviendo y siguiendo búsqueda.")
                volver_a_consola_despacho(page)
                page.wait_for_timeout(1200)
                continue

            estado_detalle = extraer_estado_actividad_desde_detalle(page) or estado_toa
            observacion_detalle = extraer_observacion_desde_detalle(page)

            log(f"✅ Estado extraído desde detalle: {estado_detalle}")
            if observacion_detalle:
                log("✅ Observación extraída desde detalle.")
            else:
                log("ℹ️ La actividad no tiene observación visible o no se pudo extraer.")

            return {
                "aid": aid,
                "Estado_actividad": estado_detalle,
                "Observacion_actividad": observacion_detalle,
                "Estado_grilla": estado_toa,
                "OrdenTrabajo_TOA": orden_toa,
                "TipoActividad_TOA": tipo_toa,
                "rowIndex": str(row_index) if row_index is not None else None,
            }

        if intento_scroll < max_scrolls:
            scroll_grilla(page)

    log("⚠️ No se encontró actividad que coincida con Orden de venta/trabajo + Tipo de actividad en la grilla.")
    return {"__no_encontrada_marcar_eliminado": True, "motivo": "sin coincidencia por Orden de venta/trabajo + Tipo de actividad"}

def obtener_observacion_actividad(page, id_toa: str) -> Optional[str]:
    log(f"📝 Intentando abrir detalle para extraer observación de AID/ID {id_toa}")

    if not click_actividad_por_aid(page, id_toa):
        log("⚠️ No se pudo abrir la actividad para leer observación.")
        return None

    try:
        return extraer_observacion_desde_detalle(page)
    finally:
        volver_a_grilla(page)


def preparar_sesion_si_no_existe() -> None:
    if Path(STATE_FILE).exists():
        return

    log("⚠️ No existe state.json. Se abrirá navegador visible para iniciar sesión una vez.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context()
        page = context.new_page()
        page.goto(TOA_URL, wait_until="domcontentloaded", timeout=60000)
        iniciar_sesion_toa(page, context)
        browser.close()



def seleccionar_fecha_cita_monitor(page, inicio_sla: Any) -> int:
    """
    Navega al día de la cita para monitoreo.
    A diferencia del agendamiento, aquí se permite avanzar o retroceder,
    porque una cita creada puede quedar en días anteriores o posteriores.
    Retorna el delta de días aplicado: positivo si avanzó, negativo si retrocedió.
    """
    fecha_objetivo = convertir_fecha_chile(inicio_sla)
    hoy = datetime.now(ZoneInfo("America/Santiago")).date()
    dias_delta = (fecha_objetivo.date() - hoy).days

    if dias_delta == 0:
        return 0

    selector = (
        '[data-ofsc-id="dc__top_panel__date_picker__popup--button-next"]'
        if dias_delta > 0
        else '[data-ofsc-id="dc__top_panel__date_picker__popup--button-previous"]'
    )

    pasos = abs(dias_delta)
    direccion = "Siguiente" if dias_delta > 0 else "Anterior"
    log(f"📅 Navegando fecha monitor: {fecha_objetivo.date()} | {direccion} x{pasos}")

    for _ in range(pasos):
        boton = page.locator(selector).first
        boton.wait_for(state="visible", timeout=10000)
        boton.click(force=True)
        page.wait_for_timeout(250)

    return dias_delta


def restablecer_fecha_monitor(page, dias_delta: int) -> None:
    """
    Devuelve la grilla a la fecha actual después de monitorear una cita.

    Importante: seleccionar_fecha_cita_monitor() calcula el movimiento desde la fecha actual
    hacia la fecha de la cita. Por eso al finalizar se aplica exactamente el movimiento
    inverso, dejando TOA nuevamente en la fecha actual antes de procesar el siguiente
    registro de SharePoint.
    """
    if dias_delta == 0:
        log("📅 La actividad era de fecha actual. No es necesario restablecer fecha.")
        return

    selector = (
        '[data-ofsc-id="dc__top_panel__date_picker__popup--button-previous"]'
        if dias_delta > 0
        else '[data-ofsc-id="dc__top_panel__date_picker__popup--button-next"]'
    )

    pasos = abs(dias_delta)
    direccion = "Anterior" if dias_delta > 0 else "Siguiente"
    log(f"📅 Restableciendo fecha actual: {direccion} x{pasos}")

    for i in range(pasos):
        boton = page.locator(selector).first
        boton.wait_for(state="visible", timeout=8000)
        boton.click(force=True)
        page.wait_for_timeout(350)

    # Pequeña espera adicional para que Oracle DataGrid recargue después de volver a hoy.
    page.wait_for_timeout(900)
    log("✅ Fecha restablecida a la fecha actual para continuar con la siguiente actividad.")


def click_vista_lista(page) -> bool:
    """
    Fuerza la vista de lista antes de aplicar la vista jerárquica.
    En TOA este botón suele aparecer como:
    - title="Vista de lista"
    - aria-label="Vista de lista"
    - data-ofsc-id="dc__top_panel__page_selector__list__btn"

    Si ya está seleccionada, no hace nada y retorna True.
    """
    log("📋 Verificando botón 'Vista de lista'.")

    selectores_vista_lista = [
        'button[data-ofsc-id="dc__top_panel__page_selector__list__btn"]',
        '[data-ofsc-id="dc__top_panel__page_selector__list__btn"]',
        'button[title="Vista de lista"][aria-label="Vista de lista"]',
        'button[aria-label="Vista de lista"]',
        'button[title="Vista de lista"]',
        'button:has-text("Vista de lista")',
    ]

    for selector in selectores_vista_lista:
        try:
            boton = page.locator(selector).first
            boton.wait_for(state="visible", timeout=5000)

            aria_checked = (boton.get_attribute("aria-checked") or "").lower()
            option_selected = (boton.get_attribute("data-ofsc-option-selected") or "").lower()
            class_name = boton.get_attribute("class") or ""

            if aria_checked == "true" or option_selected == "true" or "radio-selected" in class_name:
                log(f"✅ 'Vista de lista' ya estaba seleccionada. Selector: {selector}")
                return True

            boton.click(force=True, timeout=5000)
            page.wait_for_timeout(1000)
            log(f"✅ Botón 'Vista de lista' presionado con selector: {selector}")
            return True
        except Exception:
            continue

    # Fallback por accesibilidad/rol, útil cuando el texto existe pero no está visible como selector clásico.
    try:
        boton = page.get_by_role("radio", name=re.compile(r"Vista\s+de\s+lista", re.I)).first
        boton.wait_for(state="visible", timeout=4000)
        aria_checked = (boton.get_attribute("aria-checked") or "").lower()
        if aria_checked != "true":
            boton.click(force=True, timeout=5000)
            page.wait_for_timeout(1000)
            log("✅ Botón 'Vista de lista' presionado por rol radio.")
        else:
            log("✅ 'Vista de lista' ya estaba seleccionada por rol radio.")
        return True
    except Exception:
        pass

    log("⚠️ No se pudo encontrar/presionar el botón 'Vista de lista'.")
    return False

def click_boton_ver_y_aplicar_jerarquicamente(page) -> bool:
    """
    Abre el menú/botón "Ver", asegura la opción "Aplicar jerárquicamente"
    y presiona "Aplicar". Esto es necesario después de mover la fecha en TOA,
    porque la grilla puede quedar sin aplicar la vista jerárquica y no renderizar
    correctamente las actividades por ruta/recurso.
    """
    log("👁️ Aplicando vista: Ver -> Aplicar jerárquicamente -> Aplicar")

    # 1) Abrir botón Ver
    selectores_ver = [
        'button[title="Ver"][aria-label="Ver"]',
        'button[aria-label="Ver"]',
        'button[title="Ver"]',
        'button:has-text("Ver")',
        '[data-ofsc-role="button-open-menu"] button:has-text("Ver")',
    ]

    abierto = False
    for selector in selectores_ver:
        try:
            boton_ver = page.locator(selector).first
            boton_ver.wait_for(state="visible", timeout=5000)
            boton_ver.click(force=True, timeout=5000)
            page.wait_for_timeout(700)
            abierto = True
            log(f"✅ Botón Ver presionado con selector: {selector}")
            break
        except Exception:
            continue

    if not abierto:
        log("⚠️ No se pudo encontrar/presionar el botón Ver.")
        return False

    # 2) Marcar Aplicar jerárquicamente solo si no está marcado.
    script_estado_checkbox = """
    () => {
      const norm = (txt) => String(txt || '')
        .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
        .toLowerCase().trim();

      const nodos = Array.from(document.querySelectorAll('label, oj-option, span, div'));
      const nodo = nodos.find(el => norm(el.innerText || el.textContent).includes('aplicar jerarquicamente'));
      if (!nodo) return {found:false, checked:false};

      let input = null;
      const label = nodo.closest('label') || (nodo.tagName && nodo.tagName.toLowerCase() === 'label' ? nodo : null);
      if (label && label.getAttribute('for')) {
        input = document.getElementById(label.getAttribute('for'));
      }
      if (!input) {
        const choice = nodo.closest('.oj-choice-item, .oj-checkboxset-wrapper, .oj-form-control-container');
        if (choice) input = choice.querySelector('input[type="checkbox"]');
      }
      if (!input) {
        input = document.querySelector('input[type="checkbox"]');
      }

      const choice = input ? input.closest('.oj-choice-item') : nodo.closest('.oj-choice-item');
      const checked = Boolean(
        (input && input.checked) ||
        (input && input.classList.contains('oj-selected')) ||
        (choice && choice.classList.contains('oj-selected'))
      );

      return {found:true, checked, inputId: input ? input.id : null};
    }
    """

    try:
        estado_checkbox = page.evaluate(script_estado_checkbox)
    except Exception:
        estado_checkbox = {"found": False, "checked": False}

    if not estado_checkbox or not estado_checkbox.get("found"):
        log("⚠️ No se encontró la opción 'Aplicar jerárquicamente'. Se intentará aplicar igual.")
    elif estado_checkbox.get("checked"):
        log("✅ 'Aplicar jerárquicamente' ya estaba marcado.")
    else:
        try:
            input_id = estado_checkbox.get("inputId")
            if input_id:
                page.locator(f'#{input_id.replace(":", "\\:").replace("|", "\\|")}').click(force=True, timeout=3000)
            else:
                page.locator('label:has-text("Aplicar jerárquicamente")').first.click(force=True, timeout=3000)
            page.wait_for_timeout(400)
            log("✅ 'Aplicar jerárquicamente' marcado.")
        except Exception:
            try:
                page.get_by_text("Aplicar jerárquicamente", exact=False).click(force=True, timeout=3000)
                page.wait_for_timeout(400)
                log("✅ 'Aplicar jerárquicamente' marcado por texto.")
            except Exception as error:
                log(f"⚠️ No se pudo marcar 'Aplicar jerárquicamente': {error}")

    # 3) Click en Aplicar
    selectores_aplicar = [
        'button[title="Aplicar"][aria-label="Aplicar"]',
        'button[aria-label="Aplicar"]',
        'button[title="Aplicar"]',
        'button:has-text("Aplicar")',
    ]

    for selector in selectores_aplicar:
        try:
            boton_aplicar = page.locator(selector).last
            boton_aplicar.wait_for(state="visible", timeout=5000)
            boton_aplicar.click(force=True, timeout=5000)
            page.wait_for_timeout(1800)
            log(f"✅ Botón Aplicar presionado con selector: {selector}")
            return True
        except Exception:
            continue

    log("⚠️ No se pudo encontrar/presionar el botón Aplicar.")
    return False


def aplicar_vista_despues_de_cambiar_fecha(page, dias_delta: int) -> None:
    """
    Después de avanzar/retroceder fechas, deja la grilla en Vista de lista y,
    solo cuando corresponda, aplica Ver -> Aplicar jerárquicamente -> Aplicar.

    Importante:
    - Vista de lista es un botón tipo radio, por eso se puede verificar sin problema.
    - Ver -> Aplicar jerárquicamente -> Aplicar se ejecuta solo una vez por ejecución
      cuando MONITOR_APLICAR_VER_SOLO_UNA_VEZ=true, para evitar que TOA marque/desmarque
      la opción repetidamente.
    """
    global VISTA_JERARQUICA_APLICADA_EN_ESTA_EJECUCION

    debe_seleccionar_lista = MONITOR_VISTA_LISTA_SIEMPRE or (MONITOR_VISTA_LISTA_AL_CAMBIAR_FECHA and dias_delta != 0)
    debe_aplicar_ver = MONITOR_APLICAR_VER_SIEMPRE or (MONITOR_APLICAR_VER_AL_CAMBIAR_FECHA and dias_delta != 0)

    if not debe_seleccionar_lista and not debe_aplicar_ver:
        return

    try:
        if debe_seleccionar_lista:
            click_vista_lista(page)

        if debe_aplicar_ver:
            if MONITOR_APLICAR_VER_SOLO_UNA_VEZ and VISTA_JERARQUICA_APLICADA_EN_ESTA_EJECUCION:
                log("👁️ Vista jerárquica ya aplicada en esta ejecución. Se omite Ver -> Aplicar para no alternar la opción.")
                return

            aplicado = click_boton_ver_y_aplicar_jerarquicamente(page)
            if aplicado:
                VISTA_JERARQUICA_APLICADA_EN_ESTA_EJECUCION = True
    except Exception as error:
        log(f"⚠️ Error aplicando vista después de cambiar fecha: {error}")


def _click_nodo_por_texto_monitor(page, texto: str, descripcion: str, timeout: int = 10000) -> bool:
    """Hace click en un nodo visible del árbol TOA usando texto, sin depender de providerid/mapping."""
    texto = str(texto or "").strip()
    if not texto:
        return False

    # Selectores ordenados desde lo más específico hacia lo más amplio.
    selectores = [
        f'button.edt-label:text-is("{texto}")',
        f'button:text-is("{texto}")',
        f'[role="treeitem"] button:text-is("{texto}")',
        f'[role="treeitem"]:has-text("{texto}") button.edt-label',
        f'span:text-is("{texto}")',
        f'div:text-is("{texto}")',
        f'text="{texto}"',
    ]

    # Variantes útiles cuando TOA cambia mayúsculas/minúsculas o normaliza espacios.
    variantes = list(dict.fromkeys([texto, texto.upper(), texto.title(), normalizar_texto(texto)]))

    for variante in variantes:
        for selector in selectores:
            selector_final = selector.replace(texto, variante)
            try:
                loc = page.locator(selector_final).first
                loc.wait_for(state="visible", timeout=timeout)
                try:
                    loc.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                loc.click(force=True)
                page.wait_for_timeout(700)
                log(f"✅ Nodo seleccionado por texto ({descripcion}): {variante}")
                return True
            except Exception:
                continue

    return False


def seleccionar_zona_y_ruta_toa_sin_mapping(page, ruta_completa: str) -> None:
    """Selecciona zona/ruta en TOA sin flujo Mapping, usando solo RutaTOACompleta desde SharePoint."""
    ruta_limpia = str(ruta_completa or "").strip()
    if not ruta_limpia:
        raise ValueError("RutaTOACompleta viene vacía desde SharePoint.")

    log("🧭 Seleccionando zona y ruta TOA sin flujo Mapping")
    log(f"📌 RutaTOACompleta: {ruta_limpia}")

    # Formato usado cuando no hay zona padre: "| NOMBRE_RUTA".
    if ruta_limpia.startswith("| "):
        nombre_nodo = ruta_limpia[2:].strip()
        if _click_nodo_por_texto_monitor(page, nombre_nodo, "ruta directa"):
            return
        raise RuntimeError(f"No se pudo seleccionar la ruta directa por texto: {nombre_nodo}")

    partes = ruta_limpia.split("|")
    if len(partes) != 2:
        # Último respaldo: intentar hacer click directo con todo el texto.
        if _click_nodo_por_texto_monitor(page, ruta_limpia, "ruta completa"):
            return
        raise ValueError(
            f"Formato inválido en RutaTOACompleta: {ruta_limpia}. Debe venir como: MACROZONA | RUTA"
        )

    zona_toa = partes[0].strip()
    ruta_toa = partes[1].strip()
    log(f"📌 Zona padre: {zona_toa}")
    log(f"📌 Ruta hija: {ruta_toa}")

    # Usa la función ya probada del proyecto original para expandir la zona por texto.
    try:
        from toa_monitor_helpers import abrir_zona_padre_por_texto
        abrir_zona_padre_por_texto(page, zona_toa)
    except Exception as error:
        log(f"⚠️ No se pudo expandir zona por método principal: {error}")
        # Respaldo: intentar click directo en zona y luego ruta.
        _click_nodo_por_texto_monitor(page, zona_toa, "zona padre", timeout=5000)

    if _click_nodo_por_texto_monitor(page, ruta_toa, "ruta hija"):
        return

    raise RuntimeError(f"No se pudo seleccionar la ruta hija por texto: {ruta_toa}")

def procesar_actividad(page, actividad: Dict[str, Any], dataset: List[Dict[str, Any]], pids_toa: Dict[str, Any]) -> None:
    id_sp = actividad["ID"]
    id_toa = actividad["ID_TOA"]
    ruta_toa_completa = actividad.get("RutaTOACompleta")
    inicio_sla = actividad.get("InicioSLA")

    if not ruta_toa_completa:
        log(f"⚠️ ID SharePoint {id_sp}: sin RutaTOACompleta. Se omite.")
        return

    if not inicio_sla:
        log(f"⚠️ ID SharePoint {id_sp}: sin InicioSLA. Se omite.")
        return

    log("=" * 70)
    log(f"🚦 Monitoreando SharePoint ID {id_sp} / TOA ID {id_toa}")

    # Se selecciona Macrozona padre y Ruta hija usando los PIDs extraídos desde el Excel.
    # Esto evita depender del texto visible del árbol de TOA.
    filtrar_providerid_por_ruta(dataset, ruta_toa_completa)
    try:
        seleccionar_zona_y_ruta_toa(page, pids_toa, ruta_toa_completa)
    except Exception as error:
        log(f"⚠️ Falló selección por PIDs del Excel: {error}")
        log("↩️ Respaldo: intentando seleccionar zona/ruta por texto.")
        seleccionar_zona_y_ruta_toa_sin_mapping(page, ruta_toa_completa)

    page.wait_for_timeout(1500)
    dias_delta = seleccionar_fecha_cita_monitor(page, inicio_sla)
    aplicar_vista_despues_de_cambiar_fecha(page, dias_delta)

    try:
        # Modo anterior: solo abrir/ingresar y detenerse sin actualizar.
        if MONITOR_SOLO_ABRIR_CITA:
            abierta = abrir_cita_por_aid_visible_o_scroll(page, id_toa)
            if abierta:
                mantener_navegador_abierto_hasta_ctrl_c()
            else:
                log(f"⚠️ No se pudo abrir la cita TOA ID {id_toa}. No se hace nada más.")
            return

        # Modo principal: primero busca en la grilla por las 2 columnas visibles:
        # Orden de venta/trabajo + Tipo de actividad. Luego ingresa al detalle,
        # valida que dentro esté el ID_TOA esperado y recién ahí extrae
        # Estado_actividad + Observacion_actividad para actualizar SharePoint.
        if MONITOR_EXTRAER_ESTADO_DESDE_DETALLE:
            orden_busqueda = actividad.get("OrdenVenta") or actividad.get("OrdenTrabajo")
            tipo_actividad = actividad.get("TipoActividad")
            estado_sp_actual = actividad.get("Estado_actividad_actual")

            if actividad.get("OrdenVenta"):
                log(f"📌 Orden de venta SharePoint para validación: {actividad.get('OrdenVenta')}")
            if actividad.get("OrdenTrabajoOriginal"):
                log(f"📌 Orden de trabajo/cliente SharePoint de respaldo: {actividad.get('OrdenTrabajoOriginal')}")
            if tipo_actividad:
                log(f"📌 Tipo de actividad SharePoint para validación inicial: {tipo_actividad}")

            resultado_validacion = buscar_ingresar_por_orden_estado_y_validar_id(
                page=page,
                id_toa=id_toa,
                orden_sharepoint=orden_busqueda,
                estado_sharepoint=estado_sp_actual,
                tipo_sharepoint=tipo_actividad,
            )

            # Importante: en este flujo NO se usa ID_TOA/AID como respaldo para encontrar la cita.
            # La actividad se busca únicamente por Orden de venta/trabajo + Tipo de actividad en la grilla.
            # El ID_TOA se valida recién después de ingresar al detalle del candidato encontrado.
            if resultado_validacion.get("__datos_validacion_incompletos"):
                log("⚠️ No se marca ELIMINADO porque faltan datos de validación desde SharePoint.")
                return

            if resultado_validacion.get("__no_encontrada_marcar_eliminado"):
                marcar_sharepoint_como_eliminado(
                    actividad,
                    resultado_validacion.get("motivo") or "actividad no encontrada en TOA",
                )
                return

            if not resultado_validacion:
                marcar_sharepoint_como_eliminado(actividad, "actividad no encontrada en TOA")
                return

            estado = normalizar_estado(resultado_validacion.get("Estado_actividad"))
            observacion = resultado_validacion.get("Observacion_actividad")

            if not estado:
                log(f"⚠️ No se detectó Estado de actividad para TOA ID {id_toa}. No se actualiza SharePoint.")
                return

            actualizar_sharepoint_estado_actividad(
                id_sharepoint=id_sp,
                id_toa=id_toa,
                estado_actividad=estado,
                observacion_actividad=observacion or actividad.get("Observacion_actividad_actual"),
            )

            if detalle_actividad_parece_abierto(page):
                volver_a_consola_despacho(page)
                page.wait_for_timeout(1200)
            return

        # Lógica anterior completa por Orden/Tipo + resumen, disponible como respaldo.
        orden_trabajo = actividad.get("OrdenTrabajo")
        tipo_actividad = actividad.get("TipoActividad")

        resultado = buscar_actividad_por_orden_tipo_en_grilla(
            page=page,
            id_toa=id_toa,
            orden_trabajo=orden_trabajo,
            tipo_actividad=tipo_actividad,
        )

        # Fallback: si SharePoint aún no devuelve Orden/Tipo, mantiene búsqueda por AID.
        if not resultado and (not orden_trabajo or not tipo_actividad):
            log("↩️ Fallback: buscando directo por ID_TOA/AID porque faltan OrdenTrabajo o TipoActividad.")
            estado_directo = buscar_estado_actividad_en_grilla(page, id_toa)
            observacion_directa = obtener_observacion_actividad(page, id_toa)
            resultado = {
                "Estado_actividad": estado_directo,
                "Observacion_actividad": observacion_directa,
            }

        estado = resultado.get("Estado_actividad") if resultado else None
        observacion = resultado.get("Observacion_actividad") if resultado else None

        if not estado and observacion is None:
            log(f"⚠️ No se detectaron datos para TOA ID {id_toa}. No se actualiza SharePoint.")
            return

        actualizar_sharepoint_estado_actividad(
            id_sharepoint=id_sp,
            id_toa=id_toa,
            estado_actividad=estado,
            observacion_actividad=observacion,
        )
    finally:
        # Si quedó dentro del detalle, vuelve a consola antes de cambiar/restablecer fecha.
        try:
            if detalle_actividad_parece_abierto(page):
                volver_a_consola_despacho(page)
        except Exception:
            pass

        # Regresar a fecha actual para que la siguiente iteración parta limpia.
        # Esto evita que el siguiente registro se procese desde una fecha relativa incorrecta.
        if MONITOR_VOLVER_FECHA_ACTUAL_AL_FINAL:
            try:
                restablecer_fecha_monitor(page, dias_delta)
            except Exception as error:
                log(f"⚠️ No se pudo restablecer fecha: {error}")
        else:
            log("ℹ️ MONITOR_VOLVER_FECHA_ACTUAL_AL_FINAL=false: no se restablece la fecha actual.")

def ejecutar_monitor_una_vuelta() -> None:
    preparar_sesion_si_no_existe()

    actividades = obtener_actividades_a_monitorear()
    if not actividades:
        log("✅ No hay actividades creadas para monitorear en esta vuelta.")
        return

    log("📥 Cargando PIDs de macrozona padre y ruta hija desde Excel...")
    dataset = obtener_inputexcel()
    pids_toa = cargar_pids_desde_power_automate(dataset)
    log(f"✅ PIDs cargados desde Excel: {len(dataset)} rutas")

    with sync_playwright() as p:
        headless_efectivo = False if MONITOR_SOLO_ABRIR_CITA else MONITOR_HEADLESS
        if MONITOR_SOLO_ABRIR_CITA and MONITOR_HEADLESS:
            log("👀 MONITOR_SOLO_ABRIR_CITA=true: se fuerza navegador visible aunque MONITOR_HEADLESS esté en true.")
        browser = p.chromium.launch(headless=headless_efectivo)
        context = browser.new_context(storage_state=STATE_FILE, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(15000)

        try:
            abrir_pagina_inicial(page, context)
            reingresar_password_toa_si_aparece(page)

            for actividad in actividades:
                try:
                    procesar_actividad(page, actividad, dataset, pids_toa)
                    if MONITOR_REABRIR_TOA_CADA_ACTIVIDAD:
                        abrir_pagina_inicial(page, context)
                        reingresar_password_toa_si_aparece(page)
                except Exception as error:
                    log(f"❌ Error monitoreando SP ID {actividad.get('ID')} / TOA {actividad.get('ID_TOA')}: {error}")

        finally:
            context.close()
            browser.close()


def ejecutar_monitor_constante() -> None:
    log(f"▶️ Monitor iniciado. Intervalo: {MONITOR_INTERVAL_SECONDS} segundos. Headless: {MONITOR_HEADLESS}. Solo abrir cita: {MONITOR_SOLO_ABRIR_CITA}. Ingresar detalle: {MONITOR_INGRESAR_A_DETALLE_CITA}. Extraer estado detalle: {MONITOR_EXTRAER_ESTADO_DESDE_DETALLE}")

    while True:
        try:
            ejecutar_monitor_una_vuelta()
        except KeyboardInterrupt:
            log("🛑 Monitor detenido manualmente.")
            break
        except Exception as error:
            log(f"❌ Error general del monitor: {error}")

        import time

        log(f"⏳ Esperando {MONITOR_INTERVAL_SECONDS} segundos para la próxima revisión...")
        time.sleep(MONITOR_INTERVAL_SECONDS)


if __name__ == "__main__":
    ejecutar_monitor_constante()
