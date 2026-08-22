"""
Pokémon TCG Price Tracker
==========================
App móvil-friendly en Streamlit para monitorear precios de cartas Pokémon TCG,
gestionar una colección personal, visualizar tendencias y recibir alertas push
vía ntfy.sh cuando el precio de una carta cambia por encima de un umbral.

Fuente de datos: API pública y gratuita de pokemontcg.io (https://pokemontcg.io)
Esta API expone precios agregados de TCGPlayer y Cardmarket de forma estructurada
y autorizada, sin necesidad de scraping directo al sitio web.

Archivo único listo para desplegar en Streamlit Community Cloud.
"""

import streamlit as st
import sqlite3
import requests
import threading
import time
import random
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# ----------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# ----------------------------------------------------------------------------

DB_PATH = "pokemon_tcg.db"
API_BASE = "https://api.pokemontcg.io/v2/cards"
BACKGROUND_CHECK_INTERVAL_SECONDS = 3 * 60 * 60  # cada 3 horas

# Lista curada de cartas populares para el Dashboard de mercado.
# El usuario puede editar esta lista o buscar cualquier otra carta manualmente.
MARKET_WATCHLIST = [
    "Charizard ex",
    "Pikachu VMAX",
    "Umbreon VMAX",
    "Mew ex",
    "Rayquaza VMAX",
    "Gengar VMAX",
    "Lugia V",
    "Giratina VSTAR",
]

