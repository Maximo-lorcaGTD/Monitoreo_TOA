# Monitor TOA -> SharePoint

Esta versión ingresa primero por coincidencia en grilla usando **Orden de venta/trabajo + Estado de actividad**. Solo después de entrar valida el **ID_TOA** dentro del detalle y actualiza SharePoint.

Repositorio independiente para monitorear actividades ya creadas en Oracle Field Service / TOA y actualizar el campo `Estado_actividad` en SharePoint.

## Qué hace

1. Consulta Power Automate para traer las citas creadas desde SharePoint.
2. Omite directamente las actividades que ya estén en `cancelada` o `finalizada`.
3. Entra a TOA.
4. Selecciona la ruta usando `RutaTOACompleta` desde SharePoint.
5. Avanza/retrocede a la fecha de la actividad.
6. Activa `Vista de lista` y aplica la vista jerárquica.
7. Busca la actividad revisando la casilla de actividad.
8. Valida `OrdenTrabajo`/`OrdenVenta` y `TipoActividad` contra SharePoint.
9. Confirma que el `aid` coincida con el `ID_TOA` esperado.
10. Ingresa al detalle de la actividad validada.
11. Extrae `Estado de actividad` desde `data-label="astatus"`.
12. Actualiza `Estado_actividad` en SharePoint.
13. Vuelve a `Consola de despacho` y continúa con la siguiente actividad.

## Importante

Esta versión **no usa `POWER_AUTOMATE_MAPPING_URL`**. La selección de zona/ruta se hace directamente por texto usando `RutaTOACompleta`.

Por eso, tu `.env` debe tener solo:

```env
POWER_AUTOMATE_MONITOR_URL=
POWER_AUTOMATE_UPDATE_ACTIVIDAD_URL=
```

No agregues `POWER_AUTOMATE_MAPPING_URL` en este repositorio.

## Instalación

```bash
pip install -r requirements.txt
playwright install chromium
```

## Ejecución

```bash
python src/monitor_estado_actividad.py
```

En Windows también puedes ejecutar:

```bat
ejecutar_monitor_estado.bat
```

## Variables principales

Copia `.env.example` como `.env` y completa tus valores.

```env
TOA_BASE_URL=https://gtd-zcn.etadirect.com/
TOA_USER=
TOA_PASSWORD=
POWER_AUTOMATE_MONITOR_URL=
POWER_AUTOMATE_UPDATE_ACTIVIDAD_URL=
MONITOR_INTERVAL_SECONDS=120
MONITOR_HEADLESS=false
MONITOR_IGNORAR_ESTADOS_ACTIVIDAD=cancelada,finalizada,eliminado
```

## Campos que debe devolver el flujo de listado

El flujo asociado a `POWER_AUTOMATE_MONITOR_URL` debe devolver, como mínimo:

```json
{
  "ID": 31,
  "ID_TOA": "666754",
  "RutaTOACompleta": "ZONA / RUTA",
  "InicioSLA": "2026-06-09T10:00:00Z",
  "OrdenTrabajo": "Orden del cliente#0001341563",
  "TipoActividad": "NRA",
  "Estado_Creacion": "Creada",
  "Estado_actividad": "pendiente"
}
```

Si tu lista usa `OrdenVenta` u `Orden de venta` en vez de `OrdenTrabajo`, el script también lo reconoce.


## Selección de Macrozona y Ruta por PIDs del Excel

Esta versión carga `POWER_AUTOMATE_EXCEL_URL` y usa los PIDs del Excel para seleccionar el árbol TOA:

- PID padre: expande la macrozona padre.
- PID hija/providerid: selecciona la ruta hija.

El flujo de Excel puede devolver columnas como `PID Padre`, `PID Macrozona`, `PID Ruta Hija`, `providerid`, `ProviderID` o equivalentes.


## Método de Macrozona padre / Ruta hija

Esta versión usa exactamente el método del bot original funcional: carga el Excel con `POWER_AUTOMATE_EXCEL_URL` o `POWER_AUTOMATE_MAPPING_URL`, normaliza `RutaTOACompleta`, obtiene el `providerid` de la ruta hija, expande la macrozona padre por texto y luego selecciona la ruta hija por `data-label-pid`. No intenta expandir la macrozona por PID padre.


## Ajuste de ingreso a actividad

Esta versión intenta ingresar por `ID_TOA/AID` si la validación por Orden/Tipo no encuentra coincidencia en la grilla. También usa scroll más fino (`MONITOR_SCROLL_GRID_DELTA=250`) para evitar saltarse filas virtualizadas de Oracle DataGrid.

## Último ajuste aplicado

Esta versión busca primero en la grilla por **OrdenVenta/OrdenTrabajo + Estado_actividad**. Cuando encuentra un candidato, ingresa al detalle y valida que dentro del formulario aparezca el **ID_TOA** esperado. Solo después de confirmar el ID extrae:

- `Estado_actividad` desde `data-label="astatus"`
- `Observacion_actividad` desde campos de observación visibles, priorizando `data-label="XA_obse_tecn"`

Luego actualiza SharePoint con ambos valores.


## Validación ID_TOA

En esta versión el monitor NO usa el `ID_TOA/AID` para buscar la actividad en la grilla. Primero encuentra el candidato por `OrdenVenta/OrdenTrabajo` + `Estado_actividad`, ingresa a la actividad y recién dentro del detalle valida que el `ID_TOA` corresponda.

## Control de fecha entre actividades

Esta versión vuelve explícitamente a la fecha actual al terminar cada actividad, incluso cuando no se detectan filas visibles en la grilla. Esto evita que el siguiente registro de SharePoint se procese desde una fecha relativa incorrecta.

Variables relevantes:

```env
MONITOR_VOLVER_FECHA_ACTUAL_AL_FINAL=true
MONITOR_REABRIR_TOA_CADA_ACTIVIDAD=false
```

Con `MONITOR_REABRIR_TOA_CADA_ACTIVIDAD=false`, el monitor no vuelve a abrir TOA después de cada registro. Primero deja la fecha en el día actual y luego continúa con el siguiente elemento de la lista.


## Validación para ingresar a actividad

Esta versión busca en la grilla de TOA usando dos columnas visibles antes de ingresar al detalle:

- `appt_number`: Orden de venta / Orden del cliente / Orden de trabajo.
- `aworktype`: Tipo de actividad.

El `ID_TOA` no se usa para buscar en la grilla. Se valida recién dentro del detalle de la actividad. Después extrae `Estado_actividad` y `Observacion_actividad` y actualiza SharePoint.


## Marcado automático como ELIMINADO

Si el monitor llega a la ruta/fecha, aplica la vista y no encuentra una actividad que coincida por **OrdenVenta/OrdenTrabajo + TipoActividad**, actualizará el mismo item de SharePoint con:

```env
Estado_actividad=ELIMINADO
```

Variables relevantes:

```env
MONITOR_MARCAR_ELIMINADO_SI_NO_ENCUENTRA=true
MONITOR_ESTADO_SI_NO_ENCUENTRA=ELIMINADO
MONITOR_IGNORAR_ESTADOS_ACTIVIDAD=cancelada,finalizada,eliminado
```

Si faltan datos de validación desde SharePoint, como OrdenVenta/OrdenTrabajo o TipoActividad, no marca ELIMINADO para evitar falsos positivos.
