# Consulta segura de incidencias BMC Helix (Etapa 1)

El flujo permite elegir Grupo Resolutor y Usuario Resolutor y escribir una Resolución en Tkinter; busca la incidencia, completa los campos de `Redes Chile`, selecciona Estado `Cerrado` y Motivo `Resolución autom. notificada`, escribe Resolución y pulsa `Guardar` al final.

## Suposiciones

- Windows con Google Chrome instalado, Python 3.11 o superior y una sesión SSO ya autenticada o que pueda autenticarse manualmente en el Chrome abierto por la herramienta.
- Al ejecutar `python main.py` solo se abre Tkinter; Chrome no se inicia hasta enviar una incidencia.
- La automatización mantiene una única pestaña Chrome. Las páginas emergentes se cierran para evitar ventanas duplicadas; la búsqueda debe ocurrir en la SPA principal.
- No se presupone idioma de interfaz. Los selectores se obtienen de la sesión real mediante Playwright codegen.
- Chrome queda abierto entre búsquedas mientras Tkinter siga abierto. Al cerrar Tkinter, el contexto de Chrome se cierra limpiamente.

## Estructura

```text
.
├── main.py
├── requirements.txt
├── README.md
└── helix_stage1/
    ├── __init__.py
    ├── automation.py
    ├── config.py
      ├── resolver_catalog.py
    ├── selectors.py
    └── ui.py
```

En tiempo de ejecución se crean `chrome_profile/` y `logs/`; están excluidos de Git porque contienen datos de sesión y operación.

## Instalación y ejecución