st.set_page_config(
    page_title="Pokémon TCG Tracker",
    page_icon="🎴",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ----------------------------------------------------------------------------
# BASE DE DATOS SQLITE
# ----------------------------------------------------------------------------

def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS collection (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            set_name TEXT,
            card_number TEXT,
            purchase_price REAL NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name TEXT NOT NULL,
            set_name TEXT,
            price REAL NOT NULL,
            recorded_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def save_price_point(card_name, set_name, price):
    conn = get_connection()
    conn.execute(
        "INSERT INTO price_history (card_name, set_name, price, recorded_at) VALUES (?, ?, ?, ?)",
        (card_name, set_name, price, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_price_history(card_name, days=30):
    conn = get_connection()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT price, recorded_at FROM price_history "
        "WHERE card_name = ? AND recorded_at >= ? ORDER BY recorded_at ASC",
        (card_name, cutoff),
    ).fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame(columns=["recorded_at", "price"])
    df = pd.DataFrame(rows, columns=["price", "recorded_at"])
    df["recorded_at"] = pd.to_datetime(df["recorded_at"])
    return df


def get_latest_two_prices(card_name):
    conn = get_connection()
    rows = conn.execute(
        "SELECT price, recorded_at FROM price_history WHERE card_name = ? "
        "ORDER BY recorded_at DESC LIMIT 2",
        (card_name,),
    ).fetchall()
    conn.close()
    return rows


# --- Colección personal (CRUD) ---

def add_collection_card(name, set_name, card_number, purchase_price, quantity):
    conn = get_connection()
    conn.execute(
        "INSERT INTO collection (name, set_name, card_number, purchase_price, quantity) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, set_name, card_number, purchase_price, quantity),
    )
    conn.commit()
    conn.close()


def update_collection_card(card_id, name, set_name, card_number, purchase_price, quantity):
    conn = get_connection()
    conn.execute(
        "UPDATE collection SET name=?, set_name=?, card_number=?, purchase_price=?, quantity=? "
        "WHERE id=?",
        (name, set_name, card_number, purchase_price, quantity, card_id),
    )
    conn.commit()
    conn.close()


def delete_collection_card(card_id):
    conn = get_connection()
    conn.execute("DELETE FROM collection WHERE id=?", (card_id,))
    conn.commit()
    conn.close()


def get_collection():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM collection ORDER BY name ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# INTEGRACIÓN CON LA API DE pokemontcg.io
# ----------------------------------------------------------------------------

def _extract_market_price(card_json):
    """Extrae un precio de mercado representativo del objeto 'card' devuelto
    por la API, priorizando tcgplayer (USD) y usando cardmarket como respaldo."""
    tcgplayer = card_json.get("tcgplayer", {}).get("prices", {})
    for variant in ("holofoil", "reverseHolofoil", "normal", "1stEditionHolofoil"):
        if variant in tcgplayer and tcgplayer[variant].get("market"):
            return tcgplayer[variant]["market"], "USD (TCGPlayer)"

    cardmarket = card_json.get("cardmarket", {}).get("prices", {})
    if cardmarket.get("averageSellPrice"):
        return cardmarket["averageSellPrice"], "EUR (Cardmarket)"

    return None, None


@st.cache_data(ttl=BACKGROUND_CHECK_INTERVAL_SECONDS, show_spinner=False)
def fetch_card_from_api(card_name):
    """Busca una carta por nombre en pokemontcg.io y devuelve su info + precio."""
    try:
        resp = requests.get(
            API_BASE,
            params={"q": f'name:"{card_name}"', "orderBy": "-set.releaseDate", "pageSize": 1},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None
        card = data[0]
        price, price_source = _extract_market_price(card)
        return {
            "name": card.get("name"),
            "set": card.get("set", {}).get("name"),
            "number": card.get("number"),
            "image": card.get("images", {}).get("small"),
            "price": price,
            "price_source": price_source,
        }
    except requests.RequestException:
        return None


def refresh_market_watchlist():
    """Consulta la API para cada carta del watchlist y guarda un punto de precio
    en el histórico. Pensado para llamarse periódicamente, no en cada rerun."""
    results = []
    for name in MARKET_WATCHLIST:
        info = fetch_card_from_api.__wrapped__(name)  # bypass cache for the refresh job
        if info and info["price"] is not None:
            save_price_point(info["name"], info["set"], info["price"])
            results.append(info)
        time.sleep(random.uniform(1.0, 2.5))  # espaciar llamadas, ser buen ciudadano de la API
    return results


# ----------------------------------------------------------------------------
# NOTIFICACIONES PUSH (ntfy.sh)
# ----------------------------------------------------------------------------

def send_ntfy_notification(channel, title, message, priority="default"):
    if not channel:
        return False
    try:
        requests.post(
            f"https://ntfy.sh/{channel}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=10,
        )
        return True
    except requests.RequestException:
        return False


def check_and_alert(card_name, set_name, threshold_pct, channel):
    rows = get_latest_two_prices(card_name)
    if len(rows) < 2:
        return
    latest, previous = rows[0]["price"], rows[1]["price"]
    if previous == 0:
        return
    change_pct = ((latest - previous) / previous) * 100
    if abs(change_pct) >= threshold_pct:
        direction = "📈" if change_pct > 0 else "📉"
        msg = (
            f"[Pokémon TCG] ¡Alerta de Precio! {direction} La carta {card_name} "
            f"({set_name}) ha cambiado de ${previous:.2f} a ${latest:.2f} "
            f"({change_pct:+.1f}%)."
        )
        send_ntfy_notification(channel, "Alerta de precio Pokémon TCG", msg)


def background_monitor_loop():
    """Hilo en segundo plano: refresca precios del watchlist y de la colección
    del usuario, y dispara alertas ntfy si superan el umbral configurado."""
    while True:
        try:
            threshold = float(get_setting("alert_threshold_pct", "5"))
            channel = get_setting("ntfy_channel", "")

            market_results = refresh_market_watchlist()
            for info in market_results:
                check_and_alert(info["name"], info["set"], threshold, channel)

            for item in get_collection():
                info = fetch_card_from_api.__wrapped__(item["name"])
                if info and info["price"] is not None:
                    save_price_point(info["name"], info["set"], info["price"])
                    check_and_alert(info["name"], info["set"], threshold, channel)
                time.sleep(random.uniform(1.0, 2.5))
        except Exception:
            pass
        time.sleep(BACKGROUND_CHECK_INTERVAL_SECONDS)


def start_background_thread_once():
    if "bg_thread_started" not in st.session_state:
        t = threading.Thread(target=background_monitor_loop, daemon=True)
        t.start()
        st.session_state["bg_thread_started"] = True


# ----------------------------------------------------------------------------
# MOTOR PREDICTIVO (regresión lineal simple)
# ----------------------------------------------------------------------------

def predict_trend(df):
    """Recibe un DataFrame con columnas ['recorded_at', 'price'] y devuelve
    una proyección a 7 y 30 días usando regresión lineal simple (numpy.polyfit)."""
    if len(df) < 3:
        return None

    df = df.sort_values("recorded_at").reset_index(drop=True)
    x = np.array([(t - df["recorded_at"].iloc[0]).total_seconds() / 86400 for t in df["recorded_at"]])
    y = df["price"].values

    slope, intercept = np.polyfit(x, y, 1)

    last_day = x[-1]
    pred_7 = slope * (last_day + 7) + intercept
    pred_30 = slope * (last_day + 30) + intercept
    current_price = y[-1]

    pct_change_30 = ((pred_30 - current_price) / current_price) * 100 if current_price else 0

    if pct_change_30 > 5:
        diagnosis = "Tendencia a futuro: Alza Probable 📈"
    elif pct_change_30 < -5:
        diagnosis = "Tendencia a futuro: Baja Probable 📉"
    else:
        diagnosis = "Tendencia a futuro: Estable ➡️"

    return {
        "slope_per_day": slope,
        "pred_7": pred_7,
        "pred_30": pred_30,
        "diagnosis": diagnosis,
    }


# ----------------------------------------------------------------------------
# UI: DASHBOARD DE MERCADO
# ----------------------------------------------------------------------------

def page_dashboard():
    st.title("🏠 Dashboard de Mercado")

    if st.button("🔄 Actualizar precios ahora", use_container_width=True):
        with st.spinner("Consultando pokemontcg.io..."):
            refresh_market_watchlist()
        st.success("Precios actualizados.")

    changes = []
    for name in MARKET_WATCHLIST:
        rows = get_latest_two_prices(name)
        if len(rows) >= 2 and rows[1]["price"]:
            latest, previous = rows[0]["price"], rows[1]["price"]
            pct = ((latest - previous) / previous) * 100
            changes.append({"name": name, "latest": latest, "pct": pct})
        elif len(rows) == 1:
            changes.append({"name": name, "latest": rows[0]["price"], "pct": 0.0})

    if not changes:
        st.info("Aún no hay datos históricos. Presiona 'Actualizar precios ahora' para comenzar.")
        return

    gainers = sorted(changes, key=lambda c: c["pct"], reverse=True)[:3]
    losers = sorted(changes, key=lambda c: c["pct"])[:3]

    st.subheader("Top Subidas 🔥")
    cols = st.columns(len(gainers)) if gainers else []
    for col, c in zip(cols, gainers):
        col.metric(c["name"], f"${c['latest']:.2f}", f"{c['pct']:+.1f}%")

    st.subheader("Top Bajadas 📉")
    cols = st.columns(len(losers)) if losers else []
    for col, c in zip(cols, losers):
        col.metric(c["name"], f"${c['latest']:.2f}", f"{c['pct']:+.1f}%")

    st.divider()
    st.subheader("Todas las cartas monitoreadas")
    st.dataframe(
        pd.DataFrame(changes).rename(columns={"name": "Carta", "latest": "Precio", "pct": "% cambio"}),
        use_container_width=True,
        hide_index=True,
    )


# ----------------------------------------------------------------------------
# UI: MI COLECCIÓN
# ----------------------------------------------------------------------------

def page_collection():
    st.title("💼 Mi Colección")

    collection = get_collection()

    total_value = 0.0
    total_cost = 0.0
    for item in collection:
        rows = get_latest_two_prices(item["name"])
        current_price = rows[0]["price"] if rows else item["purchase_price"]
        total_value += current_price * item["quantity"]
        total_cost += item["purchase_price"] * item["quantity"]

    net = total_value - total_cost
    c1, c2, c3 = st.columns(3)
    c1.metric("Valor Actual", f"${total_value:,.2f}")
    c2.metric("Costo Total", f"${total_cost:,.2f}")
    c3.metric("Ganancia/Pérdida", f"${net:,.2f}", f"{(net/total_cost*100) if total_cost else 0:+.1f}%")

    st.divider()

    with st.expander("➕ Agregar carta a la colección", expanded=len(collection) == 0):
        with st.form("add_card_form", clear_on_submit=True):
            name = st.text_input("Nombre de la carta")
            set_name = st.text_input("Expansión / Set")
            card_number = st.text_input("Código / Número")
            purchase_price = st.number_input("Precio de Compra (USD)", min_value=0.0, step=0.5)
            quantity = st.number_input("Cantidad", min_value=1, step=1, value=1)
            submitted = st.form_submit_button("Agregar", use_container_width=True)
            if submitted and name:
                add_collection_card(name, set_name, card_number, purchase_price, int(quantity))
                st.success(f"'{name}' agregada a tu colección.")
                st.rerun()

    st.divider()
    st.subheader("Tus cartas")

    if not collection:
        st.info("Todavía no has agregado cartas.")
        return

    for item in collection:
        with st.container(border=True):
            st.markdown(f"**{item['name']}** — {item['set_name'] or 's/set'} #{item['card_number'] or '-'}")
            st.caption(f"Compra: ${item['purchase_price']:.2f} × {item['quantity']}")

            edit_key = f"edit_{item['id']}"
            if st.session_state.get(edit_key):
                with st.form(f"form_{item['id']}"):
                    new_name = st.text_input("Nombre", value=item["name"])
                    new_set = st.text_input("Set", value=item["set_name"] or "")
                    new_number = st.text_input("Número", value=item["card_number"] or "")
                    new_price = st.number_input("Precio de compra", value=float(item["purchase_price"]), min_value=0.0)
                    new_qty = st.number_input("Cantidad", value=int(item["quantity"]), min_value=1, step=1)
                    col_a, col_b = st.columns(2)
                    if col_a.form_submit_button("Guardar", use_container_width=True):
                        update_collection_card(item["id"], new_name, new_set, new_number, new_price, int(new_qty))
                        st.session_state[edit_key] = False
                        st.rerun()
                    if col_b.form_submit_button("Cancelar", use_container_width=True):
                        st.session_state[edit_key] = False
                        st.rerun()
            else:
                col_a, col_b = st.columns(2)
                if col_a.button("✏️ Editar", key=f"btn_edit_{item['id']}", use_container_width=True):
                    st.session_state[edit_key] = True
                    st.rerun()
                if col_b.button("🗑️ Eliminar", key=f"btn_del_{item['id']}", use_container_width=True):
                    delete_collection_card(item["id"])
                    st.rerun()


# ----------------------------------------------------------------------------
# UI: PREDICCIONES Y GRÁFICAS
# ----------------------------------------------------------------------------

def page_predictions():
    st.title("📊 Predicciones y Gráficas")

    collection_names = [c["name"] for c in get_collection()]
    all_candidates = sorted(set(MARKET_WATCHLIST + collection_names))

    if not all_candidates:
        st.info("Agrega cartas a tu colección o espera a que se cargue el mercado.")
        return

    selected = st.selectbox("Selecciona una carta", all_candidates)

    if st.button("Buscar carta más reciente en la API"):
        with st.spinner("Consultando..."):
            info = fetch_card_from_api(selected)
        if info and info["price"] is not None:
            save_price_point(info["name"], info["set"], info["price"])
            st.success(f"Precio actual: ${info['price']:.2f} ({info['price_source']})")
        else:
            st.warning("No se encontró precio para esa carta en este momento.")

    df = get_price_history(selected, days=30)

    if df.empty:
        st.info("Sin histórico de precios todavía para esta carta.")
        return

    chart_df = df.set_index("recorded_at")[["price"]]
    st.line_chart(chart_df)

    trend = predict_trend(df)
    if trend:
        st.subheader("Análisis Predictivo")
        st.write(f"**{trend['diagnosis']}**")
        col1, col2 = st.columns(2)
        col1.metric("Proyección a 7 días", f"${trend['pred_7']:.2f}")
        col2.metric("Proyección a 30 días", f"${trend['pred_30']:.2f}")
        st.caption(f"Variación estimada: ${trend['slope_per_day']:.3f} / día (regresión lineal simple)")
    else:
        st.info("Se necesitan al menos 3 puntos de precio histórico para generar una predicción.")


# ----------------------------------------------------------------------------
# UI: ALERTAS Y CONFIGURACIÓN
# ----------------------------------------------------------------------------

def page_settings():
    st.title("⚙️ Alertas y Configuración")

    st.subheader("Umbral de alerta")
    threshold = st.number_input(
        "Porcentaje de fluctuación para disparar una alerta (%)",
        min_value=0.5, max_value=100.0,
        value=float(get_setting("alert_threshold_pct", "5")),
        step=0.5,
    )

    st.subheader("Canal de ntfy.sh")
    st.caption(
        "Instala la app gratuita ntfy (Android/iOS) y suscríbete al mismo nombre de canal "
        "que escribas aquí para recibir las notificaciones en tu pantalla de bloqueo."
    )
    channel = st.text_input(
        "Nombre de tu canal de ntfy",
        value=get_setting("ntfy_channel", ""),
        placeholder="mis_alertas_pokemon_123",
    )

    st.subheader("Pool de proxies (opcional)")
    st.caption("Lista de proxies HTTP separados por coma, usados como respaldo si la API pública tiene límite de tasa.")
    proxies = st.text_area(
        "Proxies (opcional)",
        value=get_setting("proxy_pool", ""),
        placeholder="http://usuario:pass@proxy1:puerto, http://usuario:pass@proxy2:puerto",
    )

    if st.button("💾 Guardar configuración", use_container_width=True):
        set_setting("alert_threshold_pct", threshold)
        set_setting("ntfy_channel", channel.strip())
        set_setting("proxy_pool", proxies.strip())
        st.success("Configuración guardada.")

    st.divider()
    if get_setting("ntfy_channel") and st.button("🔔 Enviar notificación de prueba", use_container_width=True):
        ok = send_ntfy_notification(
            get_setting("ntfy_channel"),
            "Prueba - Pokémon TCG Tracker",
            "✅ Esta es una notificación de prueba. Tu canal de ntfy está funcionando.",
        )
        if ok:
            st.success("Notificación enviada. Revisa tu app de ntfy.")
        else:
            st.error("No se pudo enviar la notificación. Verifica el nombre del canal.")


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    init_db()
    start_background_thread_once()

    st.sidebar.title("🎴 Pokémon TCG Tracker")
    page = st.sidebar.radio(
        "Navegación",
        ["🏠 Dashboard", "💼 Mi Colección", "📊 Predicciones", "⚙️ Alertas"],
        label_visibility="collapsed",
    )

    if page == "🏠 Dashboard":
        page_dashboard()
    elif page == "💼 Mi Colección":
        page_collection()
    elif page == "📊 Predicciones":
        page_predictions()
    elif page == "⚙️ Alertas":
        page_settings()


if __name__ == "__main__":
    main()
