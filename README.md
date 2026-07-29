# Agente EEQ — Descarga automática de facturas

Automatiza la descarga de facturas en PDF desde el portal de la [Empresa Eléctrica Quito](https://www.eeq.com.ec/consulte-su-factura). Incluye una interfaz web accesible desde cualquier dispositivo en la misma red o por internet vía Tailscale.

---

## Qué hace el agente

El agente opera un navegador Chromium real (no visible al usuario) y reproduce exactamente los pasos que haría una persona en el sitio de la EEQ:

1. **Abre el portal** `eeq.com.ec/consulte-su-factura` y cierra automáticamente el banner de cookies.
2. **Selecciona** el modo de búsqueda "Consulta por Cuenta Contrato".
3. **Ingresa** el número de cuenta contrato y ejecuta la consulta.
4. **Navega** a los documentos del servicio (detecta la fila correcta aunque haya varios servicios).
5. **Filtra por año** usando el combo "Buscar por año". Si seleccionas "Todos los años", detecta automáticamente qué años están disponibles en el portal y los itera uno por uno.
6. **Lee la tabla de documentos** con los campos: número de documento, fecha, número de factura, fecha de vencimiento, tipo y valor.
7. **Descarga cada factura en PDF**: hace clic en el botón de descarga de cada fila, completa el modal que aparece (selecciona "No soy titular") y espera la confirmación.
8. **Salta duplicados**: si una factura ya existe en disco, la omite sin volver a descargarla.
9. **Reintentos automáticos**: si una descarga falla, espera y reintenta hasta 2 veces. Si el sitio sigue fallando, recarga la consulta entera y continúa.
10. **Pausa entre lotes**: descarga N facturas seguidas, luego hace una pausa configurable antes del siguiente lote para no saturar el sitio y evitar bloqueos de IP.
11. **Detecta CAPTCHAs**: si el sitio presenta un reCAPTCHA o hCAPTCHA, pausa el proceso, muestra un aviso en la interfaz web, y espera a que el usuario lo resuelva manualmente en el navegador que se abrió.
12. **Procesa múltiples cuentas** en secuencia: la interfaz permite ingresar varias cuentas contrato y el agente las procesa una por una.
13. **Genera un ZIP automático** al finalizar con todas las facturas descargadas, organizado por cuenta y año.
14. **Guarda capturas de error** en la carpeta `debug/` cuando algo falla, para diagnóstico.

---

## Dónde se guardan los archivos

```
AgenteEEQ/
├── descargas_eeq/
│   ├── 2026/
│   │   ├── 001-999-123456789.pdf
│   │   └── ...
│   ├── 2025/
│   │   └── ...
│   └── ...
└── debug/
    └── 20260610_143022_captcha_detectado.png   ← capturas de error
```

El ZIP descargado desde la interfaz tiene la estructura:
```
facturas_eeq.zip
└── <cuenta>/
    └── <anio>/
        └── <nombre>.pdf
```

---

## Requisitos previos

- **Python 3.10 o superior** — verificar con `python3 --version`
- Conexión a internet
- El sitio de la EEQ debe estar accesible

---

## Instalación (primera vez)

**1. Instalar dependencias Python**
```bash
pip3 install -r requirements.txt
```

**2. Instalar el navegador que usa el agente**
```bash
playwright install chromium
```

> Descarga un Chromium (~170 MB). Solo se hace una vez.

**3. Verificar instalación**
```bash
python3 -c "from playwright.sync_api import sync_playwright; print('OK')"
```

---

## Iniciar el servidor web

```bash
python3 server.py
```

- Se abre automáticamente el navegador en `http://localhost:3002`
- El servidor queda activo mientras la terminal esté abierta
- Para detenerlo: `Ctrl + C`

---

## Acceso público con Tailscale Funnel (solo desarrollo local, ya no se usa en producción)

> **Nota**: desde la Fase 4 de `PlataformaWebProyectos`, el acceso público en producción se hace vía
> Caddy en el servidor Lightsail (ver "Despliegue en producción" más abajo), no por Tailscale. Esta
> sección queda como referencia para correr el agente localmente en tu Mac durante desarrollo.

Para exponer la app en internet bajo el mismo dominio que AgenteInformes, ejecuta **una sola vez** en la Mac servidor:

```bash
tailscale funnel --bg --set-path /eeq 3002
```

URL pública resultante:
```
https://airiss-mac-mini.taild79942.ts.net/eeq
```

Accesible desde cualquier dispositivo, sin Tailscale instalado.

Para verificar que las otras apps no fueron afectadas:
```bash
tailscale funnel status
```

> El flag `--bg` mantiene la regla activa aunque se cierre la terminal. Solo hay que ejecutar el comando una vez.

---

## Acceso desde otra PC (misma red WiFi)

1. Averigua la IP local de la Mac que ejecuta el servidor:
   ```bash
   ifconfig | grep "inet 192"
   ```
2. Desde cualquier otro dispositivo en la misma red, abre:
   ```
   http://192.168.X.X:3002
   ```

---

## Cómo usar la interfaz

1. **Cuentas contrato** — Ingresa el número de la esquina superior derecha de tu factura. Puedes agregar varias con `+ Agregar otra cuenta`.
2. **Año** — Selecciona el año o elige "Todos los años disponibles" para bajar todo el historial.
3. **Ajustes avanzados** *(opcional)* — Cambia los tiempos de espera sin tocar el código.
4. **Iniciar descarga** — El panel de progreso muestra el log en tiempo real con cada acción del agente.
5. **CAPTCHA** — Si el portal de la EEQ lo detecta, aparece un aviso. Resuélvelo en la ventana del navegador que abrió el agente y haz clic en **Continuar**.
6. **Resultados** — Al terminar se muestra la tabla de archivos y el ZIP se descarga automáticamente.

---

## Ajustes avanzados

| Parámetro | Por defecto | Descripción |
|-----------|-------------|-------------|
| Pausa entre descargas | 8 s | Espera entre cada PDF descargado |
| Pausa entre lotes | 180 s | Pausa larga cada N descargas para no saturar el sitio |
| Descargas por lote | 2 | Cuántas descargas seguidas antes de la pausa larga |

**Ejemplo con 10 facturas (valores por defecto):**
```
Descarga 1 → espera 8s → Descarga 2 → PAUSA 180s
Descarga 3 → espera 8s → Descarga 4 → PAUSA 180s
...
```
Tiempo total estimado: ~16 minutos para 10 facturas.

Con pocas facturas (2-3) puedes bajar la pausa a 30-60s. Con muchas (20+) conviene dejar los valores por defecto para no ser bloqueado.

Durante una pausa, el sidebar muestra el contador regresivo y un botón **Saltar pausa** para continuar antes de tiempo.

---

## Uso por terminal (sin interfaz web)

```bash
python3 eeq_descargar_facturas.py
```

El script pedirá el número de cuenta y el año de forma interactiva.

Variables de entorno opcionales:
```bash
EEQ_DESCARGAS_POR_LOTE=3 EEQ_PAUSA_ENTRE_LOTES=120 python3 eeq_descargar_facturas.py
```

---

## Despliegue en producción (Lightsail, sin Tailscale)

Desde la Fase 4 de `PlataformaWebProyectos` (Anexo B), este agente corre en el **mismo servidor
Lightsail** que la plataforma (Windows Server 2022), detrás de **Caddy**, y la plataforma lo llama
por HTTP en `127.0.0.1:3002` — ya no vive en el Mac del desarrollador expuesto por Tailscale
Funnel. El único paso que sigue siendo manual: el **CAPTCHA del portal de la EEQ** lo resuelve un
humano abriendo `https://eeq.airis.ec` en su navegador — esto no cambia con la migración de
servidor, solo cambia el dominio (antes era el enlace de Tailscale).

### Primera instalación

Requiere un runtime nuevo en ese servidor (Python + Playwright no existían ahí antes):

```powershell
choco install -y python
New-Item -ItemType Directory -Force C:\airis | Out-Null
cd C:\airis
git clone https://github.com/SebasAisalla/AgenteEEQ.git
cd C:\airis\AgenteEEQ
pip install -r requirements.txt
playwright install chromium

nssm install AIRISAgenteEEQ "C:\Python311\python.exe"
nssm set AIRISAgenteEEQ AppParameters "server.py"
nssm set AIRISAgenteEEQ AppDirectory "C:\airis\AgenteEEQ"
nssm start AIRISAgenteEEQ
```

> **Integración con PlataformaWebProyectos:** este agente no usa Postgres ni
> tiene migraciones propias. Cuando una actualización cambie las operaciones
> EEQ, actualiza después la plataforma, ejecuta allí `npm run db:migrar` y
> reinicia `AIRISPlataforma`; esas migraciones crean el registro de operaciones
> y bloquean duplicados por proyecto. Reinicia ambos servicios antes de iniciar
> un cálculo nuevo.

`server.py` ya escucha en `127.0.0.1` (no `0.0.0.0`) — el firewall de Lightsail (solo 80/443
abiertos) protege igual, pero es más explícito y consistente con el patrón de la plataforma.

### Bloque de Caddy (`C:\Caddy\Caddyfile`)

```caddy
eeq.airis.ec {
    reverse_proxy 127.0.0.1:3002
}
```

A diferencia de AgenteWebGis, este dominio **sí se usa en producción real**: es donde el PM/admin
abre la SPA para resolver el CAPTCHA cuando el portal de la EEQ lo presenta. Tras agregar el
bloque:

```powershell
caddy reload --config C:\Caddy\Caddyfile
```

Y un registro DNS tipo **A** de `eeq.airis.ec` apuntando a la IP estática del Lightsail.

### Actualizar el servidor cuando hay cambios

```powershell
nssm stop AIRISAgenteEEQ
cd C:\airis\AgenteEEQ
git pull
pip install -r requirements.txt    # solo si requirements.txt cambió
nssm start AIRISAgenteEEQ
```

Las carpetas `datos/` (facturas descargadas por cliente) y `debug/` (capturas de error) están
gitignoreadas — viven solo en el servidor, `git pull` nunca las toca ni las sobrescribe.

---

## Solución de problemas

**`ModuleNotFoundError: No module named 'flask'`**
```bash
pip3 install -r requirements.txt
```

**`playwright._impl._errors.Error: Executable doesn't exist`**
```bash
playwright install chromium
```

**El navegador se abre pero no encuentra facturas**
- Verifica que el número de cuenta sea correcto (solo dígitos).
- El sitio puede estar en mantenimiento; intenta más tarde.
- Revisa las capturas en la carpeta `debug/`.

**Otro dispositivo no puede conectarse**
- Verifica que ambos equipos estén en la misma red WiFi.
- En Mac: Preferencias del Sistema → Seguridad → Firewall → Opciones → permitir `python3`.

**El proceso se congela sin avanzar**
- Puede ser un CAPTCHA no detectado. Revisa la ventana del navegador que abrió el agente.
- Aumenta la pausa entre lotes en los ajustes avanzados.

**Error 502 al entrar por el link de Tailscale**
- El servidor no está corriendo. Ejecuta `python3 server.py` o `pm2 restart AgenteEEQ`.
