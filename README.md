# GitPush Studio 3.4.0

Publica, clona y empaqueta tus proyectos de GitHub sin tocar la terminal;
varias carpetas a la vez a tu misma cuenta, cada una con su TXT; y
**analiza tus proyectos** (lenguajes, líneas, duplicados, historial) con un
informe HTML que no se conecta a nada.

La 3.4 vuelca en un TXT **todo lo que se habló en un repositorio de GitHub**:
cada issue y cada pull request, abiertos y cerrados, con quién escribió qué y
cuándo, las revisiones, los comentarios en el código en hilo, los commits y
los eventos, en orden (ver «Qué cambia en 3.4»).

La 3.3 cerró el círculo con las IA: el TXT del proyecto se le pasa a una IA
(con los secretos tachados, sin lo que no aporta, solo lo que cambió y
repartido en partes que quepan en su contexto) y lo que devuelva se
**aplica de vuelta** archivo a archivo, viendo antes cada línea que cambia y
con una copia exacta previa. Además: auto-publicación que reacciona al
momento, revisión de secretos en todo el historial, Git LFS para los
archivos de más de 100 MB, «¿por qué no se sube este archivo?», diferencias
línea a línea en la vista previa, arrastrar carpetas, atajos, bandeja del
sistema, arranque con Windows y un `.exe` que no necesita Python. Y nunca
publica en el repositorio equivocado: si la carpeta ya publica en otro que
el del formulario, pregunta antes de tocar nada (ver «Qué cambia en 3.3»).

El TXT **no tiene límite de tamaño**: cada archivo de texto entra entero,
mida lo que mida (ver «El TXT sin límites»). Y lo que pones en «Archivos y
carpetas a ignorar» **no se escribe en el `.gitignore`**: queda fuera de
GitHub sin que allí aparezca ni su nombre (ver «Lo que no se publica, sin
.gitignore»). La 3.2 fue una versión de **seguridad de los datos** (ver «Qué
cambia en 3.2»).

```
gitpush_studio/
├── __init__.py          nombre y versión
├── __main__.py          python -m gitpush_studio  → interfaz gráfica
├── cli.py               python -m gitpush_studio.cli → publish | batch | scan | pack | apply-txt
│                        | analyze | scan-history | issues-txt | why | status | pull | versions
│                        | restore | repos | branches | release
├── core/                cero tkinter, cero dependencias externas
│   ├── errors.py        StepError · Cancelled · Blocked
│   ├── fs.py            tamaños, binarios, rutas, nombres que no son UTF-8
│   ├── desktop.py       abrir un archivo · «abrir ubicación» con el archivo seleccionado
│   │                    (shell de Windows por ctypes · Finder · FileManager1 por D-Bus)
│   ├── atomic.py        escrituras atómicas · creación exclusiva · temporales olvidados
│   ├── urls.py          parseo de URLs/usuarios de GitHub
│   ├── refnames.py      nombres de rama válidos con las reglas de git (sin procesos)
│   ├── ignore.py        IgnoreMatcher (reglas de git) · quitar del .gitignore lo que
│   │                    ahora va a la lista privada (el resto, byte a byte igual)
│   ├── exclude.py       lo que no se publica, en la lista privada de git
│   │                    (.git/info/exclude): nunca en el .gitignore, solo crece
│   ├── batch.py         varias carpetas a una misma cuenta: validar · resolver URLs · plan
│   ├── reader.py        lectura por lotes con memoria acotada y ritmo MEDIDO (1 hilo si abrir
│   │                    es instantáneo, hasta 16 si cada apertura espera al antivirus);
│   │                    nunca sigue enlaces ni abre tuberías
│   ├── textenc.py       ¿texto o binario? UTF-8 / UTF-16 (con y sin BOM)
│   ├── txtbody.py       el contenido de cada archivo en el TXT, por bloques: UTF-8 tal cual,
│   │                    UTF-16 y ANSI (cp1252) convertidos, caracteres partidos recompuestos
│   ├── walk.py          recorrido scandir (.gitignore anidados) · project_entries · árbol · huella
│   ├── secret_rules.py  qué es un secreto: formatos de proveedor, nombres, jueces de valores
│   ├── scan.py          revisión de seguridad: todo el contenido, por trozos, con presupuesto
│   ├── history_scan.py  secretos en TODO el historial: commit de entrada y si sigue
│   ├── lfs.py           archivos de más de 100 MB por Git LFS (todo o nada)
│   ├── why.py           ¿por qué no se sube? (la regla exacta, con archivo y línea)
│   ├── snapshot.py      descarga por .tar.gz (sin git) con extracción segura y todo-o-nada
│   ├── preamble.py      presentación en prosa y resumen final de los TXT
│   ├── txtformat.py     el formato del TXT: lo que escribe pack y el lector tolerante de lo
│   │                    que devuelve una IA (``` de Markdown, sin rayas, texto alrededor…)
│   ├── diff.py          diferencias línea a línea (Myers, memoria lineal, presupuesto)
│   ├── apply_txt.py     aplicar un TXT de vuelta: rutas seguras, línea a línea, copia previa
│   ├── redact.py        tachar secretos en el TXT y recuperarlos al aplicar
│   ├── txtchanges.py    el TXT de «solo lo que cambió» (enteros o solo diferencias)
│   ├── txtparts.py      el TXT repartido en partes (tokens estimados, archivos enteros)
│   ├── gitblobs.py      leer de git lo que no cambió (un proceso; idéntico, comprobado)
│   ├── pack.py          consolidar a TXT (proyecto, cada repo, perfil entero): sin límite,
│   │                    atómico, con progreso, «Detener» y freno antes de llenar el disco
│   ├── net.py           HTTP con redirecciones seguras, tope de tamaño y Retry-After
│   ├── urls.py          URLs de GitHub: el mismo repositorio escrito de mil formas, sin tokens
│   ├── git.py           Git (run/stream/read_blobs/commit_tree) · parsers puros
│   │                    · auth por entorno (solo github.com) · clone_repo · BulkCloner
│   ├── gitspeed.py      git status rápido (untrackedCache/fsmonitor) solo si es seguro
│   ├── watch.py         vigilar la carpeta sin sondear (inotify / ReadDirectoryChangesW)
│   ├── versions.py      las dos versiones de un archivo en la vista previa
│   ├── startup.py       abrir la app al iniciar Windows (clave Run del usuario)
│   ├── updates.py       ¿hay versión nueva? (una vez al día, solo si lo pides)
│   ├── gitobj.py        identificadores de blobs de git calculados en Python (verificación)
│   ├── safekeeping.py   copia EXACTA en una rama de respaldo que no se publica · borrar solo
│   │                    lo guardado · recuperar un respaldo · archivos en riesgo
│   │                    · repositorios anidados · guardián de cambios durante la operación
│   ├── integrate.py     traer cambios sin hacer commit de lo tuyo (merge-tree + 3 vías)
│   ├── github.py        API mínima (REST + GraphQL con respaldo a REST) · repos · Pages
│   ├── secrets.py       DPAPI / llavero macOS (por stdin) / secret-tool / b64 (avisable)
│   ├── config.py        carga con copia de seguridad · tipos validados · guardado atómico
│   ├── diaglog.py       registro de diagnóstico con rotación y sin secretos
│   ├── licenses.py      catálogo de licencias · texto incluido o descargado · LICENSE exclusivo
│   ├── publish.py       PublishJob · Callbacks · STEPS (13) · publish · publish_many
│   ├── remotes.py       ¿la carpeta ya publica en OTRO repositorio? (nunca se cambia solo)
│   ├── ghsession.py     la API de GitHub para descargas largas: una conexión, gzip con tope,
│   │                    «Link», esperas por la cuota, proxy, el token solo a la API
│   ├── ghstore.py       lo descargado, en SQLite página a página (para seguir donde se quedó)
│   ├── ghfetch.py       qué se descarga: listados de todo el repositorio + lo de cada PR
│   ├── ghrender.py      el TXT del historial: índice, conversaciones, hilos, sin ambigüedad
│   ├── ghdump.py        el volcado entero: repositorio → almacén → TXT, sin pisar nada
│   ├── sync.py          traer cambios de GitHub: inspect (qué pasaría en cada modo) · apply
│   ├── history.py       list_versions · restore_version · restore_backup · describe_changes
│   ├── release.py       suggest_tag · create_release (etiqueta + Release + zip)
│   ├── languages.py     59 lenguajes: comentarios, cadenas, docstrings, extensiones, shebangs
│   ├── linecount.py     código / comentario / en blanco enmascarando con expresiones en C
│   ├── gitstats.py      historial en flujo: personas (unión de alias), meses, horas, archivos
│   ├── analytics.py     análisis del proyecto: lenguajes, generado, duplicados, TODO
│   └── report_html.py   informe HTML autónomo (CSP estricta, sin red) y JSON
├── informe/plantilla.html   el informe: gráficos SVG propios, tema claro/oscuro, accesible
├── assets/              el icono (dibujado por tools/crear_icono.py, sin librerías)
└── ui/                  solo widgets y callbacks
    ├── theme.py · widgets.py · base.py · console.py · help.py · jobs.py
    ├── diffview.py      diferencias con colores (solo lectura, se pueden copiar)
    ├── winshell.py      Windows por ctypes: soltar carpetas y el icono de la bandeja
    ├── app.py           ventana principal: compone las piezas de abajo
    ├── app_form.py      el formulario (estado, licencia, modo, ignorar)
    ├── app_profiles.py  perfiles guardados y token
    ├── app_dialogs.py   abrir cada herramienta una sola vez
    ├── app_publish.py   analizar y publicar (una, marcadas, varias carpetas)
    ├── app_auto.py      auto-publicación (vigilante en tiempo real + ronda periódica)
    ├── app_windows.py   escritorio: atajos, soltar, bandeja, inicio, versiones nuevas
    └── dialogs/         batch · preview · review · clone · bulk_clone · consolidate · pages
                         · create_repo · sync · history · branches · release · analytics
                         · apply_txt · history_scan · why · remote · issues_txt
