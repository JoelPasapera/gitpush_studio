# GitPush Studio 2.0

Publica, clona y empaqueta tus proyectos de GitHub sin tocar la terminal.
Ahora también **varios proyectos a la vez**, cada uno a su repositorio.

```
gitpush_studio/
├── __init__.py          nombre y versión
├── __main__.py          python -m gitpush_studio  → interfaz gráfica
├── cli.py               python -m gitpush_studio.cli → publish | scan | pack | status
├── core/                cero tkinter, cero dependencias externas
│   ├── errors.py        StepError · Cancelled · Blocked
│   ├── fs.py            tamaños, binarios, rutas
│   ├── urls.py          parseo de URLs/usuarios de GitHub
│   ├── ignore.py        IgnoreMatcher (con anclado «/» arreglado) y edición del .gitignore
│   ├── reader.py        lectura por lotes en paralelo con memoria acotada
│   ├── walk.py          recorrido scandir · project_entries (git como fuente de verdad) · árbol · huella
│   ├── scan.py          revisión de seguridad sobre cualquier lista de archivos
│   ├── pack.py          consolidar a TXT (proyecto, cada repo, perfil entero)
│   ├── git.py           Git (run/stream/list_files/status_changes/diff_vs_remote) · parsers puros
│   │                    · auth por entorno · clone_repo · BulkCloner cancelable
│   ├── github.py        API mínima con reintento y límite de peticiones · repos · Pages
│   ├── secrets.py       DPAPI / llavero macOS / secret-tool / b64 (avisable)
│   ├── config.py        carga con copia de seguridad · guardado atómico · token
│   ├── publish.py       PublishJob · Callbacks · STEPS · publish · publish_many
│   ├── sync.py          traer cambios de GitHub: inspect (qué cambiaría) · apply (merge | mirror)
│   ├── history.py       list_versions · restore_version · describe_changes (mensaje automático)
│   └── release.py       suggest_tag · create_release (etiqueta + Release + zip)
└── ui/                  solo widgets y callbacks
    ├── theme.py         paleta y fuentes
    ├── widgets.py       fábricas de widgets (button, card, entry, checkbox…)
    ├── base.py          BaseDialog · DecisionDialog · WorkerDialog
    ├── console.py       consola de solo lectura: seleccionable, copiable, sin arrastrar la vista
    ├── help.py          qué hace cada botón (lo imprime la consola al abrir y con «❔ Ayuda»)
    ├── jobs.py          PublishController (decisiones en serie) · QueuePanel
    ├── app.py           ventana principal
    └── dialogs/         preview · review · clone · bulk_clone · consolidate · pages · create_repo
                         · sync · history · branches · release
tests/
├── gitenv.py                repositorios de prueba con rama e identidad fijadas, para que el
│                            resultado no dependa de la configuración git del equipo
├── test_tk_collisions.py    ningún widget puede tapar un nombre de tkinter (`_bind`, `update`…)
├── test_safety.py           **seguridad de los datos**: fotos completas de carpeta y remoto
│                            antes/después; nada cambia fuera de lo aprobado
├── test_pure.py             sin git: ignore, urls, parsers, config, fs
├── test_git_and_publish.py  con git real y repositorios bare locales como «GitHub»
├── tkstub.py                sustitutos de tkinter/customtkinter sin pantalla
└── test_ui_headless.py      la interfaz completa, sin pantalla, contra git real
```

## Ejecutar

```
pip install customtkinter          # solo para la interfaz gráfica
python -m gitpush_studio           # interfaz
python -m unittest discover -s tests -t . -v
python -m unittest tests.test_safety -v      # solo las de seguridad de datos

python -m gitpush_studio.cli publish --profile web --profile api --profile docs
python -m gitpush_studio.cli publish --folder ./mi-app --url https://github.com/yo/mi-app.git --yes
python -m gitpush_studio.cli scan ./mi-app
python -m gitpush_studio.cli pack ./mi-app -o mi-app.txt
python -m gitpush_studio.cli status ./mi-app
python -m gitpush_studio.cli pull ./mi-app --url https://github.com/yo/mi-app.git --yes
python -m gitpush_studio.cli versions ./mi-app
python -m gitpush_studio.cli restore ./mi-app 272f003
python -m gitpush_studio.cli branches --url https://github.com/yo/mi-app.git
python -m gitpush_studio.cli release ./mi-app --url https://github.com/yo/mi-app.git --token ghp_… --tag v1.2.0
```

