# GitPush Studio 3.2.0 🧐

Publica, clona y empaqueta tus proyectos de GitHub sin tocar la terminal;
varias carpetas a la vez a tu misma cuenta, cada una con su TXT; y ahora
también **analiza tus proyectos** (lenguajes, líneas, duplicados,
historial) con un informe HTML que no se conecta a nada.

La 3.2 es sobre todo una versión de **seguridad de los datos**: una
auditoría completa encontró fallos que podían borrar o publicar archivos
tuyos sin permiso, y están todos corregidos y defendidos por pruebas
(ver «Qué cambia en 3.2»).

```
gitpush_studio/
├── __init__.py          nombre y versión
├── __main__.py          python -m gitpush_studio  → interfaz gráfica
├── cli.py               python -m gitpush_studio.cli → publish | batch | scan | pack | analyze
│                        | status | pull | versions | restore | repos | branches | release
├── core/                cero tkinter, cero dependencias externas
│   ├── errors.py        StepError · Cancelled · Blocked
│   ├── fs.py            tamaños, binarios, rutas, nombres que no son UTF-8
│   ├── desktop.py       abrir un archivo · «abrir ubicación» con el archivo seleccionado
│   │                    (shell de Windows por ctypes · Finder · FileManager1 por D-Bus)
│   ├── atomic.py        escrituras atómicas · creación exclusiva · temporales olvidados
│   ├── urls.py          parseo de URLs/usuarios de GitHub
│   ├── refnames.py      nombres de rama válidos con las reglas de git (sin procesos)
│   ├── ignore.py        IgnoreMatcher (reglas de git) y edición del .gitignore (solo añade)
│   ├── batch.py         varias carpetas a una misma cuenta: validar · resolver URLs · plan
│   ├── reader.py        lectura por lotes con memoria acotada; nunca sigue enlaces ni abre tuberías
│   ├── textenc.py       ¿texto o binario? UTF-8 / UTF-16 (con y sin BOM)
│   ├── walk.py          recorrido scandir (.gitignore anidados) · project_entries · árbol · huella
│   ├── secret_rules.py  qué es un secreto: formatos de proveedor, nombres, jueces de valores
│   ├── scan.py          revisión de seguridad: todo el contenido, por trozos, con presupuesto
│   ├── snapshot.py      descarga por .tar.gz (sin git) con extracción segura y todo-o-nada
│   ├── preamble.py      presentación en prosa y resumen final de los TXT
│   ├── pack.py          consolidar a TXT (proyecto, cada repo, perfil entero), atómico
│   ├── net.py           HTTP con redirecciones seguras, tope de tamaño y Retry-After
│   ├── git.py           Git (run/stream/read_blobs/commit_tree) · parsers puros
│   │                    · auth por entorno (solo github.com) · clone_repo · BulkCloner
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
│   ├── sync.py          traer cambios de GitHub: inspect (qué pasaría en cada modo) · apply
│   ├── history.py       list_versions · restore_version · restore_backup · describe_changes
│   ├── release.py       suggest_tag · create_release (etiqueta + Release + zip)
│   ├── languages.py     59 lenguajes: comentarios, cadenas, docstrings, extensiones, shebangs
│   ├── linecount.py     código / comentario / en blanco enmascarando con expresiones en C
│   ├── gitstats.py      historial en flujo: personas (unión de alias), meses, horas, archivos
│   ├── analytics.py     análisis del proyecto: lenguajes, generado, duplicados, TODO
│   └── report_html.py   informe HTML autónomo (CSP estricta, sin red) y JSON
├── informe/plantilla.html   el informe: gráficos SVG propios, tema claro/oscuro, accesible
└── ui/                  solo widgets y callbacks
    ├── theme.py · widgets.py · base.py · console.py · help.py · jobs.py
    ├── app.py           ventana principal: compone las piezas de abajo
    ├── app_form.py      el formulario (estado, licencia, modo, ignorar)
    ├── app_profiles.py  perfiles guardados y token
    ├── app_dialogs.py   abrir cada herramienta una sola vez
    ├── app_publish.py   analizar y publicar (una, marcadas, varias carpetas)
    ├── app_auto.py      auto-publicación
    └── dialogs/         batch · preview · review · clone · bulk_clone · consolidate · pages
                         · create_repo · sync · history · branches · release · analytics
tools/
├── benchmark.py         proyecto sintético grande y tiempos de cada operación
├── mutaciones.py        pruebas de mutación: rompe el código a propósito y exige que las
│                        pruebas lo detecten
└── mutaciones_seguridad.json   las mutaciones del código que borra, pisa o publica archivos
tests/                   600 pruebas; las de seguridad comparan FOTOS completas de carpeta
                         y remoto antes/después (nada cambia fuera de lo aprobado)
├── test_safety.py       publicar/traer/restaurar nunca borra ni pisa nada sin permiso
├── test_backups.py      copias exactas, invisibles, verificadas antes de borrar
├── test_pack_safety.py  el TXT no escribe fuera, no sigue enlaces, no se cuelga
├── test_scan.py         lo que la revisión se dejaba y lo que marcaba de más
├── test_ignore_vs_git.py    patrones y .gitignore anidados contra git real
├── test_fuzz.py         fuzzing propio de todo lo que lee datos ajenos
├── test_net.py          token solo a GitHub, redirecciones, respuestas rotas, procesos colgados
└── …                    flujo, analíticas, licencias, interfaz sin pantalla, etc.
```

## Ejecutar

```
pip install customtkinter          # solo para la interfaz gráfica
python -m gitpush_studio           # interfaz
python -m unittest discover -s tests -t . -v
python -m unittest tests.test_safety tests.test_backups -v     # seguridad de datos
python tools/benchmark.py --files 20000                         # rendimiento
python tools/mutaciones.py                 # ¿las pruebas detectan los fallos? (~25 min)

python -m gitpush_studio.cli publish --profile web --profile api --profile docs
python -m gitpush_studio.cli publish --folder ./mi-app --url https://github.com/yo/mi-app.git --yes
python -m gitpush_studio.cli batch --project ./web --project ./api --project "./docs=mis-docs"
python -m gitpush_studio.cli scan ./mi-app
python -m gitpush_studio.cli pack ./mi-app -o mi-app.txt      # nunca encima de un .txt tuyo
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

Rendimiento (`tools/benchmark.py`, 20.000 archivos, 62 MB): recorrido 0,2 s, revisión 6 s,
TXT 2,6 s, analíticas 8 s, copia exacta 1,2 s.

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
propio `.gitignore`, su revisión de seguridad y su vista previa. Cuando dos necesitan tu
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
del `.gitignore`, porque el TXT **nunca se sube**: su ruta se añade a los patrones de
ignorar antes de generarlo —de modo que ni un TXT a medio escribir llega a GitHub— y queda
escrita en el `.gitignore`, así que sigue excluido en las publicaciones siguientes y aunque
se hubiera subido antes por error (entonces se le quita el seguimiento y **sigue en tu
carpeta**). Respeta el cuadro de ignorar: lo que dejaste fuera no se copia dentro del TXT.
Si generarlo falla, se avisa y la publicación continúa: el TXT es un extra, no el objetivo.

El TXT lleva **todo tu código junto en un archivo**: guárdalo como guardarías el proyecto.

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

Resultado con esta versión: **92 de 92 mutaciones detectadas** (`tools/mutaciones_seguridad.json`:
copias de seguridad, borrado verificado, traer cambios, restaurar, publicar, vista previa, el
TXT, el lector, la revisión de secretos, la red y las descargas). Llegar ahí no fue gratis: las
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