tools/
├── benchmark.py         proyecto sintético grande y tiempos de cada operación
├── mutaciones.py        pruebas de mutación: rompe el código a propósito y exige que las
│                        pruebas lo detecten
├── mutaciones_seguridad.json   las mutaciones del código que borra, pisa o publica archivos
├── crear_icono.py       dibuja el icono (PNG e ICO escritos a mano)
└── construir_exe.py     «GitPush Studio.exe» con PyInstaller (sin Python instalado)
.github/workflows/windows-exe.yml   el .exe se construye solo al publicar una etiqueta «v…»
tests/                   925 pruebas; las de seguridad comparan FOTOS completas de
                         carpeta y remoto antes/después (nada cambia fuera de lo aprobado)
├── test_apply_txt.py    aplicar un TXT: nunca fuera, nunca a través de enlaces, línea a
│                        línea byte a byte, copia previa, nada si algo cambió entre medias
├── test_txt_for_ai.py   ningún secreto llega al TXT tachado; partes que nunca se aplican
│                        como archivo entero; solo lo que cambió
├── test_safety.py       publicar/traer/restaurar nunca borra ni pisa nada sin permiso
├── test_backups.py      copias exactas, invisibles, verificadas antes de borrar
├── test_pack_safety.py  el TXT no escribe fuera, no sigue enlaces, no se cuelga
├── test_txt_unlimited.py    archivos de 40 MB enteros sin cargarlos, TXT de más de 100 MB,
│                        bloques de 1 byte = archivo entero, UTF-16/ANSI, «Detener», disco
├── test_scan.py         lo que la revisión se dejaba y lo que marcaba de más
├── test_ignore_vs_git.py    patrones y .gitignore anidados contra git real
├── test_private_excludes.py lo del cuadro no llega a GitHub ni por su nombre, en ningún
│                        commit; tu .gitignore intacto; un «!» no lo cuela
├── test_fuzz.py         fuzzing propio de todo lo que lee datos ajenos
├── test_net.py          token solo a GitHub, redirecciones, respuestas rotas, procesos colgados
├── test_remotes.py      nunca se publica en el repositorio equivocado (ni con --yes, ni solo)
├── test_ghdump.py       el historial a TXT contra un GitHub falso (fakegithub.py): el token
│                        nunca sale de la API, cuota, bomba gzip, reanudar = mismo TXT
└── …                    flujo, analíticas, licencias, interfaz sin pantalla, etc.
```

## Ejecutar

Al descomprimir el zip queda una carpeta `gitpush_studio/` con todo el proyecto dentro: los
comandos de abajo se ejecutan desde ella (o abre `GitPushStudio.pyw` con doble clic).

```
pip install customtkinter          # solo para la interfaz gráfica
python -m gitpush_studio           # interfaz
python -m unittest discover -s tests -t . -v
python -m unittest tests.test_safety tests.test_backups -v     # seguridad de datos
python tools/benchmark.py --files 20000                         # rendimiento
python tools/mutaciones.py                 # ¿las pruebas detectan los fallos? (~15 min)