Desde PowerShell, situado en la carpeta del proyecto:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chrome
python main.py
```

Tkinter suele venir incluido con Python para Windows. `playwright install chrome` instala el canal de Chrome administrado por Playwright si hace falta; `channel="chrome"` también puede usar Google Chrome instalado en el equipo.

## Obtener los selectores reales

1. Cierra la aplicación si está ejecutándose. Abre PowerShell en la carpeta del proyecto y lanza:

   ```powershell
   playwright codegen --target python --channel=chrome "https://entelhelix.onbmc.com/arsys/forms/onbmc-s/SHR%3ALandingConsole/Default+Administrator+View/"
   ```

2. En la ventana de Chrome de codegen, inicia sesión manualmente si SSO no te autentica. No compartas contraseñas, cookies, tokens, datos personales ni capturas con información sensible.
3. En Helix, haz clic manualmente en `Buscar incidencia`, ingresa un número de prueba autorizado en `ID de la incidencia` y pulsa Enter. No modifiques ni guardes registros.
4. Indica si después de Enter aparece una lista de resultados o se abre directamente el formulario del incidente. No incluyas credenciales, cookies ni datos sensibles.
5. Las capturas compartidas permiten usar estos locators para el paso actual:

   ```python
   OPEN_INCIDENT_SEARCH = LocatorSpec("css", '[ardbn="z2NI_SearchIncident"][artype="NavBarItem"]')
   SEARCH_FIELD = LocatorSpec("label", "ID de la incidencia*+")
   ```

   El atributo `ardbn="z2NI_SearchIncident"` se observó en el elemento `NavBarItem` del HTML adjunto; evita la coincidencia con etiquetas de texto ocultas. Evitamos IDs `arid_WIN_…` porque son autogenerados. Los selectores de resultados y verificación se definirán cuando conozcamos el comportamiento de Enter.

El botón de la UI se llama `Buscar incidencia`: navega al formulario, escribe el número, valida el valor y envía Enter. La pestaña queda abierta en la respuesta de Helix.

## Carga y procesamiento de Excel

La UI permite cargar archivos `.xlsx`, `.xlsm` u `.ods` con el botón `Cargar Excel`. La hoja activa debe tener una columna con cabecera `Incidencias`; la cabecera puede estar dentro de las primeras 10 filas. Cada valor se normaliza a mayúsculas y debe cumplir el formato `INC` seguido de 12 dígitos. Las filas vacías se omiten, los duplicados se cargan una sola vez y los valores inválidos se reportan en el log de Tkinter.

Al cargar correctamente, la primera incidencia válida queda escrita en el campo `Número de incidencia` y se genera un TXT en `logs/` con columnas `numero de incidencia` y `Estado`; en ese resumen inicial `OK` significa incidencia válida cargada y `NOK` significa fila inválida o duplicada.

El botón `Procesar Excel` ejecuta las incidencias cargadas una por una. La UI envía una incidencia al trabajador de Playwright, espera que termine con `searched` o `error`, registra el resultado y solo entonces inicia la siguiente. Si una incidencia falla, el lote se detiene por seguridad y genera un reporte parcial para evitar continuar sobre una pantalla posiblemente inconsistente. Durante cada incidencia se muestra `Automatización en proceso`; no manipules Remedy manualmente mientras ese mensaje esté activo, porque la automatización valida cada paso contra la pantalla esperada. Al finalizar el lote se genera un reporte `resultado_cierre_excel_*.txt` en `logs/`.

## Grupo y usuario resolutor

Tkinter ofrece dos listas desplegables: `NOC 1L DX` y `NOC 1L TX`. Al elegir grupo, Usuario Resolutor queda limitado a los usuarios asociados a ese grupo; se debe escoger ambos antes de enviar la incidencia. El catálogo actual en `helix_stage1/resolver_catalog.py` refleja literalmente las capturas: 11 usuarios para DX y 9 para TX. Está aceptado como borrador y se puede corregir después.

El grupo, usuario y texto de Resolución elegidos en Tkinter viajan con la orden del trabajador. Después de abrir `Redes Chile`, se localiza el widget Estado por `[ardbn="Status"][artype="EnumSel"]`. Dentro de `div.selection`, el input readonly muestra el estado actual y el enlace hermano `a.selectionbtn` abre el menú. La opción se busca solo dentro de `table.MenuTable tr.MenuTableRow td.MenuEntryName`, con texto exacto `Cerrado`, evitando otra coincidencia de la página (`td.f1.trimJustl`). Después se verifica el valor del input Estado.

Para `Motivo del estado`, se usa el control observado `textarea[armenu="SYS:RSN:StatusReason-Q-HPD-HelpDesk"]`; el enlace hermano `a.btn.btn3d.menu` abre su lista. Se selecciona solo la opción visible exacta `Resolución autom. notificada` de `MenuTable` y se verifica el valor del textarea; después se escribe y verifica el textarea etiquetado `Resolución`.

Al final se espera el control `Guardar` de Remedy (`a[artype="Control"][arid="301614800"]`) habilitado y se pulsa una sola vez. El flujo espera una respuesta y busca una confirmación visible de Remedy. Si no la detecta, informa que el clic fue enviado y no lo repite automáticamente; verifica Remedy antes de volver a ejecutar para evitar un guardado duplicado. Si la página sigue abierta, después del guardado se pulsa `Inicio` (`a[artype="Control"][title="Inicio"]`) y se espera nuevamente el menú `Buscar incidencia` para dejar Remedy listo para la próxima búsqueda.

## SSO alternativo: Chrome con depuración remota

Si el perfil aislado `chrome_profile/` no obtiene el SSO automáticamente, se puede iniciar una instancia de Chrome dedicada con depuración remota, autenticarla manualmente y conectar Playwright mediante CDP. Cierra primero la aplicación y cualquier proceso que use ese mismo perfil. En PowerShell, inicia Chrome con un perfil dedicado (ajusta la ruta de `chrome.exe` si fuese necesario):

```powershell
& "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$PWD\chrome_profile"
```

Después de autenticarte, la alternativa de conexión en Python es:

```python
browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
context = browser.contexts[0]
```

En ese modo, el cierre debe desconectar Playwright sin cerrar el navegador que el usuario mantiene abierto. Es una alternativa de configuración distinta a `launch_persistent_context`; no se activa automáticamente en esta Etapa 1. No expongas el puerto 9222 fuera de `127.0.0.1`.

## Comportamiento y seguridad

- Tkinter y Playwright se comunican por colas; un único hilo dedicado crea y usa Playwright.
- El perfil del navegador persiste entre ejecuciones de la aplicación. No uses simultáneamente ese perfil en otra instancia de Chrome.
- No se reintentan Enter ni Guardar, para evitar acciones duplicadas.
- Se esperan hasta 90 segundos para cargar navegación y campos; el clic dispone de 30 segundos. Además hay pausas base configurables: 2 s tras cargar consola, 3 s tras seleccionar menú, 1.5 s tras escribir ID, 4 s tras Enter, 3.5 s tras abrir Redes Chile, 2.5 s después de Grupo, sin pausa después de Usuario, 1.5 s tras abrir Estado y 2.5 s después de elegir Cerrado. El valor de Usuario Resolutor se verifica inmediatamente y luego se inicia el paso de Estado.
- Puedes ajustar los márgenes y timeouts en `helix_stage1/config.py` (`*_SETTLE_DELAY_MS`, `SPA_READY_TIMEOUT_MS`, `RESOLVER_FIELD_TIMEOUT_MS` y `MENU_CLICK_TIMEOUT_MS`). Son pausas de Playwright, no `time.sleep`; los locators siguen esperando hasta sus timeouts si Remedy necesita más tiempo.
- Se comprueba que el campo visible contenga exactamente el número recibido desde Tkinter.
- Ante error se registra fecha, incidencia y resultado en `logs/automation.log`. La aplicación no guarda capturas de pantalla automáticamente.
- Si Helix redirige a login, autentícate manualmente. La herramienta no automatiza SSO.
- El flujo termina tras enviar Guardar. La UI indica si detectó confirmación de Remedy; si no aparece, verifica el estado en Remedy antes de volver a ejecutar.
