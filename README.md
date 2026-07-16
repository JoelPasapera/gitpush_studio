# 🚀 GitPush Studio

> **Publica cualquier proyecto en GitHub sin usar la terminal.**

GitPush Studio es una aplicación de escritorio desarrollada en **Python** que simplifica por completo el proceso de publicar proyectos locales en GitHub. Está diseñada especialmente para usuarios que prefieren una interfaz gráfica en lugar de recordar comandos de Git.

Con unos pocos clics podrás inicializar un repositorio, configurar Git, crear commits, clonar repositorios, clonar todos los repositorios de un usuario, enlazar un repositorio remoto y publicar tu proyecto de forma segura.

---

# ✨ Características

## 📁 Publicación completa con un clic

GitPush Studio automatiza todo el flujo habitual de Git:

* Inicializar repositorio (`git init`)
* Configurar usuario y correo
* Crear o actualizar `.gitignore`
* Agregar todos los archivos (`git add -A`)
* Crear commits
* Configurar la rama principal
* Agregar o actualizar el repositorio remoto
* Publicar automáticamente en GitHub

Todo ello sin abrir la terminal.

---

## 🛡 Revisión de seguridad integrada

Antes de publicar, la aplicación analiza automáticamente el proyecto para detectar posibles problemas.

### Detecta archivos demasiado grandes

* Archivos mayores de **100 MB** (GitHub los rechaza).
* Advertencias para archivos entre **50 MB y 100 MB**.

---

### Detecta posibles secretos

Busca automáticamente archivos sensibles como:

* `.env`
* `.env.*`
* `id_rsa`
* `id_ed25519`
* `*.pem`
* `*.key`
* `*.pfx`
* `credentials.json`
* `service-account.json`
* `secrets.json`
* `.npmrc`
* `.netrc`

También analiza el contenido de archivos de texto para detectar:

* Tokens de GitHub
* Claves de AWS
* Claves de Google
* Tokens de Slack
* Claves API
* Contraseñas escritas directamente en el código
* Llaves privadas

Si encuentra algún riesgo, permite excluir esos archivos con un solo clic antes de realizar el push.

---

## 🧩 Gestión de perfiles

Guarda configuraciones completas para reutilizarlas posteriormente.

Cada perfil almacena:

* Carpeta del proyecto
* Usuario Git
* Correo
* URL del repositorio
* Rama principal
* Mensaje del commit
* Configuración del `.gitignore`
* Opciones de publicación

Ideal para trabajar con múltiples proyectos.

---

## 📦 Creación de repositorios desde la aplicación

No es necesario abrir GitHub.

La aplicación permite crear repositorios directamente utilizando la API oficial de GitHub.

Incluye soporte para:

* Repositorios públicos
* Repositorios privados
* Descripción del proyecto
* Almacenamiento opcional del token personal

---

## 🔐 Almacenamiento seguro del token

En Windows, el token se protege utilizando **DPAPI**, por lo que únicamente puede ser utilizado por la cuenta del usuario que lo almacenó.

En otros sistemas se almacena codificado para facilitar su reutilización.

---

## 📋 Consola integrada

Cada paso del proceso se muestra en tiempo real:

* Comandos ejecutados
* Salida de Git
* Advertencias
* Errores
* Estado de la publicación

Esto permite entender exactamente qué está haciendo la aplicación.

---

## 🎨 Interfaz moderna

La interfaz está inspirada en el modo oscuro de GitHub e incluye:

* Barra lateral para perfiles
* Barra de progreso
* Consola integrada
* Indicadores visuales de estado
* Ventanas de revisión
* Colores consistentes con GitHub

---

# 📷 Flujo de trabajo

1. Selecciona la carpeta del proyecto.
2. Configura tu usuario de Git (opcional).
3. Introduce la URL del repositorio o crea uno nuevo.
4. Escribe el mensaje del commit.
5. Pulsa **Publicar en GitHub**.
6. GitPush Studio analiza automáticamente el proyecto.
7. Si todo está correcto, realiza el commit y publica el proyecto.

---

# ⚙️ Requisitos

* Python 3.9 o superior
* Git instalado
* customtkinter

Instalación de la dependencia:

```bash
pip install customtkinter
```

---

# ▶️ Ejecución

```bash
python gitpush_studio.py
```

---

# 📂 Configuración

Los perfiles de usuario se almacenan en:

```
~/.gitpush_studio/profiles.json
```

---

# 🧠 Tecnologías utilizadas

* Python
* Tkinter
* CustomTkinter
* Git
* GitHub REST API
* threading
* subprocess
* urllib
* pathlib

---

# 📌 Casos de uso

GitPush Studio resulta especialmente útil para:

* Estudiantes que están aprendiendo Git.
* Desarrolladores que prefieren una interfaz gráfica.
* Profesores que enseñan control de versiones.
* Usuarios que desean evitar errores al utilizar la terminal.
* Personas que publican proyectos con frecuencia.

---

# ⚠️ Nota de seguridad

Aunque GitPush Studio analiza automáticamente archivos potencialmente sensibles, siempre es recomendable revisar el contenido del proyecto antes de publicarlo.

Nunca compartas:

* Contraseñas
* Tokens
* Claves privadas
* Archivos de configuración con información confidencial

---

# 📄 Licencia

Este proyecto se distribuye bajo la licencia **MIT**.

Puedes utilizarlo, modificarlo y distribuirlo libremente respetando los términos de dicha licencia.

---

# ❤️ Contribuciones

Las contribuciones son bienvenidas.

Si encuentras un error, tienes una sugerencia o deseas mejorar la aplicación, puedes abrir un **Issue** o enviar un **Pull Request**.

---

# ⭐ Si este proyecto te resulta útil...

¡Considera dejar una estrella ⭐ en el repositorio!

Es una excelente forma de apoyar el desarrollo del proyecto y ayudar a que más personas lo descubran.