python -m gitpush_studio.cli publish --profile web --profile api --profile docs
python -m gitpush_studio.cli publish --folder ./mi-app --url https://github.com/yo/mi-app.git --yes
python -m gitpush_studio.cli publish --folder ./mi-app --url https://github.com/yo/nuevo.git --yes --switch-remote
python -m gitpush_studio.cli batch --project ./web --project ./api --project "./docs=mis-docs"
python -m gitpush_studio.cli scan ./mi-app
python -m gitpush_studio.cli pack ./mi-app -o mi-app.txt      # nunca encima de un .txt tuyo
python -m gitpush_studio.cli pack ./mi-app --for-ai --redact --changes archivos --split-tokens 128000
python -m gitpush_studio.cli apply-txt ./mi-app respuesta-de-la-ia.txt --diff   # pregunta antes
python -m gitpush_studio.cli scan-history ./mi-app             # secretos en todo el historial
python -m gitpush_studio.cli issues-txt yo/mi-app -o historial.txt --redact   # issues y PR
python -m gitpush_studio.cli why ./mi-app notas/borrador.md    # ¿por qué no se sube?
python -m gitpush_studio.cli analyze ./mi-app --html informe.html --json datos.json
python -m gitpush_studio.cli pull ./mi-app --url https://github.com/yo/mi-app.git --mode merge
python -m gitpush_studio.cli versions ./mi-app
python -m gitpush_studio.cli restore ./mi-app 272f003                    # versión: commit nuevo
python -m gitpush_studio.cli restore ./mi-app respaldo-2026-10-16-0930   # respaldo: sin commit
python -m gitpush_studio.cli release ./mi-app --url https://github.com/yo/mi-app.git --token ghp_… --tag v1.2.0
```

## Flujo local ↔ GitHub

- **Copias de seguridad exactas que no se publican.** Antes de cualquier operación que pise
  o borre archivos (traer cambios, restaurar, recuperar un respaldo, y al publicar si un
  archivo tuyo ignorado choca con uno de GitHub) se guarda la carpeta en una rama
  «respaldo-FECHA» **byte a byte** (`hash-object --no-filters`: con CRLF, binarios, nombres que
  no son UTF-8), con un índice temporal (el tuyo no se toca), sin hooks y sin mover tu rama.
  Entran lo rastreado, lo no rastreado y —aunque estén ignorados— los archivos que la operación
  va a pisar. Esa rama **no forma parte de lo que se publica**.
- **Nada se borra «porque está en la copia»**: antes de borrar un archivo se recalcula, con una
  implementación propia del identificador de git (`gitobj`), que lo que hay en el disco es lo
  guardado. Si cambió entre medias, no se borra.
- **Traer cambios de GitHub** (⬇⬆): «Comprobar» enseña qué pasaría **en cada modo** (al cambiar
  de modo se vuelve a pintar) y al aplicar se trae **exactamente lo que se enseñó**: si GitHub
  cambió entre medias, no se aplica nada y se pide comprobar otra vez.
  - *Combinar*: lo tuyo sin publicar **se queda sin commit** (al publicar pasará por la vista
    previa y la revisión de secretos). Si GitHub y tú cambiasteis el mismo archivo se juntan
    las líneas y, donde chocan, gana GitHub; un binario, entero. Si un lado borró lo que el
    otro cambió no se adivina: no se toca nada y se propone el modo espejo.
  - *Espejo de GitHub*: la carpeta queda exactamente como GitHub; lo ignorado que no estorba
    se queda.
- **Versiones** (⏪): «Restaurar» una versión prepara el commit nuevo ANTES de tocar nada (si git
  no puede crearlo —identidad, firma—, la carpeta sigue intacta). «Recuperar» un **respaldo**
  devuelve sus archivos a la carpeta **sin commit**: un respaldo puede contener un `.env` o un
  borrador que nunca se publicó, y restaurarlo como versión lo habría metido en el historial.
- Nunca se trabaja encima de una operación de git a medias (merge, rebase, cherry-pick,
  bisect), de conflictos sin resolver ni de un HEAD suelto: se explica y no se toca.
- **Si la carpeta cambia mientras se trabaja** (un editor que guarda, una sincronización), lo
  que la operación iba a pisar ya no coincide con la copia: justo antes de tocar el primer
  archivo se comprueba (estado en git + tamaño, fecha y tipo de cada ruta afectada) y, si algo
  cambió, no se hace nada. Lo que la operación no toca no se vigila: guardar otro archivo no
  bloquea nada.
- **Otros repositorios dentro del proyecto** (una carpeta con su propio `.git`): si la versión
  que llega pone un archivo en su sitio, git borraría la carpeta entera con su historial, y eso
  no cabe en un respaldo. Se detecta antes y no se empieza (espejo, restaurar, publicar en
  agregar/editar, actualizar un clon); en *Combinar* aparece como un choque más. Al publicar,
  esa carpeta **no se sube** (git solo guardaría un enlace a un commit que GitHub no tiene) y
  se explica por qué; un submódulo registrado en `.gitmodules` se publica como siempre.
- **Ramas** (🌿): la carpeta manda; los nombres se validan con las reglas de git antes de llegar
  a la línea de comandos (un «-» delante sería una opción de git), y siempre se usa el nombre
  completo `refs/heads/…` (con `fetch origin main` a secas, una **etiqueta** llamada `main` en
  GitHub ganaba a la rama y se combinaba otra historia).
- **Vista previa archivo por archivo**: *Mantener* un borrado lo devuelve desde GitHub sin pisar
  nada de la carpeta (tampoco un archivo que esté donde hace falta una carpeta); *No subir* un
  nuevo lo saca del índice; *No actualizar* un modificado deja tu archivo como está. En modo
  agregar/editar la vista previa también anuncia lo que desaparecerá de GitHub porque lo
  excluiste tú (cuadro de ignorar o revisión), y se puede mantener.
- **Licencia al publicar**: `LICENSE` solo si el proyecto no tiene ninguna; se reconocen
  `LICENSE*`, `LICENCE*`, `COPYING*`, `UNLICENSE`, `COPYRIGHT` y la carpeta `LICENSES/`, y la
  creación es exclusiva (si aparece una entre medias, no se pisa).
- **Revisión de seguridad con nivel de confianza** (`alta` / `media` / `baja`): ver más abajo.
- **Conectarse con el token guardado**: la autorización viaja por entorno (`GIT_CONFIG_*`, nunca
  en argv ni en `.git/config`) y **solo hacia `https://github.com/`**: un remoto que apunte a
  otro servidor no la recibe nunca. En la API, una redirección a otro servidor no lleva la
  cabecera `Authorization`.
- **Publicar versión** (🏷): se comprueba antes que la etiqueta no exista ya en GitHub (creada
  desde la web u otro equipo) y que no se llame igual que una rama.

## Revisión de seguridad

Se lee el **contenido de todos los archivos de texto** que se publicarían: los de hasta 1 MB en
paralelo y los grandes (hasta 64 MB) por trozos solapados con memoria acotada; los textos en
**UTF-16** (lo que escribe PowerShell) se leen como texto. Cada regla revisa **todas** sus
coincidencias (un valor de ejemplo encima ya no tapa la contraseña de verdad de debajo) y la
lista se recorta al final quedándose con lo más grave, nunca antes de mirar el contenido.

- **Formatos de proveedor** (`alta`): llaves privadas (PEM, OpenSSH, PGP, PuTTY), GitHub
  (`ghp_ gho_ ghu_ ghs_ ghr_ github_pat_`), GitLab, AWS (id y clave secreta), Google (API y
  OAuth), Slack (tokens y webhooks), Stripe, OpenAI/Anthropic (`sk-…`), SendGrid, npm, PyPI,
  Hugging Face, Telegram, Discord, Azure Storage y contraseñas en cadenas de conexión
  (`postgres://u:clave@…`). Un relleno evidente (`ghp_xxxx…`, `AKIAIOSFODNN7EXAMPLE`, `…`)
  baja a `baja`.
- **Asignaciones** (`API_KEY = "…"`, `DB_PASSWORD=…` en `.env`/`.properties`/`.tfvars`/YAML,
  `define('DB_PASSWORD', …)`): `media`; `alta` si el valor es largo y aleatorio; `baja` si
  parece un ejemplo, una etiqueta («Contraseña» en un archivo de traducciones) o una frase. En
  código fuente, `password = getpass()` es una expresión y no se marca.
- **Nombres** (`alta`): `.env*`, `.envrc`, `id_rsa*`, `*.p12`, `*.pfx`, `*.jks`, `*.ppk`,
  `.git-credentials`, `.netrc`, `*.tfstate`… Los `.pem` y `.key` **se miran por dentro**: un
  `cacert.pem` con certificados públicos o una presentación `charla.key` ya no se marcan.
- Lo que no se pudo leer (un texto descomunal, el presupuesto agotado) **se dice** en vez de dar
  un «sin hallazgos» falso. La ventana marca por defecto lo que no es `baja`, y la
  auto-publicación solo se detiene por `alta` o `media`.
- Por eso **«parece un valor de ejemplo»** (`baja`) solo se dice cuando lo es: plantillas
  (`${VAR}`, `$VAR`, `{{ var }}`, `<tu-token>`), señales claras (`example`, `changeme`,
  `dummy`, `xxxx`) o un valor hecho ENTERO de palabras de relleno (`your_api_key_here`,
  `test123`, `tu_clave_aqui`). Un `$` dentro de una contraseña (`Xk9#mQ2$vL7p`) o una palabra
  como «test» en medio (`Contest2024!`) ya no la rebajan.

Rendimiento (`tools/benchmark.py`, 20.000 archivos, 62 MB, la misma máquina antes → ahora):
recorrido 0,1 s, revisión de secretos 4,3 → 2,6 s, TXT 1,9 → 0,52 s, analíticas 6,4 → 5,1 s,
copia exacta 0,8 s. La revisión y las analíticas se benefician de la misma lectura exacta.

## Analizar proyecto (📊)