## Flujo local ↔ GitHub

- **Traer cambios de GitHub** (⬇⬆): primero enseña qué llegaría, qué cambiaría y qué
  desaparecería, y qué tienes sin publicar; al aplicar, tu versión actual queda en una rama
  «respaldo-FECHA» y luego se combina (lo tuyo se conserva; donde ambos tocaron lo mismo, gana
  GitHub) o se hace espejo (la carpeta queda exactamente como GitHub).
- **Versiones** (⏪): historial con archivos, líneas y respaldos; «Restaurar» pone la carpeta
  como en esa versión creando un commit nuevo (nada se reescribe) y, al publicar, GitHub la sigue.
- **Ramas** (🌿): lista las ramas del remoto y deja elegir o escribir el destino. La carpeta
  manda: la rama local pasa a llamarse como la elegida con el contenido tal cual, y lo que hay
  en GitHub en esa rama se combina al publicar. Nunca se hace checkout, que alteraría archivos.
- **Decidir archivo por archivo en la vista previa**: cada grupo tiene un botón por archivo y otro
  para todo el grupo.
  - *Se borrarán* → «Mantener»: sigue en GitHub; vuelve al índice desde la versión remota y, por
    defecto, también a tu carpeta (si lo desmarcas, se conserva solo esta vez). Si el archivo ya
    existe en la carpeta —por ejemplo porque está ignorado— **nunca se sobrescribe**: solo se
    devuelve la entrada al índice.
  - *Nuevos* → «No subir»: sale del índice (`git rm --cached`), el archivo sigue en tu PC y, por
    defecto, se añade al .gitignore para no volver a preguntar.
  - *Modificados* → «No actualizar»: el índice recupera la versión de GitHub y **tu archivo local
    no se toca**; como sigue siendo distinto, volverá a aparecer la próxima vez (para quedarte con
    la versión de GitHub está «⬇⬆ Traer cambios»).
  Todo viaja en un `PreviewChoice` junto a la respuesta de la vista previa: sin pasos extra, sin
  segundo `fetch`, y las órdenes de git se agrupan de 50 rutas en 50.
- **Mensaje automático**: «Añade 2 imágenes y actualiza 3 archivos en src/» a partir de lo
  preparado para el commit, con el detalle de rutas en el cuerpo.
- **Publicar versión** (🏷): etiqueta anotada + push de la etiqueta + GitHub Release, con zip
  opcional de lo publicado (`git archive`). Exige no tener cambios pendientes.

## Publicación múltiple

Cada perfil guardado ya describe un proyecto completo (carpeta, URL, rama, modo, ignorar,
identidad). En la barra lateral, marca los perfiles que quieras y pulsa «🚀🚀 Publicar
seleccionados»: cada proyecto recorre el pipeline completo con su propio `.gitignore`, su
revisión de seguridad y su vista previa. Los proyectos con remotos distintos van en
paralelo; los que comparten remoto, en serie. Cuando dos necesitan tu decisión, las
ventanas se muestran de una en una mientras los demás siguen avanzando. El panel «Cola de
publicación» enseña una fila por proyecto y permite detener (lo que ya está en marcha
termina; no se empieza nada nuevo).

## Qué cambia respecto a 1.5

- **Pipeline declarado** (`publish.STEPS`) con contexto propio por publicación → `publish_many`.
- **Git como fuente de verdad**: revisión con `git ls-files --cached --others --exclude-standard`
  (respeta .gitignore anidados y lo ya rastreado); `.gitignore` se escribe antes de revisar;
  auto-publicación con `git status --porcelain` (huella MD5 solo antes del primer `git init`).
- **Config**: un `profiles.json` ilegible se aparta como `.bak-FECHA` y se avisa; guardado atómico.
- **Token**: nunca en argv ni en `.git/config` (`GIT_CONFIG_*` por entorno, git ≥ 2.31);
  fuera de Windows va al llavero del sistema; si no hay, la app lo dice en vez de decir «cifrado».
- **Clonación masiva**: «Detener» mata los clones en curso y limpia carpetas a medias.
- `LC_ALL=C` en git: los mensajes se reconocen en cualquier idioma.
- Siete diálogos sobre dos clases base; un solo camino de publicación para la individual,
  la múltiple y la automática.
