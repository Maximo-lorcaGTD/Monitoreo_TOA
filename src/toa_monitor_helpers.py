"""
Helpers mínimos para el monitor TOA.
Este archivo reemplaza las dependencias del bot de agendamiento para que el monitor
pueda vivir en un repositorio aparte.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

load_dotenv()

# ============================================================
# CONFIGURACIÓN BASE
# ============================================================

TOA_URL = os.getenv("TOA_URL") or os.getenv("TOA_BASE_URL", "https://gtd-zcn.etadirect.com/")
TOA_USER = os.getenv("TOA_USER")
TOA_PASSWORD = os.getenv("TOA_PASSWORD")
POWER_AUTOMATE_EXCEL_URL = os.getenv("POWER_AUTOMATE_EXCEL_URL")
POWER_AUTOMATE_MAPPING_URL = os.getenv("POWER_AUTOMATE_MAPPING_URL") or POWER_AUTOMATE_EXCEL_URL
STATE_FILE = os.getenv("PLAYWRIGHT_STATE_FILE") or str(Path(__file__).with_name("state.json"))

URL_INICIAL = TOA_URL
DEBUG_SCREENSHOTS = False
ESPERA_CLICK = 0.1

SELECTOR_USUARIO_TOA = (
    "#username, "
    'input[name="username"], '
    'input[type="text"][autocomplete="username"], '
    'input[type="email"], '
    'input[aria-label*="Usuario"], '
    'input[placeholder*="Usuario"], '
    'input[aria-label*="User"], '
    'input[placeholder*="User"]'
)

SELECTOR_PASSWORD_TOA = (
    "#password, "
    'input#id_index_2[type="password"], '
    'input[type="password"][aria-label*="Contraseña"], '
    'input[type="password"][aria-label*="Password"], '
    'input[type="password"][data-label="restore"], '
    'input.form-item[type="password"], '
    'input[type="password"]'
)

SELECTOR_BOTON_LOGIN_TOA = (
    "#sign-in, "
    'button:visible:has-text("Ingresar"), '
    'button:visible:has-text("Iniciar sesión"), '
    'button:visible:has-text("Sign in"), '
    'button:visible:has-text("Continuar"), '
    'input[type="submit"]:visible'
)

SELECTOR_CERRAR_SESION_TOA = (
    'button.dismiss:visible:has-text("Cerrar sesión"), '
    'button[history-direction="back"]:visible:has-text("Cerrar sesión"), '
    'button:visible:has-text("Cerrar sesión")'
)

# ============================================================
# UTILIDADES GENERALES
# ============================================================


def esperar_pagina(page, segundos: float = ESPERA_CLICK) -> None:
    page.wait_for_timeout(int(segundos * 1000))


def esperar_carga(page, timeout: int = 30000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        print("⚠️ La página no llegó a networkidle, se continuará.")


def tomar_captura(page, nombre_archivo: str) -> None:
    if DEBUG_SCREENSHOTS:
        page.screenshot(path=nombre_archivo, full_page=True)
        print(f"📸 Captura guardada: {nombre_archivo}")


def normalizar_texto(valor: Any) -> str:
    if valor is None:
        return ""

    texto = str(valor).strip().upper()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.replace("MACROZONA", "MACRO ZONA")
    texto = texto.replace("CONTRUCCION", "CONSTRUCCION")
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def convertir_fecha_chile(valor: Any):
    """
    Convierte una fecha recibida desde SharePoint/Power Automate a America/Santiago.
    Acepta ISO con Z, ISO normal y dd/mm/yy HH:MM.
    """
    if not valor:
        return None

    texto = str(valor).replace("Z", "+00:00")

    try:
        fecha = datetime.fromisoformat(texto)
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=ZoneInfo("UTC"))
        return fecha.astimezone(ZoneInfo("America/Santiago"))
    except Exception:
        fecha = datetime.strptime(str(valor), "%d/%m/%y %H:%M")
        return fecha.replace(tzinfo=ZoneInfo("America/Santiago"))

# ============================================================
# LOGIN / SESIÓN
# ============================================================


def limpiar_estado_sesion(context=None) -> None:
    try:
        if context:
            context.clear_cookies()
            print("🧹 Cookies del contexto eliminadas.")
    except Exception as e:
        print(f"⚠️ No se pudieron limpiar cookies: {e}")

    try:
        archivo_estado = Path(STATE_FILE)
        if archivo_estado.exists():
            archivo_estado.unlink()
            print(f"🧹 Archivo de sesión eliminado: {STATE_FILE}")
    except Exception as e:
        print(f"⚠️ No se pudo eliminar state.json: {e}")


def guardar_diagnostico_login(page, motivo: str) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta_base = Path(__file__).with_name(f"login_debug_{timestamp}")

    try:
        page.screenshot(path=f"{ruta_base}.png", full_page=True)
        print(f"📸 Captura de diagnóstico guardada: {ruta_base}.png")
    except Exception as e:
        print(f"⚠️ No se pudo guardar captura de diagnóstico: {e}")

    try:
        inputs = page.locator("input").evaluate_all(
            """els => els.map((el, index) => ({
                index,
                type: el.type || "",
                id: el.id || "",
                name: el.name || "",
                placeholder: el.placeholder || "",
                ariaLabel: el.getAttribute("aria-label") || "",
                autocomplete: el.autocomplete || "",
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            }))"""
        )
    except Exception as e:
        inputs = f"No se pudieron leer inputs: {e}"

    diagnostico = {"motivo": motivo, "url": page.url, "title": page.title(), "inputs": inputs}

    try:
        Path(f"{ruta_base}.json").write_text(
            json.dumps(diagnostico, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"🧾 Diagnóstico de login guardado: {ruta_base}.json")
    except Exception as e:
        print(f"⚠️ No se pudo guardar diagnóstico de login: {e}")


def pantalla_login_visible(page) -> bool:
    try:
        return (
            page.locator(SELECTOR_USUARIO_TOA).first.is_visible(timeout=1000)
            or page.locator(SELECTOR_PASSWORD_TOA).first.is_visible(timeout=1000)
        )
    except Exception:
        return False


def obtener_pagina_activa(context=None, page=None):
    try:
        if page and not page.is_closed():
            return page
    except Exception:
        pass

    try:
        if context:
            for p in reversed(context.pages):
                if not p.is_closed():
                    return p
    except Exception:
        pass

    return None


def cerrar_sesion_si_aparece(page=None, context=None, timeout: int = 3000) -> bool:
    page_activa = obtener_pagina_activa(context=context, page=page)
    if not page_activa:
        return False

    try:
        boton_cerrar = page_activa.locator(SELECTOR_CERRAR_SESION_TOA).first
        if boton_cerrar.is_visible(timeout=timeout):
            print("🚪 Se detectó botón 'Cerrar sesión'. Presionando...")
            boton_cerrar.scroll_into_view_if_needed(timeout=2000)
            boton_cerrar.click(force=True)
            page_activa.wait_for_timeout(2000)
            print("✅ Botón 'Cerrar sesión' presionado correctamente.")
            return True
    except Exception:
        pass

    return False


def esperar_login_completado(page, context=None, timeout: int = 120000):
    print("⏳ Esperando que TOA complete el inicio de sesión...")
    fin = datetime.now() + timedelta(milliseconds=timeout)

    while datetime.now() < fin:
        page_activa = obtener_pagina_activa(context=context, page=page)
        if not page_activa:
            return "PAGINA_CERRADA"

        if cerrar_sesion_si_aparece(page_activa, context=context, timeout=1000):
            print("🔁 TOA mostró 'Cerrar sesión'. Se limpiará la sesión y se reintentará login.")
            limpiar_estado_sesion(context)
            return "SESION_CERRADA"

        if not pantalla_login_visible(page_activa):
            esperar_carga(page_activa, 15000)
            if cerrar_sesion_si_aparece(page_activa, context=context, timeout=1000):
                limpiar_estado_sesion(context)
                return "SESION_CERRADA"
            if not pantalla_login_visible(page_activa):
                print("✅ Inicio de sesión detectado.")
                return "OK"

        page_activa.wait_for_timeout(1000)

    try:
        page_activa = obtener_pagina_activa(context=context, page=page)
        if page_activa:
            guardar_diagnostico_login(page_activa, "El login no terminó dentro del tiempo esperado")
    except Exception:
        pass

    raise RuntimeError(
        "El login no terminó dentro del tiempo esperado. "
        "Si había MFA, completa el acceso en el navegador."
    )


def iniciar_sesion_toa(page, context, reintento_cierre: int = 0):
    if not TOA_USER or not TOA_PASSWORD:
        raise ValueError("Faltan TOA_USER o TOA_PASSWORD en el archivo .env")

    try:
        print("👤 Validando estado de sesión TOA...")
        esperar_carga(page, 15000)
        esperar_pagina(page, 2)

        try:
            usuario_visible = page.locator(SELECTOR_USUARIO_TOA).first.is_visible(timeout=3000)
        except Exception:
            usuario_visible = False

        try:
            password_visible = page.locator(SELECTOR_PASSWORD_TOA).first.is_visible(timeout=3000)
        except Exception:
            password_visible = False

        if not usuario_visible and not password_visible:
            print("✅ TOA ya está con sesión activa. No se reintentará login.")
            context.storage_state(path=STATE_FILE)
            return "OK"

        print("👤 Detectada necesidad de inicio de sesión. Ingresando credenciales...")

        campo_usuario = page.locator(SELECTOR_USUARIO_TOA).first
        campo_password = page.locator(SELECTOR_PASSWORD_TOA).first

        try:
            campo_usuario.wait_for(state="visible", timeout=15000)
            print("🔑 Campo de usuario detectado.")
            campo_usuario.fill(TOA_USER)
        except PlaywrightTimeoutError:
            try:
                if campo_password.is_visible(timeout=3000):
                    print("🔐 TOA no pidió usuario; solo está solicitando contraseña.")
                else:
                    print("✅ No apareció usuario ni contraseña. Se asume sesión activa.")
                    context.storage_state(path=STATE_FILE)
                    return "OK"
            except Exception:
                context.storage_state(path=STATE_FILE)
                return "OK"

        campo_password.wait_for(state="visible", timeout=10000)
        campo_password.fill(TOA_PASSWORD)

        boton_login = page.locator(SELECTOR_BOTON_LOGIN_TOA).first
        if boton_login.is_visible(timeout=3000):
            boton_login.click()
        else:
            campo_password.press("Enter")

        esperar_carga(page, 15000)

        try:
            checkbox_del = page.locator('input#delsession')
            if checkbox_del.is_visible(timeout=5000):
                print("🔄 Manejando conflicto de sesión antigua detectada...")
                checkbox_del.click()
                page.locator(SELECTOR_PASSWORD_TOA).first.fill(TOA_PASSWORD)
                page.locator(SELECTOR_BOTON_LOGIN_TOA).first.click()
                esperar_carga(page, 15000)
        except Exception:
            pass

        print("\n🔐 Si aparece MFA o Microsoft Login, complétalo en la ventana del navegador.")
        resultado_login = esperar_login_completado(page, context)

        if resultado_login == "OK":
            print("✅ Login completado correctamente. Guardando sesión...")
            context.storage_state(path=STATE_FILE)
            print(f"✅ Sesión guardada correctamente en: {Path(STATE_FILE).resolve()}")
            return "OK"

        if resultado_login in ["SESION_CERRADA", "PAGINA_CERRADA"]:
            if reintento_cierre >= 2:
                raise RuntimeError("TOA pidió cerrar sesión varias veces. Se detuvo el proceso.")

            print("🔁 Reintentando inicio de sesión después de limpiar sesión...")
            page_activa = obtener_pagina_activa(context=context, page=page) or context.new_page()
            page_activa.goto(URL_INICIAL, wait_until="domcontentloaded", timeout=60000)
            esperar_carga(page_activa, 30000)
            return iniciar_sesion_toa(page_activa, context, reintento_cierre=reintento_cierre + 1)

        raise RuntimeError(f"Resultado de login no reconocido: {resultado_login}")

    except Exception as e:
        print(f"❌ Error durante el inicio de sesión: {e}")
        try:
            cerrar_sesion_si_aparece(page, context=context, timeout=5000)
        except Exception:
            pass
        raise


def abrir_pagina_inicial(page, context=None) -> None:
    print("🌐 Abriendo aplicación TOA...")
    page.goto(URL_INICIAL, wait_until="domcontentloaded", timeout=60000)
    esperar_carga(page, 30000)

    try:
        necesita_login = (
            page.locator(SELECTOR_USUARIO_TOA).first.is_visible(timeout=5000)
            or page.locator(SELECTOR_PASSWORD_TOA).first.is_visible(timeout=1000)
        )
    except Exception:
        necesita_login = False

    if necesita_login:
        if context:
            iniciar_sesion_toa(page, context)
        else:
            print("⚠️ Se requiere inicio de sesión pero no se proporcionó contexto.")
    else:
        print("✅ Página inicial cargada / sesión activa.")


def reingresar_password_toa_si_aparece(page) -> None:
    print("🔐 Validando si TOA solicita contraseña...")

    try:
        campo_password = page.locator(SELECTOR_PASSWORD_TOA).first
        campo_password.wait_for(state="visible", timeout=8000)

        if not TOA_PASSWORD:
            raise ValueError("Falta configurar TOA_PASSWORD en el archivo .env")

        print("🔑 Campo de contraseña detectado. Ingresando clave...")
        campo_password.fill(TOA_PASSWORD)
        esperar_pagina(page, 0.1)
        campo_password.press("Enter")
        esperar_pagina(page, 2)

        botones = page.locator(
            'button:visible:has-text("Ingresar"), '
            'button:visible:has-text("Iniciar sesión"), '
            'button:visible:has-text("Continuar"), '
            'button:visible:has-text("Aceptar"), '
            'input[type="submit"]:visible'
        )

        if botones.count() > 0:
            try:
                botones.first.click(timeout=5000)
                print("✅ Botón de ingreso presionado.")
            except Exception:
                print("⚠️ No fue necesario presionar botón adicional.")

        esperar_carga(page, 30000)
        print("✅ Contraseña TOA ingresada correctamente.")

    except PlaywrightTimeoutError:
        print("✅ No se solicitó contraseña TOA.")

# ============================================================
# MAPPING RUTA / PROVIDERID DESDE EXCEL
# ============================================================


def _valor_por_alias(fila: Dict[str, Any], aliases: List[str]) -> Any:
    """Busca un valor en la fila aceptando diferencias de mayúsculas, espacios y guiones."""
    if not isinstance(fila, dict):
        return None

    def norm_key(valor: Any) -> str:
        texto = str(valor or "").strip().lower()
        texto = unicodedata.normalize("NFKD", texto)
        texto = "".join(c for c in texto if not unicodedata.combining(c))
        texto = re.sub(r"[^a-z0-9]", "", texto)
        return texto

    mapa = {norm_key(k): v for k, v in fila.items()}
    for alias in aliases:
        valor = mapa.get(norm_key(alias))
        if valor not in [None, ""]:
            return valor
    return None


def _limpiar_pid(valor: Any) -> str:
    if valor is None:
        return ""
    texto = str(valor).strip()
    if texto.endswith(".0") and texto[:-2].isdigit():
        texto = texto[:-2]
    return texto


def _extraer_pid_desde_url(url: Any) -> str:
    if not url:
        return ""
    texto = str(url)
    patrones = [
        r"(?:providerid|provider_id|pid|resourceId|resource_id)=([0-9]+)",
        r"/providers?/([0-9]+)",
        r"provider/([0-9]+)",
    ]
    for patron in patrones:
        m = re.search(patron, texto, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def construir_ruta_toa_desde_fila(fila: Dict[str, Any]):
    ruta_directa = _valor_por_alias(fila, [
        "RutaTOACompleta", "Ruta TOA Completa", "rutaTOACompleta", "Ruta TOA", "RutaCompleta"
    ])
    if ruta_directa:
        return str(ruta_directa).strip()

    subgrupo = _valor_por_alias(fila, [
        "Subgrupo TOA", "SubgrupoTOA", "subgrupoTOA", "Ruta Hija", "RutaHija", "Hija", "Zona hija"
    ])
    zona = _valor_por_alias(fila, [
        "Zona Subgrupo TOA", "ZonaSubgrupoTOA", "zonaSubgrupoTOA", "Macrozona", "Macro Zona", "Zona Padre", "Padre"
    ])
    if subgrupo and zona:
        return f"{str(zona).strip()} | {str(subgrupo).strip()}"
    if subgrupo:
        return f"| {str(subgrupo).strip()}"
    return None



def obtener_inputexcel() -> List[Dict[str, str]]:
    """
    Método original del bot funcional para leer el Excel de rutas.

    Importante:
    - Usa POWER_AUTOMATE_MAPPING_URL o POWER_AUTOMATE_EXCEL_URL.
    - Del Excel solo se necesita RutaTOACompleta y providerid.
    - El providerid corresponde a la ruta hija.
    """
    url_flujo = POWER_AUTOMATE_MAPPING_URL or POWER_AUTOMATE_EXCEL_URL
    if not url_flujo:
        raise ValueError("Falta POWER_AUTOMATE_EXCEL_URL o POWER_AUTOMATE_MAPPING_URL en .env")

    response = requests.post(
        url_flujo,
        headers={"Content-Type": "application/json"},
        json={"dataset": ""},
        timeout=180,
    )

    print("Status mapping:", response.status_code)
    print("Respuesta mapping:", response.text)

    if response.status_code != 200:
        raise RuntimeError(
            f"Power Automate Mapping falló. "
            f"Status: {response.status_code}. "
            f"Respuesta: {response.text}"
        )

    data = response.json()

    if isinstance(data, dict) and "body" in data and isinstance(data["body"], dict) and "value" in data["body"]:
        registros = data["body"]["value"]
    elif isinstance(data, dict) and "value" in data:
        registros = data["value"]
    elif isinstance(data, list):
        registros = data
    else:
        raise ValueError("El flujo no devolvió body/value válido.")

    dataset = []

    for item in registros:
        ruta_toa_completa = construir_ruta_toa_desde_fila(item)

        providerid = (
            _valor_por_alias(item, ["providerid", "providerId", "ProviderID", "ProviderId", "Provider ID"])
            or _valor_por_alias(item, ["PID", "Pid", "pid"])
            or _valor_por_alias(item, ["PID Ruta Hija", "PIDRutaHija", "PID_Hija", "PID Hija"])
        )

        url = (
            _valor_por_alias(item, ["URL", "URL_concat", "url", "Link", "Enlace"])
        )

        if not providerid:
            providerid = _extraer_pid_desde_url(url)

        providerid = _limpiar_pid(providerid)

        if ruta_toa_completa and providerid:
            dataset.append({
                "RutaTOACompleta": ruta_toa_completa,
                "RutaNormalizada": normalizar_texto(ruta_toa_completa),
                "providerid": str(providerid).strip(),
                "URL": str(url).strip() if url else "",
            })

    print("✅ Dataset Power Automate normalizado:")
    for item in dataset:
        print(f"   - {item['RutaTOACompleta']} => {item['providerid']}")

    return dataset


def filtrar_providerid_por_ruta(dataset: List[Dict[str, str]], ruta_toa_completa: str) -> str:
    ruta_buscada = normalizar_texto(ruta_toa_completa)

    for item in dataset:
        if item["RutaNormalizada"] == ruta_buscada:
            return item["providerid"]

    raise ValueError(
        f"No se encontró providerid para RutaTOACompleta: {ruta_toa_completa}. "
        f"Rutas disponibles: {[i['RutaTOACompleta'] for i in dataset]}"
    )


def cargar_pids_desde_power_automate(dataset: List[Dict[str, str]]) -> Dict[str, str]:
    pids = {}

    for item in dataset:
        pids[item["RutaNormalizada"]] = item["providerid"]

    return pids


def obtener_pid_por_ruta_completa(pids_toa: Dict[str, str], ruta_completa: str) -> str:
    ruta_normalizada = normalizar_texto(ruta_completa)

    pid = pids_toa.get(ruta_normalizada)

    if not pid:
        raise ValueError(
            f"No existe PID para RutaTOACompleta '{ruta_completa}'. "
            f"Rutas disponibles: {list(pids_toa.keys())}"
        )

    return pid


def separar_zona_y_ruta(ruta_completa: str):
    if not ruta_completa:
        raise ValueError("RutaTOACompleta viene vacía desde SharePoint.")

    partes = str(ruta_completa).split("|")

    if len(partes) != 2:
        raise ValueError(
            f"Formato inválido en RutaTOACompleta: {ruta_completa}. "
            "Debe venir como: MACROZONA NORTE | CENTRO"
        )

    zona = partes[0].strip()
    ruta = partes[1].strip()

    return zona, ruta


# ============================================================
# NAVEGACIÓN ÁRBOL TOA - MÉTODO ORIGINAL DEL BOT FUNCIONAL
# ============================================================


def abrir_zona_padre_por_texto(page, zona_toa: str, timeout: int = 10000) -> bool:
    """
    Expande una zona padre en el árbol del panel izquierdo de TOA.

    Método copiado del bot original funcional:
    - Busca la macrozona padre por texto.
    - Expande con button.ptplus del mismo contenedor.
    - Usa estrategias de respaldo por hermano, XPath directo y coordenadas.
    """
    print(f"📂 Buscando zona padre exacta para expandir: {zona_toa}")

    posibles_textos = [
        zona_toa,
        normalizar_texto(zona_toa).replace("MACRO ZONA", "MACROZONA"),
        normalizar_texto(zona_toa),
        zona_toa.upper(),
        zona_toa.title(),
    ]

    # Eliminar duplicados manteniendo orden
    posibles_textos = list(dict.fromkeys(posibles_textos))

    for texto in posibles_textos:
        print(f"   🔍 Intentando con texto: '{texto}'")

        # ── ESTRATEGIA 1 (principal): Localizar button.edt-label con el texto
        #    y luego buscar button.ptplus dentro del mismo contenedor edt-item ──
        try:
            texto_zona = page.locator(
                f'button.edt-label:text-is("{texto}")'
            ).first

            texto_zona.wait_for(state="visible", timeout=timeout)
            texto_zona.scroll_into_view_if_needed(timeout=3000)
            print(f"   ✅ Texto '{texto}' encontrado en button.edt-label")

            contenedor = texto_zona.locator(
                'xpath=ancestor::div[contains(@class,"edt-item")][1]'
            )

            boton_expandir = contenedor.locator('button.edt-open.icr.ptplus').first
            boton_expandir.wait_for(state="visible", timeout=5000)
            boton_expandir.click(force=True)

            esperar_pagina(page, 1)
            print(f"✅ Zona padre expandida (estrategia 1 - edt-label + edt-item): {texto}")
            return True

        except Exception as e:
            print(f"   ⚠️ Estrategia 1 falló: {e}")

        # ── ESTRATEGIA 2: Buscar el texto en cualquier tipo de elemento
        #    y navegar al hermano button.ptplus ──
        try:
            texto_zona = page.locator(
                f'button:text-is("{texto}"), '
                f'span:text-is("{texto}"), '
                f'div:text-is("{texto}"), '
                f'a:text-is("{texto}")'
            ).first

            texto_zona.wait_for(state="visible", timeout=5000)
            texto_zona.scroll_into_view_if_needed(timeout=3000)
            print(f"   ✅ Texto '{texto}' encontrado en el DOM (estrategia 2)")

            selectores_hermano = [
                'xpath=preceding-sibling::button[contains(@class,"ptplus")][1]',
                'xpath=../button[contains(@class,"ptplus")]',
                'xpath=../button[contains(@class,"edt-open")]',
                'xpath=preceding-sibling::button[contains(@aria-label,"Ampliar")][1]',
                'xpath=ancestor::div[1]//button[contains(@class,"ptplus")]',
            ]

            for sel_h in selectores_hermano:
                try:
                    boton = texto_zona.locator(sel_h).first
                    boton.wait_for(state="visible", timeout=2000)
                    boton.click(force=True)
                    esperar_pagina(page, 1)
                    print(f"✅ Zona padre expandida (estrategia 2 - hermano): {texto}")
                    return True
                except Exception:
                    continue

        except Exception as e:
            print(f"   ⚠️ Estrategia 2 falló: {e}")

        # ── ESTRATEGIA 3: XPath directo combinando texto y botón ptplus ──
        try:
            xpath_directo = (
                f'//button[contains(@class,"edt-label") and normalize-space()="{texto}"]'
                f'/ancestor::div[contains(@class,"edt-item")][1]'
                f'//button[contains(@class,"ptplus")]'
            )

            boton = page.locator(xpath_directo).first
            boton.wait_for(state="visible", timeout=5000)
            boton.click(force=True)

            esperar_pagina(page, 1)
            print(f"✅ Zona padre expandida (estrategia 3 - XPath directo): {texto}")
            return True

        except Exception as e:
            print(f"   ⚠️ Estrategia 3 falló: {e}")

        # ── ESTRATEGIA 4: Click por coordenadas a la izquierda del texto ──
        try:
            texto_zona = page.locator(
                f'button.edt-label:text-is("{texto}"), '
                f'button:text-is("{texto}"), '
                f'span:text-is("{texto}")'
            ).first

            texto_zona.wait_for(state="visible", timeout=3000)
            box = texto_zona.bounding_box()

            if box:
                for offset_x in [20, 30, 40, 50, 60]:
                    click_x = box["x"] - offset_x
                    click_y = box["y"] + box["height"] / 2

                    if click_x > 0:
                        page.mouse.click(click_x, click_y)
                        esperar_pagina(page, 1)
                        print(f"   🖱️ Click en ({click_x:.0f}, {click_y:.0f}) offset -{offset_x}px")

                        try:
                            page.locator('button.ptminus:visible').first.wait_for(
                                state="visible", timeout=2000
                            )
                            print(f"✅ Zona padre expandida (estrategia 4 - coordenadas): {texto}")
                            return True
                        except Exception:
                            continue
        except Exception as e:
            print(f"   ⚠️ Estrategia 4 falló: {e}")

    try:
        html_panel = page.locator('[role="treeitem"], .edt-item').first.inner_html()
        print(f"🔍 HTML del primer treeitem:\n{html_panel[:1500]}")
    except Exception:
        print("⚠️ No se pudo obtener HTML del panel para depuración")

    tomar_captura(page, f"error_expandir_zona_padre_{zona_toa}.png")
    raise RuntimeError(f"No se pudo expandir la zona padre exacta: {zona_toa}")


def click_pid_toa(page, pid: str, descripcion: str, expandir: bool = False, timeout: int = 10000) -> bool:
    print(f"🔎 Buscando {descripcion} / PID {pid}")

    pid = str(pid).strip()

    if expandir:
        selectores = [
            f'button.edt-open.icr.ptplus[aria-label="Ampliar_{pid}"]',
            f'button[aria-label="Ampliar_{pid}"]',
            f'button[aria-label*="Ampliar_{pid}"]',
            f'button[aria-label*="{pid}"]',
        ]
    else:
        selectores = [
            f'[data-label-pid="{pid}"]',
            f'[data-label-pid="{pid}"] span',
            f'text="{descripcion}"',
        ]

    for selector in selectores:
        try:
            elemento = page.locator(selector).first
            elemento.wait_for(state="visible", timeout=timeout)

            try:
                elemento.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass

            elemento.click(force=True)
            esperar_pagina(page, 0.3)

            print(f"✅ Click realizado en {descripcion} / PID {pid}")
            return True

        except Exception:
            continue

    raise RuntimeError(f"No se pudo hacer click en {descripcion} / PID {pid}")


def seleccionar_zona_y_ruta_toa(page, pids_toa: Dict[str, str], ruta_completa: str) -> None:
    ruta_limpia = str(ruta_completa).strip()

    pid_ruta = obtener_pid_por_ruta_completa(pids_toa, ruta_limpia)

    print("🧭 Seleccionando zona y ruta TOA")

    # ── Sin zona padre: Excel genera "| ANTOFAGASTA" (empieza con "| ") ──
    if ruta_limpia.startswith("| "):
        nombre_nodo = ruta_limpia[2:].strip()
        print(f"🏙️ Sin zona padre — click directo en: {nombre_nodo} (PID: {pid_ruta})")

        click_pid_toa(
            page,
            pid_ruta,
            nombre_nodo,
            expandir=False,
        )

        print("✅ Nodo seleccionado directamente por PID.")
        return

    # ── Con zona padre: "MACROZONA NORTE | CENTRO" ──
    zona_toa, ruta_toa = separar_zona_y_ruta(ruta_limpia)

    print(f"📌 Zona padre: {zona_toa}")
    print(f"📌 Ruta hija: {ruta_toa}")
    print(f"📌 PID ruta hija: {pid_ruta}")

    abrir_zona_padre_por_texto(page, zona_toa)

    click_pid_toa(
        page,
        pid_ruta,
        ruta_toa,
        expandir=False,
    )

    print("✅ Zona padre expandida y ruta hija seleccionada.")