Lenguajes y líneas (código, comentario, en blanco; 59 lenguajes, con docstrings), cuánto es
tuyo y cuánto generado o de terceros, archivos duplicados, los más grandes y los TODO; y si es
un repositorio, el historial leído en flujo: quién ha trabajado (uniendo los alias de una misma
persona), cuándo (por meses y por día y hora, en la zona horaria del autor) y qué archivos se
tocan más (siguiendo los renombrados). Se guarda como **informe HTML autónomo** (gráficos
propios, tema claro/oscuro, tablas accesibles, sin ninguna conexión: política CSP
`default-src 'none'`) o como JSON.

## Registro de diagnóstico (🩺)

Lo que pasa por la consola se guarda también en `~/.gitpush_studio/registro/` (5 archivos de
1 MB como mucho, rotando solos) para poder entender un fallo después de cerrar la app. Antes de
escribir se tapan los tokens y las contraseñas dentro de URLs.

## Publicación múltiple

Hay dos caminos, y los dos terminan en el mismo motor (`publish_many`): los proyectos con
remotos distintos van en paralelo, los que comparten remoto en serie, una misma carpeta
nunca se publica dos veces a la vez, y cada proyecto recorre el pipeline completo con su
propia lista de lo que no se publica, su revisión de seguridad y su vista previa. Cuando
dos necesitan tu
decisión, las ventanas se muestran de una en una mientras los demás siguen avanzando. El
panel «Cola de publicación» enseña una fila por proyecto y permite detener (lo que ya está
en marcha termina; no se empieza nada nuevo).

**Desde los perfiles guardados.** Cada perfil describe un proyecto completo (carpeta, URL,
rama, modo, ignorar, identidad). Marca en la barra lateral los que quieras y pulsa
«🚀🚀 Publicar seleccionados».

**Varias carpetas sueltas** («🚀🚀 Publicar varias carpetas…»), para subir proyectos que no
tienes guardados como perfil sin rellenar el formulario una vez por cada uno:

- Una fila por proyecto: carpeta, nombre del repositorio (se propone a partir de la carpeta,
  con las tildes y la ñ resueltas: «Cálculo Numérico» → `Calculo-Numerico`) y la casilla
  **«📄 unir en un .txt», marcada por defecto**. «📂 Añadir las subcarpetas de…» añade de
  golpe todas las de una carpeta madre, saltando las ocultas y las de herramientas.
- **Cada fila tiene su casilla de publicar** (junto a su número) y «Publicar todos» las marca o
  desmarca a la vez. Solo se publican las marcadas; una desmarcada **se queda en la lista con
  sus datos** (y se ve apagada) para otra vez: no se comprueba, no se crea su repositorio en
  GitHub, no se genera su TXT y no estorba a las demás aunque esté a medio rellenar. El botón
  dice cuántas se van a publicar, y qué está marcado se guarda con la lista.
- Todo a **una misma cuenta**: con tu token se detecta el usuario y se crean en GitHub los
  repositorios que falten (privados por defecto); los que ya existan se reutilizan sin tocar
  su contenido. Sin token basta tu nombre de usuario, y se publica en repositorios ya
  creados.
- Rama, modo, licencia, mensaje y lo que se ignora se toman del formulario principal y se
  aplican a todos; la rama y el modo se pueden cambiar ahí mismo.
- Se comprueba **todo antes de tocar nada**: carpetas que no existen, nombres que GitHub no
  acepta, la misma carpeta dos veces, una carpeta dentro de otra del mismo lote (git rechaza
  un repositorio dentro de otro, y el de fuera acabaría con un enlace roto o con los archivos
  del de dentro) y —sobre todo— dos proyectos apuntando al mismo repositorio, que es la forma
  de que uno se coma al otro. Al final se revisa cada trabajo con las mismas reglas que una
  publicación suelta. Si algo falla, no se publica ni un proyecto: un lote a medias es peor
  que ninguno.
- **Nada se da por publicado si no arrancó**: la ventana solo se cierra cuando la publicación
  empieza de verdad. Si dices «no» al aviso del push forzado, o algo más está publicando, la
  lista sigue ahí y la consola dice por qué. Mientras se preparan los repositorios la lista se
  queda quieta (lo que se publica es lo que se ve), cada repositorio creado se anota en la
  consola en el momento —así consta aunque cierres la ventana— y un reintento tras corregir un
  nombre nunca reutiliza la URL del intento anterior.
- La lista se guarda al cerrar, así que volver a abrir no obliga a escribirlo todo otra vez.

### El TXT de cada proyecto

La casilla «unir en un .txt» es un paso más del pipeline (`step_consolidate`), con la misma
función que la herramienta «📄 Consolidar a TXT»: el árbol del proyecto y el contenido de
sus archivos en un solo archivo de texto, dentro de la propia carpeta y llamado como ella.
En la herramienta, «📂 Abrir ubicación» abre la carpeta donde quedó el TXT con el archivo ya
seleccionado (como «Abrir ubicación del archivo» del Explorador de Windows).

Va **después** de la licencia (para que el `LICENSE` recién creado también salga) y **antes**
del paso que deja fuera lo que no se publica, porque el TXT **nunca se sube**: su ruta (y la
de su temporal) entra en la lista privada de git —de modo que ni un TXT a medio escribir
llega a GitHub—, así que sigue excluido en las publicaciones siguientes y aunque se hubiera
subido antes por error (entonces se le quita el seguimiento y **sigue en tu carpeta**). No se
escribe en el `.gitignore`. Respeta el cuadro de ignorar: lo que dejaste fuera no se copia
dentro del TXT.
Si generarlo falla, se avisa y la publicación continúa: el TXT es un extra, no el objetivo.

El TXT lleva **todo tu código junto en un archivo**: guárdalo como guardarías el proyecto.

### El TXT sin límites

Antes, un archivo de más de 2 MB se quedaba fuera («omitido por tamaño») y el TXT se cortaba
a los 100 MB (200 MB el de un perfil). Ya no hay ningún tope:

- **Cada archivo de texto entra entero**, mida lo que mida. Los grandes se copian por bloques
  de 1 MB, así que la memoria no depende de su tamaño (un registro de 40 MB se vuelca con
  menos de 16 MB de memoria; las pruebas lo miden). Procesar por bloques da exactamente lo
  mismo, byte a byte, que procesar el archivo entero: las pruebas lo comprueban con bloques
  de un solo byte que parten los caracteres por la mitad.
- **Es texto si se puede leer**: el UTF-8 se copia tal cual; el UTF-16 (PowerShell, regedit) y
  la codificación clásica de Windows (ANSI: cp1252 en español, o la que use tu Windows; la de
  los CSV de Excel y los .txt antiguos) se convierten a UTF-8, así que «año» sigue siendo «año»
  (antes salía «a�o»); un byte suelto roto dentro de un UTF-8 se ve como «�». Qué es cada
  cosa lo decide el contenido (el primer byte que no es UTF-8, y si antes hubo caracteres
  UTF-8 de varios bytes), nunca dónde caen los bloques. Los binarios (imágenes, ejecutables, modelos…) se mencionan con
  su tamaño, y basta leer su principio para saberlo: uno de varios GB sin extensión conocida
  no se lee entero.
- **Un archivo que crece mientras se lee** (un registro en uso) se copia tal como era al
  abrirlo, con una nota: la copia no se alarga sin fin.
- **Progreso y «Detener»**: la herramienta muestra cuántos archivos y megas lleva; detener (o
  cerrar la ventana) no guarda nada y el TXT anterior queda como estaba. Al publicar,
  «Detener» también para el TXT, y la consola va diciendo cuánto lleva escrito.
