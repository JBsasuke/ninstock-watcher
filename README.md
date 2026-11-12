# Vigilante NinStock

Cada 5 minutos (gratis, en GitHub Actions):

1. Revisa los productos de `products.json` y, cuando uno pasa a **disponible**, te manda
   una notificación al teléfono. Tocas la notificación y se abre la tienda para comprar.
2. Publica `feed.json` con las **ofertas de Nintendo Switch** de todas las tiendas
   (Slickdeals), el estado de los productos vigilados y **noticias** (Nintendo Life).
   La app lee ese archivo: **ningún usuario necesita clave**.

> La app busca el archivo en `https://raw.githubusercontent.com/JBsasuke/ninstock-watcher/main/feed.json`,
> así que el repo tiene que llamarse **ninstock-watcher**, estar en la cuenta **JBsasuke** y la rama ser **main**.

- **Nintendo y GameStop:** revisa la página del producto.
- **Best Buy:** su API necesita clave (solo la dan con correo .edu). Sin clave esos productos quedan pausados.
- **Walmart, Target y Amazon:** también se pueden poner, pero a veces bloquean a GitHub.
  En el registro verás "la tienda bloqueó la consulta" si pasa.

Solo avisa cuando algo **cambia** a disponible, no cada 5 minutos.

---

## 1. Firebase (para que las alertas lleguen a la app)
1. Entra a https://console.firebase.google.com y crea un proyecto llamado `NinStock`.
2. Pulsa **Agregar app → Android**, paquete `com.ninstock.app`.
3. Descarga `google-services.json` y ponlo en `NinStock/app/`. Vuelve a compilar la app.
4. En Firebase: ⚙️ **Configuración del proyecto → Cuentas de servicio → Generar nueva clave privada**.
   Se descarga un `.json`. **Es secreto: no lo subas a GitHub.** Lo usarás en el paso 3.

## 2. Crear el repositorio en GitHub
1. Entra a https://github.com/new, nombre `ninstock-watcher`, **Public** (así los minutos son gratis e ilimitados).
2. Sube **todo el contenido de esta carpeta `watcher`**, incluida la carpeta `.github`.
   Con git, desde esta carpeta:
   ```
   git init -b main
   git add .
   git commit -m "Vigilante NinStock"
   git remote add origin https://github.com/JBsasuke/ninstock-watcher.git
   git push -u origin main
   ```

## 3. Poner las claves (secretos)
En el repo: **Settings → Secrets and variables → Actions → New repository secret**

| Nombre | Valor |
|---|---|
| `BESTBUY_API_KEY` | *(opcional, solo si consigues una)* |
| `FIREBASE_SERVICE_ACCOUNT` | pega **todo** el contenido del `.json` del paso 1.4 |
| `NTFY_TOPIC` | *(opcional)* ver abajo |

Los secretos no se ven aunque el repo sea público.

## 4. Probar
1. Pestaña **Actions** → si pide, pulsa **Enable workflows**.
2. **Vigilante NinStock → Run workflow**, marca **Mandar notificación de prueba** → **Run**.
3. Te debe llegar "NinStock: prueba" al teléfono (con la app abierta una vez antes y las notificaciones permitidas).
4. Corre otra vez sin marcar la prueba y abre el registro: verás el estado de cada producto.

## Agregar o quitar productos
Edita `products.json` (en GitHub mismo, con el lápiz ✏️):

```json
{
  "id": "nombre-unico",
  "name": "Lo que verás en la notificación",
  "store": "nintendo | gamestop | walmart | target | amazon | bestbuy",
  "store_name": "Nintendo",
  "url": "link exacto de la página del producto"
}
```
- Para Best Buy puedes usar `"search": "palabras del producto"` en vez de `url`.
- `"enabled": false` lo pausa.
- Si una tienda usa otras palabras, agrega `"in_stock_text": ["..."]` o `"out_of_stock_text": ["..."]`.

## Opcional: ntfy (prueba rápida sin Firebase)
1. Instala la app **ntfy** en el teléfono y suscríbete a un nombre raro, ej. `ninstock-juan-83k2x`.
2. Crea el secreto `NTFY_TOPIC` con ese nombre. Los avisos llegarán también por ahí.

## Bueno saber
- GitHub puede atrasar las corridas unos minutos cuando hay mucha gente.
- Si el repo pasa **60 días sin cambios**, GitHub apaga las tareas programadas: entra a Actions y vuelve a activarlo (o edita cualquier archivo).
- Si necesitas revisar cada minuto, se puede mover a un servidor gratis de Oracle Cloud.
