"""
Utilidades mínimas para normalizar respuestas de Power Automate/SharePoint.
Repositorio independiente del monitor TOA.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional


def limpiar_valor(valor: Any) -> str:
    """Convierte valores de Power Automate/SharePoint a texto simple."""
    if valor is None:
        return ""

    # Columnas Choice / Lookup de SharePoint suelen venir como diccionario.
    if isinstance(valor, dict):
        for clave in ("Value", "value", "DisplayName", "displayName", "Label", "label", "Name", "name", "Title", "title"):
            if clave in valor and valor[clave] not in [None, ""]:
                return limpiar_valor(valor[clave])
        return ""

    if isinstance(valor, list):
        return ", ".join(limpiar_valor(v) for v in valor if limpiar_valor(v))

    return str(valor).strip()


def obtener_valor(item: Dict[str, Any], *nombres_posibles: str) -> Optional[Any]:
    """Obtiene un campo tolerando nombres internos, nombres visibles y Choice/Value."""
    if not isinstance(item, dict):
        return None

    # Coincidencia directa.
    for nombre in nombres_posibles:
        if nombre in item:
            valor = item.get(nombre)
            if valor not in [None, ""]:
                return valor

    # Coincidencia case-insensitive.
    mapa_lower = {str(k).lower(): k for k in item.keys()}
    for nombre in nombres_posibles:
        key = mapa_lower.get(str(nombre).lower())
        if key is not None:
            valor = item.get(key)
            if valor not in [None, ""]:
                return valor

    # Soporte para campos tipo Choice indicados como Campo/Value.
    for nombre in nombres_posibles:
        if "/Value" in nombre:
            base = nombre.split("/Value", 1)[0]
            valor = item.get(base)
            if isinstance(valor, dict):
                return valor.get("Value") or valor.get("value")

    return None


def _parsear_json_si_es_string(valor: Any) -> Any:
    """
    Power Automate a veces devuelve el body como string JSON:
      {"body":"[{...}]"}
    o incluso doblemente codificado. Esta función lo normaliza.
    """
    actual = valor
    for _ in range(3):
        if not isinstance(actual, str):
            return actual
        texto = actual.strip()
        if not texto:
            return actual
        if not ((texto.startswith("{") and texto.endswith("}")) or (texto.startswith("[") and texto.endswith("]"))):
            return actual
        try:
            actual = json.loads(texto)
        except Exception:
            return valor
    return actual


def _lista_dicts(valor: Any) -> Optional[List[Dict[str, Any]]]:
    valor = _parsear_json_si_es_string(valor)
    if isinstance(valor, list):
        registros = [x for x in valor if isinstance(x, dict)]
        if registros:
            return registros
    return None


def _buscar_lista_dicts_profundo(valor: Any, profundidad: int = 0) -> Optional[List[Dict[str, Any]]]:
    """Respaldo: busca una lista de objetos en respuestas anidadas de Power Automate."""
    if profundidad > 5:
        return None

    valor = _parsear_json_si_es_string(valor)
    lista = _lista_dicts(valor)
    if lista:
        return lista

    if isinstance(valor, dict):
        # Priorizar nombres típicos antes de recorrer todo.
        for clave in ("value", "body", "registros", "items", "data", "result", "results", "outputs"):
            if clave in valor:
                encontrado = _buscar_lista_dicts_profundo(valor.get(clave), profundidad + 1)
                if encontrado:
                    return encontrado
        for v in valor.values():
            encontrado = _buscar_lista_dicts_profundo(v, profundidad + 1)
            if encontrado:
                return encontrado

    return None


def extraer_registros(respuesta: Any) -> List[Dict[str, Any]]:
    """
    Normaliza distintas formas de respuesta de Power Automate:
    - lista directa
    - string JSON con lista
    - {"value": [...]}
    - {"body": "[...]"}
    - {"body": {"value": [...]}}
    - {"body": [...]}
    - {"d": {"results": [...]}}
    - respuestas anidadas dentro de outputs/body/data
    """
    if respuesta is None:
        return []

    respuesta = _parsear_json_si_es_string(respuesta)

    lista = _lista_dicts(respuesta)
    if lista:
        return lista

    if not isinstance(respuesta, dict):
        return []

    candidatos = [
        respuesta.get("value"),
        respuesta.get("registros"),
        respuesta.get("items"),
        respuesta.get("data"),
        respuesta.get("result"),
        respuesta.get("results"),
    ]

    body = respuesta.get("body")
    body = _parsear_json_si_es_string(body)
    if isinstance(body, list):
        candidatos.append(body)
    elif isinstance(body, dict):
        candidatos.extend([
            body.get("value"),
            body.get("registros"),
            body.get("items"),
            body.get("data"),
            body.get("result"),
            body.get("results"),
        ])

    d = respuesta.get("d")
    if isinstance(d, dict):
        candidatos.append(d.get("results"))

    for candidato in candidatos:
        lista = _lista_dicts(candidato)
        if lista:
            return lista

    # Respaldo para respuestas anidadas no estándar.
    lista = _buscar_lista_dicts_profundo(respuesta)
    if lista:
        return lista

    # Si viene un solo objeto de SharePoint, devolverlo como lista de uno.
    if "ID" in respuesta or "Id" in respuesta or "id" in respuesta:
        return [respuesta]

    return []