- **El disco no se llena**: si el espacio libre baja de 512 MB (o del 5 % de un disco pequeño)
  se detiene sin guardar y lo explica; si el sistema avisa de disco lleno, lo mismo.
- **Las notas de error no llevan tu ruta**: «ERROR AL LEER ARCHIVO: Permission denied», sin
  `C:\Users\…`, porque el TXT se comparte.

Y es varias veces más rápido:

- Cada archivo se lee con **una sola lectura del tamaño exacto**. Antes se pedía «hasta 2 MB»
  y Python reservaba 2 MB por archivo aunque midiera 200 bytes: solo eso costaba cinco veces
  más que leerlo.
- **Cuántos hilos leen a la vez se mide**, no se adivina: con los archivos en la caché del
  sistema abrir uno cuesta ~5 µs y varios hilos solo se estorban (medido: el doble de lento),
  así que lee uno; en Windows, donde el antivirus revisa cada archivo antes de dejarlo abrir
  (milisegundos), se leen hasta 16 a la vez. Se vuelve a medir cada 5 segundos.
- El TXT se escribe con un búfer de 1 MB y el UTF-8 se copia sin decodificar ni recodificar;
  el listado de git consulta los tamaños con el mismo ritmo medido.

Medido en la misma máquina (Linux, 2 núcleos), versión anterior → esta:

| Proyecto | Antes | Ahora |
|---|---|---|
| 18.722 archivos, 50 MB de TXT (archivos en caché) | 2,0 s | 0,48 s |
| el mismo, listado por git | 1,96 s | 0,55 s |
| 4.000 archivos con un «antivirus» que tarda 1 ms en dejar abrir cada uno | 3,1 s | 0,5 s |
| 2 registros de 60 MB, un UTF-8 de 30 MB, un CSV ANSI, un UTF-16 y 20 binarios de 4 MB | 0,2 MB de TXT (todo «omitido por tamaño») | 169 MB completos en 0,25 s |

Los binarios no frenan: viajan dentro de los lotes sin partirlos, no se abren si su extensión
ya lo dice, y una ráfaga de miles de ellos no se acumula en memoria.

## Lo que no se publica, sin .gitignore

Antes, cada línea del cuadro «Archivos y carpetas a ignorar» se añadía al `.gitignore` del
proyecto, y el `.gitignore` se publica: en GitHub se leía `PROJECT_CONTEXT.md`,
`ARCHITECTURE.md`, `notas/`… justo los nombres de lo que no querías enseñar. Ahora:

- **Van a la lista privada de git** (`.git/info/exclude`, módulo `exclude.py`). Git la aplica
  igual que un `.gitignore` —al añadir, en el estado, en lo que lista para la revisión y el
  TXT, en la auto-publicación—, pero vive dentro de la carpeta `.git`, que nunca se sube. En
  GitHub no queda ni el archivo ni su nombre, en ningún commit (las pruebas recorren el
  historial entero buscándolos). Lo mismo con lo que excluyen la revisión de seguridad
  («Ignorar marcados») y la vista previa («no volver a preguntar»), y con el TXT del proyecto.
- **Tu `.gitignore` no se toca**: queda byte a byte como lo dejaste. Si una línea del cuadro
  ya estaba en él (lo hacían las versiones anteriores), se quita de ahí en la siguiente
  publicación —y con ella desaparece de GitHub—; lo que escribiste tú y no está en el cuadro,
  se queda. Si al final no le queda nada, se queda vacío, no se borra. (Los commits antiguos
  de GitHub conservan los `.gitignore` de entonces: eso no se reescribe.)
- **Lo que estaba fuera sigue fuera.** La lista solo crece: publicar con el cuadro vacío,
  abrir la carpeta sin su perfil o publicar varios proyectos con el cuadro de otro no vuelve
  a subir nada. Al elegir un proyecto, el cuadro enseña todo lo que ese proyecto ya deja
  fuera; si quitas una línea y publicas, primero se pregunta («Lo que dejaban fuera volverá a
  publicarse… ¿Seguro?»), y si dices que no, vuelve al cuadro. La auto-publicación nunca
  vuelve a publicar nada; en la línea de órdenes, `--unignore PATRÓN`.
- **Manda el cuadro**: git da preferencia al `.gitignore` sobre su lista privada, así que un
  `!importante.log` en tu `.gitignore` podría colar algo que el cuadro excluye (`*.log`).
  Antes ganaba el cuadro porque sus líneas iban al final del `.gitignore`; ahora se comprueba
  aparte, se deja fuera igualmente y se avisa.
- La lista nunca se escribe a través de un enlace, se escribe de forma atómica, conserva lo
  que hubiera en ese archivo fuera de su bloque y, en un worktree, es la compartida que git
  consulta de verdad.

## Qué cambia en 3.4

### 🗂 Issues y pull requests a TXT (`issues-txt`)

En la barra lateral, «🗂 Issues y PR a TXT…» (o `issues-txt` en la terminal): todo lo que se
habló en un repositorio de GitHub —cada issue y cada pull request, abiertos y cerrados— en un
solo archivo de texto, en orden:

- **Quién y cuándo**: quién abrió cada uno y su relación con el repositorio (propietario,
  colaborador, primera contribución…); cada comentario y cada respuesta con su autor y su hora
  (con la diferencia con UTC); quién lo cerró, lo reabrió o lo fusionó y cuándo; las etiquetas
  que se pusieron y quitaron, las asignaciones, los cambios de título, los revisores pedidos, las
  referencias desde commits y, si se pide, las menciones desde otros issues y pull requests. Las
  cuentas borradas salen como «@ghost (cuenta eliminada)» y los bots, marcados.
- **Los pull requests enteros**: las ramas (y la bifurcación de la que vienen), las revisiones
  (aprobó, pidió cambios…), los comentarios en el código **en hilo** —archivo, línea, el trozo de
  código comentado y cada respuesta— dentro de la revisión en la que se hicieron, y los commits.
- **Rápido**: los listados de todo el repositorio de 100 en 100 (no una consulta por comentario),
  una sola conexión HTTPS que se reutiliza y respuestas comprimidas. Un repositorio con 1.000 pull
  requests son unas 2.000 consultas; con tu token GitHub da 5.000 por hora (sin token, 60).
- **No se pierde nada**: si GitHub pide esperar (se acabó la cuota de la hora, o «ve más
  despacio»), espera y sigue solo; si se corta la conexión, reintenta; si pulsas «Detener» o
  cierras, lo descargado se guarda y la próxima vez sigue desde ahí. El TXT sale idéntico al de
  una descarga de un tirón (está probado).
- **Inequívoco**: lo que escribió cada persona va con sangría, así que una línea que empieza en
  la primera columna siempre es del volcado (un comentario con «=====» o con «ISSUE #9 · …»
  nunca parece otro issue). Los caracteres de control se neutralizan y los que invierten el
  sentido del texto se escriben a la vista. La última línea, «FIN DEL HISTORIAL», dice que el
  archivo está completo.
- **Seguro**: el token solo va a la API de GitHub (nunca a una redirección ni a un enlace de otro
  servidor); funciona detrás del proxy del sistema; una respuesta desmesurada o una «bomba»
  comprimida no llenan la memoria; nunca pisa un archivo tuyo; se propone guardarlo fuera del
  proyecto y, si lo guardas dentro, queda fuera de la publicación. Puede tachar los secretos
  que alguien pegó en un issue o en el código comentado.
- Lo que GitHub no da y por eso no está: quién puso cada reacción (solo cuántas), las versiones
  anteriores de un texto editado (solo cuándo se cambió por última vez) y lo borrado.

```
python -m gitpush_studio.cli issues-txt ana/app -o app-historial.txt --mentions --redact
```

## Qué cambia en 3.3

### Aplicar un TXT de vuelta (📥 Aplicar TXT · `apply-txt`)

