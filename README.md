# Automatización de incidencias BMC Helix

Aplicación de escritorio para cerrar incidencias individualmente o desde una planilla y para preparar nuevas incidencias en BMC Helix ITSM. La interfaz está hecha con CustomTkinter y la navegación de Remedy usa Playwright con Google Chrome.

## Requisitos

- Windows, Google Chrome y Python 3.11 o superior.
- Acceso autorizado a Helix y sesión SSO iniciada manualmente cuando Chrome la solicite. La aplicación no automatiza el inicio de sesión.
- Una sola instancia de la aplicación usando el perfil `chrome_profile/` a la vez.

## Instalación

Abre PowerShell en la raíz del proyecto. Comprueba la versión instalada con `py --list` y crea el entorno con una versión disponible de Python 3.11 o superior:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chrome
```

No hace falta activar `.venv`, así que estos comandos también funcionan cuando PowerShell bloquea `Activate.ps1`. Si `py -3.11` indica que no existe esa versión, instala Python o sustituye `-3.11` por una versión disponible compatible.

Crea los archivos locales solo si todavía no existen:

```powershell
if (-not (Test-Path helix_stage1\local_settings.py)) { Copy-Item helix_stage1\local_settings.example.py helix_stage1\local_settings.py }
if (-not (Test-Path resolver_catalog.local.json)) { Copy-Item resolver_catalog.example.json resolver_catalog.local.json }
if (-not (Test-Path customer_catalog.local.json)) { Copy-Item customer_catalog.example.json customer_catalog.local.json }
if (-not (Test-Path email_recipients.local.json)) { Copy-Item email_recipients.example.json email_recipients.local.json }
```

En `helix_stage1/local_settings.py`, configura `HELIX_URL` con la URL real de tu consola. El canal de navegador predeterminado es `chrome`. En `resolver_catalog.local.json`, registra grupos y usuarios resolutores; en `customer_catalog.local.json`, los apellidos seleccionables como Cliente al crear un ticket. En `email_recipients.local.json`, los correos se organizan por operador y grupo; la interfaz permite seleccionar cada grupo por separado para TO o CC. Los archivos `*.example.*` muestran el formato sin publicar datos reales.

Para iniciar:

```powershell
.\.venv\Scripts\python.exe main.py
```

Chrome se abre cuando se envía la primera operación. Mientras la automatización esté trabajando, no manipules Remedy manualmente.

## Cierre manual

En **Cierre manual**, selecciona Grupo Resolutor y Usuario Resolutor, ingresa una incidencia `INC` seguida de 12 dígitos y escribe la Resolución. La opción **Cerrar tareas asociadas antes del cierre** es voluntaria.

El flujo busca la incidencia, revisa las tareas si se marcó esa opción y, antes de abrir **Redes Chile**, cierra las pendientes una por una; las que ya están cerradas se omiten. Después completa Grupo Resolutor, Usuario Resolutor, Estado **Cerrado**, Motivo del estado **Resolución autom. notificada** y Resolución. Pulsa **Guardar** una vez y vuelve a Inicio para dejar la consola lista. Si ocurre un error, se detiene y lo muestra en la UI.

## Cierre masivo

En **Cierre masivo (Excel)**, carga un archivo `.xlsx`, `.xlsm` u `.ods`. La hoja activa debe contener las columnas **Incidencias** y **Resolución** dentro de las primeras 10 filas. Cada fila necesita su propia Resolución. Se omiten incidencias inválidas, duplicadas o sin Resolución, y se genera un resumen de carga en `logs/`.

Selecciona Grupo Resolutor y Usuario Resolutor, decide si también se cerrarán tareas y pulsa **Iniciar cierre masivo**. La aplicación pide confirmación y procesa las incidencias válidas una a una. Si una falla, detiene el lote y genera un reporte parcial; al terminar sin errores muestra **Trabajos finalizados**. El resultado queda en `logs/resultado_cierre_excel_*.txt`.

## Crear ticket

En **Crear ticket**, elige un Cliente del catálogo local y escribe un título de hasta 100 caracteres. La automatización abre **Nueva incidencia**, selecciona el Cliente sugerido por Remedy, busca la plantilla `TX-`, establece **Condición de falla: Sin Pérdida**, escribe el título en **Resumen** y **Notas**, y cambia **Empresa** a **REDES CHILE** en Categorización. Lee el ID asignado antes de pulsar **Guardar** una sola vez.

Al terminar, la UI deja disponibles el asunto `INC..., título` y el cuerpo del correo para copiar. Puedes elegir un grupo para TO y otro para CC; ambos son opcionales. **Crear correo** abre un borrador en la aplicación de correo predeterminada incluso sin destinatarios, y no lo envía. Cada selección incluye solo las direcciones de ese grupo: los grupos Entel de nueva incidencia, corte troncal y escalamiento TX Core no se combinan; GTD Telsur reúne los contactos de supervisión Telsur y escalamiento GTD. Las direcciones repetidas entre TO y CC se dejan solo en TO. El catálogo de ejemplo está vacío; el local se carga al iniciar la app. Además, agrega una línea `INC..., título` a `logs/Incidencias creadas.txt`. Remedy vuelve a Inicio tras el guardado; el resultado permanece visible hasta el siguiente guardado.

El mensaje de creación indica que **Guardar fue enviado**; este flujo no realiza una búsqueda posterior para comprobar que Remedy haya confirmado el alta. Si el clic queda incierto, la herramienta advierte y no lo reintenta automáticamente.

## Archivos y seguridad

- `helix_stage1/automation.py`: navegación y acciones Playwright; `selectors.py`: selectores observados en Helix.
- `helix_stage1/ui.py`: interfaz, resultados y reportes; `excel_loader.py`: lectura y validación de planillas.
- `helix_stage1/resolver_catalog.py`, `customer_catalog.py` y `email_catalog.py`: cargan los JSON locales sin incluir datos reales en el código publicado.
- `chrome_profile/`, `logs/`, `helix_stage1/local_settings.py` y los catálogos locales de resolutores, clientes y correos están excluidos de Git. El perfil puede contener datos de sesión y los logs pueden contener datos operativos: no los compartas ni los subas manualmente.
- `logs/automation.log` guarda eventos y errores. La aplicación no toma capturas de pantalla automáticamente.

Los selectores dependen de la interfaz de Helix y pueden necesitar ajustes si Remedy cambia. El cierre intenta reconocer una confirmación visible después de Guardar; si no la encuentra, informa que el clic se envió para que revises Remedy antes de reintentar. La aplicación no debe usarse como prueba independiente de que un cambio quedó persistido.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Estas pruebas cubren principalmente el flujo de creación. No sustituyen una prueba controlada en Helix para cierre manual, tareas y cierre masivo.