Le pasas el TXT a una IA, te devuelve los archivos cambiados («MODULO: ruta» y el contenido,
con o sin ``` de Markdown, con su charla alrededor) y lo pegas aquí. Antes de tocar nada
enseña cada archivo como **nuevo**, **modificado** (con «Ver cambios»: las líneas que entran y
salen), **igual** o **no se aplica** con su motivo. Lo que garantiza, con prueba cada cosa:

- **Nunca fuera de la carpeta**: rutas absolutas, «..», `.git`, enlaces simbólicos o uniones de
  Windows en el camino (aunque apunten dentro), nombres que solo cambian en mayúsculas y
  nombres que Windows no admite se rechazan.
- **Nunca borra ni vacía**: las notas del TXT («binario omitido», «borrado», un trozo de un
  archivo partido) no son contenido y no se aplican; un cuerpo vacío no vacía un archivo.
- **Lo que no cambió no cambia**: se modifica línea a línea con un algoritmo de diferencias
  propio (Myers). Cada línea igual se copia byte a byte del original —su final de línea, sus
  espacios del final, su codificación, hasta sus bytes rotos— y solo las líneas nuevas se
  escriben, con el final de línea y la codificación del archivo (UTF-8, UTF-16, ANSI, BOM).
  Una IA que quita espacios o cambia los `\r\n` no provoca ningún cambio.
- **Copia exacta antes**: en un repositorio, una rama `respaldo-FECHA` (no se publica; se
  recupera desde «⏪ Versiones»); fuera de git, una carpeta en `~/.gitpush_studio/respaldos`.
- **Si algo cambió entre revisar y aplicar** (lo editaste, apareció un archivo, alguien puso
  un enlace), no se escribe ninguno.
- **Lo que parece un resumen de la IA** («# … resto del código igual …» en lugar de cien
  líneas) o una respuesta cortada al final se marca «⚠ revisar» y no viene marcado.
- **Los secretos tachados vuelven a ser los reales** (ver abajo): una línea con «••••••••» se
  empareja con la del original; si no corresponde a ninguna, ese archivo no se aplica.

### El TXT para una IA (📄 Consolidar a TXT · `pack`)

- **Secretos tachados** (viene marcado): cada valor que la revisión de seguridad da por
  secreto se escribe «••••••••» (`TOKEN = "••••••••"`, `postgres://ana:••••••••@host`, el cuerpo
  de una llave privada). En los archivos de claves por nombre (.env, credentials.json…) se
  tachan todos los valores, y si no tienen forma «clave=valor», el contenido entero. También
  en los archivos enormes que se copian por bloques (una llave nunca queda partida).
- **Sin lo que no aporta**: archivos de bloqueo de dependencias, minificados, generados y
  código de terceros salen como una nota «<OMITIDO PARA IA…>» con su tamaño.
- **Solo lo que cambió** desde la última publicación (el último commit): enteros (se pueden
  aplicar de vuelta) o solo sus diferencias («DIFERENCIAS: ruta», que nunca se aplican). El
  árbol sigue siendo el del proyecto completo.
- **Tokens y partes**: dice cuántos tokens ocupa más o menos y lo reparte en partes que quepan
  en el contexto de la IA (32.000 a 1.000.000 de tokens), sin partir nunca un archivo salvo
  que él solo no quepa (entonces sus trozos llevan «SIGUE EN…» / «VIENE DE…»). Las partes
  van a una carpeta «nombre-partes»; las viejas se borran solo si son nuestras.
- **📋 Copiar**: el TXT al portapapeles, o parte a parte (cada pulsación, la siguiente).
- **Más rápido en repositorios grandes**: lo que no cambió se lee de git con un solo proceso
  (en Windows, abrir miles de archivos es lento por el antivirus). El resultado es idéntico byte
  a byte: solo se usa si git garantiza que es lo mismo que hay en la carpeta.

### Seguridad

- **Nunca en el repositorio equivocado**: si la carpeta ya publica en un repositorio (su
  «origin») y el formulario dice OTRO —cargaste otro perfil, soltaste otra carpeta, la URL se
  quedó la del proyecto anterior—, antes se cambiaba el «origin» sin avisar y el proyecto se
  subía allí (en modo espejo, ese repositorio quedaba igual que la carpeta). Ahora, antes de
  escribir nada, pregunta: **publicar en el de la carpeta** (la opción destacada; el formulario
  pasa a decir ese), en el del formulario o en ninguno (cerrar = ninguno). La auto-publicación
  no pregunta: se pone en pausa. En la terminal pregunta, y con `--yes` exige además
  `--switch-remote`. «Traer cambios» avisa si vas a traer de otro repositorio, y «Crear
  versión» se niega. El mismo repositorio escrito de otra forma (https o ssh, mayúsculas,
  «.git», un token dentro) no cuenta como otro, y un token metido en la URL no se enseña nunca.
- **Cambiar de carpeta pausa la auto-publicación**: antes, la ronda siguiente pasaba a vigilar
  y publicar la carpeta nueva con la URL de la anterior. Al abrir una carpeta que ya publica en
  un repositorio, una URL vacía se rellena con la suya; una distinta se avisa. En «Publicar
  varias carpetas», el nombre propuesto es el del repositorio en el que ya publica cada una.
- **🕵 Revisar historial** (`scan-history`): busca secretos en TODOS los commits de ramas y
  etiquetas (no en los respaldos locales), cada contenido una sola vez, y dice en qué commit
  entró y si sigue en la versión actual. Y lo importante: una clave que llegó a GitHub hay que
  cambiarla en su servicio, porque borrarla no la quita del historial.
- **Más de 100 MB con Git LFS**: en la revisión, «📦 Subir con Git LFS» los pasa a LFS (una línea
  por archivo en el `.gitattributes`). Todo o nada: si alguno ya está en un commit anterior, se
  explica y no se toca nada. Lo que ya va por LFS no vuelve a avisar.
- **❓ ¿Por qué no se sube?** (`why`): la regla exacta que deja fuera un archivo — el
  `.gitignore` y su línea, el de una subcarpeta, la lista privada, tu lista global de git,
  otro repositorio dentro, una carpeta vacía…

### Comodidad y velocidad

- **Auto-publicación en tiempo real**: el sistema avisa de cada cambio (Windows y Linux, por
  ctypes) y se comprueba unos segundos después; la ronda cada X minutos queda de respaldo.
- **git más rápido**: en carpetas grandes activa `core.untrackedCache` (solo en discos donde es
  fiable) y `core.fsmonitor` (Windows y macOS), nunca si ya los configuraste tú.
- **«Ver» en la vista previa**: las líneas exactas que cambian en cada archivo.
- **Arrastrar y soltar** (Windows): una carpeta pasa a ser la del proyecto; un TXT abre
  «Aplicar TXT». **Atajos**: Ctrl+Enter publica, F5 analiza, Ctrl+S guarda el perfil, Ctrl+T
  consolida, Ctrl+R aplica un TXT, F1 la ayuda.
- **Windows**: icono en la bandeja con avisos mientras la auto-publicación vigila (al minimizar
  se guarda ahí), abrir la app al iniciar Windows vigilando la última carpeta, aviso de
  versiones nuevas (si en Opciones indicas el repositorio de la app) y un **.exe sin Python**:
  `python tools/construir_exe.py`, o el flujo de GitHub Actions que lo construye y lo adjunta a
  la release al publicar una etiqueta `v…`.

## Qué cambia en 3.2 (auditoría de seguridad de los datos)

Cada punto es un fallo **confirmado ejecutándolo**, corregido y con su prueba de regresión.

- **Publicar en agregar/editar pisaba archivos ignorados**: si GitHub tenía un `config.env` y tú
  tenías el tuyo (ignorado), el merge lo sobrescribía sin avisar y tu versión se perdía (no
  estaba en ningún commit). Ahora se aparta durante el merge, vuelve intacto y además queda
  una copia exacta.
- **El respaldo se publicaba**: era un commit sobre tu rama, y al publicar subía a GitHub lo que
  nunca pasó por la vista previa ni la revisión (borradores, un `.env` no ignorado). Ahora es
  una rama aparte, exacta e invisible para la publicación.
- **Traer cambios y restaurar perdían archivos ignorados** que la operación pisaba (git los
  trata como desechables), y la copia no era byte a byte (filtros de fin de línea, hooks).
- **Mantener un borrado en la vista previa** podía borrar un archivo tuyo que estaba donde hacía
  falta una carpeta.
- **Excluir un archivo ya publicado en modo agregar/editar** lo borraba de GitHub sin que la
  vista previa lo dijera.
- **Una etiqueta con el nombre de la rama** hacía que se combinara otra historia al publicar.
- **El TXT podía escribir FUERA del proyecto** a través de un enlace roto con su nombre (un
  repositorio descargado con `tool.txt → ~/.ssh/authorized_keys`), **copiar una clave privada
  tuya** si un archivo era un enlace a ella, **colgarse para siempre** con una tubería (FIFO),
  romperse con nombres que no son UTF-8 dejando el TXT anterior destruido, y `pack`/«Generar
  TXT» **sobrescribían un .txt tuyo**. Ahora: no se sigue ningún enlace, no se abre nada que no
  sea un archivo normal, la escritura es atómica y nunca encima de algo que no sea un TXT de la
  app.
- **El TXT incluía lo que mandaste excluir** con `/**/x`, con un `.gitignore` guardado con BOM o
  con un `.gitignore` en una subcarpeta (carpetas sin repositorio).
- **Revisión de secretos**: un ejemplo encima de la contraseña real la tapaba (y se publicaba en
  automático); una contraseña con un `$` dentro, o con «test» o «here» en medio, se tomaba por
  un valor de ejemplo (y en automático también pasaba); UTF-16 y archivos de más de 1 MB no se
  leían; faltaban formatos actuales; 300 avisos por nombre dejaban el contenido sin mirar.
  Además, muchos menos avisos falsos.
- **Configuración**: `profiles` con un tipo inesperado se vaciaba y el siguiente guardado borraba
  los perfiles para siempre; `last_profile`/`last_state` raros impedían arrancar; un BOM del Bloc
  de notas se tomaba por archivo roto. Ahora se valida, se copia el original y nunca se guarda
  encima de lo que no se pudo leer ni apartar.
- **Red y procesos**: el token se mandaba a cualquier servidor del remoto (ahora solo a
  github.com) y seguía las redirecciones de urllib a otro servidor; un git colgado sin escribir
  nada ignoraba el tiempo máximo; una respuesta que no era JSON o enorme tumbaba la operación;
  un repositorio raro tumbaba una descarga en lote; una instantánea a medias (disco lleno)
  quedaba marcada como completa; un nombre `C:algo` dentro de un .tar.gz apuntaba fuera del
  destino en Windows.
- **Actualizar un clon descargado** podía pisar archivos ignorados o mover la rama equivocada si
  estaba en otra; ahora se aparta y lo explica. Y lo comprobado al empezar se vuelve a mirar
  después de la descarga (que puede tardar minutos): si tocaste algo entre medias, no se pisa.
- **Un repositorio dentro del proyecto se borraba entero**, historial incluido, al traer cambios
  en modo espejo, al restaurar una versión, al publicar en agregar/editar o al actualizar un
  clon, si la otra versión tenía un archivo con su nombre (`git reset --hard` y `git merge` lo
  hacen sin preguntar; comprobado). Ahora se detecta antes y no se toca nada.
- **Publicar un proyecto con otro repositorio dentro** subía un enlace roto (una carpeta que no
  se abre en GitHub, sin un solo archivo) y decía «publicado». Ahora esa carpeta se queda fuera
  y se explica.
- **Instantáneas**: si una descarga en una carpeta vacía fallaba, la «limpieza» borraba todo lo
  que hubiera dentro, también lo que guardaste ahí mientras se descargaba. Ahora se extrae en
  una carpeta temporal al lado y solo se coloca si sale entera; si el destino dejó de estar
  vacío, no se toca.
- **Un cambio guardado mientras se traían cambios o se restauraba** (después de la copia de
  seguridad) se perdía. Ahora se detecta justo antes de tocar nada y se para.
- **Nombres que git lee como patrones**: un archivo llamado `:!a.txt` significa para git «todo
  menos a.txt». Mantener su borrado en la vista previa ejecutaba `checkout FETCH_HEAD --` con
  ese nombre y **pisaba todos tus archivos modificados** con la versión de GitHub; excluirlo
  dejaba de publicar el proyecto entero (y el modo espejo lo borraba de GitHub). Ahora toda
  ruta va a git como literal (`--literal-pathspecs`).
- **Un repositorio dentro del proyecto sin ningún commit** hacía fallar `git add` y no se podía
  publicar nada; ahora se deja fuera antes de añadir.
- **Analíticas**: buscar duplicados abría los enlaces del proyecto siguiéndolos; uno que
  apuntara a una tubería o a `/dev/tty` dejaba el análisis colgado sin poder detenerlo.
- **macOS**: el token iba como argumento de `security` (visible con `ps`); ahora va por stdin.
- **Auto-publicación**: un fallo inesperado en el hilo de comprobación la paraba en silencio.

## Cómo se sabe que las pruebas sirven (pruebas de mutación)

Una prueba que pasa no demuestra nada si también pasaría con el código roto. `tools/mutaciones.py`
rompe el código **a propósito**, un fragmento cada vez, y exige que alguna prueba falle: quitar
la comprobación byte a byte antes de borrar, borrar carpetas con contenido, dejar de guardar
en la copia lo ignorado que se va a pisar, aplicar lo que GitHub tiene aunque haya cambiado
desde «Comprobar», seguir un enlace al escribir el TXT, pasar a git un nombre como patrón,
mandar el token a otro servidor… Cada mutación que «sobrevive» es una prueba que falta.

Resultado con esta versión: **208 de 208 mutaciones detectadas** (`tools/mutaciones_seguridad.json`:
copias de seguridad, borrado verificado, traer cambios, restaurar, publicar, vista previa, el
TXT, el lector, la revisión de secretos, la red, las descargas y qué proyectos del lote
están marcados para publicarse). Las 16 nuevas de la 3.4 defienden el volcado de issues y pull
requests: que el token nunca siga una redirección ni un enlace a otro servidor, que una respuesta
desmesurada o una bomba gzip no llenen la memoria (el tope se aplica mientras se descomprime),
que se espere a la cuota, que una conexión a medio leer no se reutilice, que reanudar vuelva a
pedir la página en curso y no tire lo descargado, que nunca pise un archivo tuyo ni escriba a
través de un enlace, que lo escrito por alguien nunca empiece en la primera columna, que no
pasen caracteres de control, que se tachen los secretos del código citado, que las respuestas
vayan en su hilo y que, guardado dentro del proyecto, no se publique. Las 51 de la 3.3 defienden aplicar un TXT (nunca fuera
de la carpeta ni a través de un enlace, nada si algo cambió entre revisar y aplicar, una nota
nunca vacía un archivo), los secretos tachados (que ninguno llegue al TXT, ni partido entre
bloques), las partes, el TXT leído de git, Git LFS (todo o nada), el historial (los respaldos
locales no cuentan como publicados), el vigilante, el git rápido (nunca pisa tu
configuración) y que nunca se publique en el repositorio equivocado (sin nadie que decida,
con la auto-publicación, con `--yes`, al cerrar la pregunta, al cambiar de carpeta a mitad de
una comprobación). Una pasada completa tarda ~15 min: las pruebas de tuberías desbloquean al
lector colgado en vez de esperar al tope de 15 minutos. Antes, de la 3.2: las 27 que defienden el TXT sin límites: que un archivo
grande se copie entero y solo hasta lo que medía al abrirlo, que un carácter partido entre
bloques no se pierda, que el ANSI no se confunda con un UTF-8 roto, que «Detener», el disco
lleno y la reserva de espacio dejen el TXT anterior intacto, que la lectura por delante tenga
tope y que el ritmo de hilos se decida midiendo. Otras 14 defienden lo que no se publica sin
`.gitignore`: que se guarde, que no se quede escrito en el `.gitignore`, que un cuadro vacío
no lo vuelva a publicar y que hacerlo pida confirmación, que un «!» no lo cuele, que no se
escriba a través de un enlace ni donde git no mira. Llegar ahí no fue gratis: las
primeras rondas dejaron vivas mutaciones que destaparon huecos reales (un respaldo sin el
submódulo, tres respaldos en el mismo minuto, operaciones a medias que nadie probaba, un
`$` que rebajaba contraseñas…), y cada una tiene ahora su prueba. Tras cambiar el código,
`python tools/mutaciones.py --comprobar` dice qué mutaciones hay que rehacer porque el
fragmento ya no existe.

## Qué cambió respecto a 1.5

- **Pipeline declarado** (`publish.STEPS`) con contexto propio por publicación → `publish_many`.
- **Git como fuente de verdad**: revisión con `git ls-files --cached --others --exclude-standard`
  (respeta .gitignore anidados y lo ya rastreado); `.gitignore` se escribe antes de revisar;
  auto-publicación con `git status --porcelain` (huella MD5 solo antes del primer `git init`).
- **Config**: un `profiles.json` ilegible se aparta como `.bak-FECHA` y se avisa; guardado atómico.
- **Token**: nunca en argv ni en `.git/config` (`GIT_CONFIG_*` por entorno, git ≥ 2.31);
  fuera de Windows va al llavero del sistema; si no hay, la app lo dice en vez de decir «cifrado».
- **Actualizar en vez de volver a bajar**: al repetir la descarga de un perfil, «Si ya está
  descargado: Actualizar» trae solo lo que cambió. En un clon, `fetch` (con `--depth 1` si la
  copia era ligera, para que siga siéndolo) y avance rápido; en una instantánea, un `ls-remote`
  compara el sha con el guardado en `.gitpush-instantanea.json` y, si no cambió, **no se descarga
  nada**. Nunca se pisa trabajo del usuario: una carpeta con cambios sin guardar o con commits
  propios se deja como está y se explica por qué, y al actualizar una instantánea la anterior se
  aparta como `repo.anterior-FECHA` en vez de borrarse.
- **Listado por GraphQL**: con token, los repositorios de un perfil se piden con una sola consulta
  (100 por página, campos exactos y el login del dueño incluido), en vez de `/user` + N páginas
  REST. GraphQL exige autenticación, así que sin token se usa REST; y si GraphQL falla por lo que
  sea, se vuelve a REST sin molestar al usuario. La consola dice por qué vía fue y cuántas
  peticiones costó.
- **Dos formas de bajar un perfil**: *Clonar (con git)*, la de siempre, deja repositorios
  completos desde los que se puede publicar, traer cambios y ver versiones; *Instantánea
  (.tar.gz)* baja el comprimido de cada repositorio y extrae solo los archivos, mucho más rápido
  y ligero (una petición HTTP, sin packfile y sin crear `.git`), pero esas carpetas no son
  repositorios. La primera puerta es `codeload.github.com`, que no consume peticiones de la API;
  si falla, se intenta el endpoint documentado `/repos/{owner}/{repo}/tarball/{ref}`, que sí las
  consume pero funciona con repositorios privados. La extracción rechaza rutas absolutas, `..`,
  enlaces y cualquier miembro que se salga de la carpeta de destino.
- **Clonación masiva**: «Detener» mata los clones en curso y limpia carpetas a medias. Un fallo
  pasajero (corte de red, servidor ocupado, `429`) se reintenta hasta dos veces con espera
  creciente, y un «baja el ritmo» de GitHub pausa **toda la cola** 20 s en vez de que los demás
  sigan insistiendo; «Detener» interrumpe esa pausa al instante. Los fallos que no se arreglan
  insistiendo (token inválido, repositorio inexistente) fallan a la primera.
- **Nombres que parecían patrones**: al excluir un archivo concreto (vista previa, revisión o
  el TXT del proyecto) su nombre se escribe escapado, como hace git. Sin escapar,
  `datos[2024].csv` se leía como «uno de estos caracteres», no coincidía con el archivo y
  **se subía igualmente**; ahora el patrón es `/datos\[2024\].csv` y excluye ese archivo y
  solo ese. Se escapan también la barra invertida y el espacio final, y la ruta ya no se
  recorta: `informe final .txt` apuntaba a `informe final`, es decir a otro archivo. Lo que
  escribes tú en el cuadro de ignorar no se toca —ahí `*.log` sigue siendo un comodín—, pero
  si escribes el nombre literal de un archivo que existe y lleva comodines, la consola te lo
  dice y te enseña cómo escribirlo escapado, en vez de no hacer nada en silencio.
- **El cuadro de ignorar habla el idioma de git**: `**/`, `*` y `?` que no cruzan la barra,
  clases `[Dd]`, `[!x]` y escapes se interpretan con las reglas de git, no con las de
  `fnmatch`. Antes `**/.env` se escribía en el `.gitignore` (git lo respetaba) pero el
  comparador propio no lo entendía, así que un `.env` **ya publicado no se desrastreaba** y
  seguía en GitHub, y además **entraba en el TXT**, que es el archivo donde más duele. Para
  decidir qué deja de subirse se pregunta directamente a git
  (`ls-files --cached --ignored --exclude-from`), y `tests/test_ignore_vs_git.py` compara
  archivo por archivo, contra `git check-ignore` real, lo que dicen los dos.
- **El TXT no pisa nada tuyo**: el nombre por defecto es el de la carpeta, así que un
  proyecto «notas» con su propio `notas.txt` lo habría perdido —`open(…, "wb")` no pregunta—
  y encima el `.gitignore` lo habría sacado de GitHub: las dos copias de un clic. Ahora se
  comprueba antes; si el archivo no lo generó la aplicación (se reconoce por su cabecera), el
  TXT se guarda como `notas-unificado.txt` y se avisa. Rehacer un TXT propio sí lo
  sobrescribe, que es lo que se quiere. El destino se resuelve una sola vez siguiendo los
  enlaces simbólicos: un atajo que apuntara dentro del proyecto dejaba el TXT ahí sin
  excluirlo, y se subía.
- **Publicar nunca destruye otra rama local**: «la carpeta manda» renombra la rama actual al
  destino con `branch -M`, que es `--force`. Si ya existía una rama local con ese nombre y sus
  propios commits, desaparecía y solo quedaba en el reflog. Ahora se aparta primero como
  `main-apartada-FECHA` y se avisa.
- **Archivos con nombres que no son UTF-8**: git los sube, pero la revisión de seguridad no
  puede leerlos y el TXT no puede incluirlos. Antes se descartaban en silencio y la revisión
  decía «✔ sin hallazgos»; ahora la consola dice cuántos hay y sugiere renombrarlos.
- `LC_ALL=C` en git: los mensajes se reconocen en cualquier idioma.
- Doce diálogos sobre dos clases base; un solo camino de publicación para la individual,
  la múltiple y la automática.
