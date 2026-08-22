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
API_CARDS = "https://api.pokemontcg.io/v2/cards"
API_SETS = "https://api.pokemontcg.io/v2/sets"
EXCHANGE_RATE_API = "https://open.er-api.com/v6/latest/USD"  # gratuita, sin API key
FALLBACK_USD_TO_MXN = 18.5  # se usa solo si la API de tipo de cambio no responde
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
# TIPO DE CAMBIO USD -> MXN
# ----------------------------------------------------------------------------

@st.cache_data(ttl=6 * 60 * 60, show_spinner=False)  # se refresca cada 6 horas
def get_usd_to_mxn_rate():
    try:
        resp = requests.get(EXCHANGE_RATE_API, timeout=10)
        resp.raise_for_status()
        rate = resp.json().get("rates", {}).get("MXN")
        if rate:
            return float(rate)
    except requests.RequestException:
        pass
    return FALLBACK_USD_TO_MXN


def format_dual_price(usd_price):
    """Devuelve el precio formateado en MXN (principal) y USD (referencia)."""
    if usd_price is None:
        return "N/D"
    rate = get_usd_to_mxn_rate()
    mxn_price = usd_price * rate
    return f"${mxn_price:,.2f} MXN (${usd_price:,.2f} USD)"


def to_mxn(usd_price):
    if usd_price is None:
        return None
    return usd_price * get_usd_to_mxn_rate()


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


def _card_json_to_dict(card):
    price, price_source = _extract_market_price(card)
    return {
        "id": card.get("id"),
        "name": card.get("name"),
        "set": card.get("set", {}).get("name"),
        "set_id": card.get("set", {}).get("id"),
        "number": card.get("number"),
        "image": card.get("images", {}).get("small"),
        "price": price,
        "price_source": price_source,
    }


@st.cache_data(ttl=BACKGROUND_CHECK_INTERVAL_SECONDS, show_spinner=False)
def fetch_card_from_api(card_name):
    """Busca una carta por nombre en pokemontcg.io y devuelve su info + precio
    (usa la impresión/expansión más reciente que encuentre)."""
    try:
        resp = requests.get(
            API_CARDS,
            params={"q": f'name:"{card_name}"', "orderBy": "-set.releaseDate", "pageSize": 1},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None
        return _card_json_to_dict(data[0])
    except requests.RequestException:
        return None


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def fetch_all_sets():
    """Devuelve la lista completa de expansiones/sets existentes en pokemontcg.io."""
    try:
        resp = requests.get(API_SETS, params={"orderBy": "-releaseDate"}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except requests.RequestException:
        return []


@st.cache_data(ttl=6 * 60 * 60, show_spinner=False)
def search_cards(name_query=None, set_id=None, page_size=20):
    """Busca cartas en TODAS las expansiones existentes, opcionalmente filtrando
    por nombre y/o por una expansión específica. Devuelve precio real de mercado
    de cada resultado."""
    query_parts = []
    if name_query:
        query_parts.append(f'name:"*{name_query}*"')
    if set_id:
        query_parts.append(f'set.id:{set_id}')

    params = {"pageSize": page_size, "orderBy": "-set.releaseDate"}
    if query_parts:
        params["q"] = " ".join(query_parts)

    try:
        resp = requests.get(API_CARDS, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [_card_json_to_dict(c) for c in data]
    except requests.RequestException:
        return []


def refresh_market_watchlist():
    """Consulta la API para cada carta del watchlist y guarda un punto de precio
    en el histórico. Pensado para llamarse periódicamente, no en cada rerun."""
    results = []
    for name in MARKET_WATCHLIST:
        info = fetch_card_from_api.__wrapped__(name)  # se salta la caché para el refresco periódico
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
        rate = get_usd_to_mxn_rate()
        msg = (
            f"[Pokémon TCG] ¡Alerta de Precio! {direction} La carta {card_name} "
            f"({set_name}) ha cambiado de ${previous*rate:,.2f} a ${latest*rate:,.2f} MXN "
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

    rate = get_usd_to_mxn_rate()
    st.caption(f"Tipo de cambio actual: 1 USD ≈ ${rate:.2f} MXN")

    st.subheader("Top Subidas 🔥")
    cols = st.columns(len(gainers)) if gainers else []
    for col, c in zip(cols, gainers):
        col.metric(c["name"], f"${c['latest']*rate:,.2f} MXN", f"{c['pct']:+.1f}%")

    st.subheader("Top Bajadas 📉")
    cols = st.columns(len(losers)) if losers else []
    for col, c in zip(cols, losers):
        col.metric(c["name"], f"${c['latest']*rate:,.2f} MXN", f"{c['pct']:+.1f}%")

    st.divider()
    st.subheader("Todas las cartas monitoreadas")
    table_df = pd.DataFrame(changes)
    table_df["precio_mxn"] = table_df["latest"] * rate
    table_df = table_df.rename(columns={
        "name": "Carta", "latest": "Precio (USD)", "precio_mxn": "Precio (MXN)", "pct": "% cambio",
    })[["Carta", "Precio (MXN)", "Precio (USD)", "% cambio"]]
    st.dataframe(
        table_df.style.format({"Precio (MXN)": "${:,.2f}", "Precio (USD)": "${:,.2f}", "% cambio": "{:+.1f}%"}),
        use_container_width=True,
        hide_index=True,
    )


# ----------------------------------------------------------------------------
# UI: MI COLECCIÓN
# ----------------------------------------------------------------------------

def page_collection():
    st.title("💼 Mi Colección")

    collection = get_collection()
    rate = get_usd_to_mxn_rate()

    total_value_usd = 0.0
    total_cost_usd = 0.0
    for item in collection:
        rows = get_latest_two_prices(item["name"])
        current_price = rows[0]["price"] if rows else item["purchase_price"]
        total_value_usd += current_price * item["quantity"]
        total_cost_usd += item["purchase_price"] * item["quantity"]

    net_usd = total_value_usd - total_cost_usd
    c1, c2, c3 = st.columns(3)
    c1.metric("Valor Actual", f"${total_value_usd*rate:,.2f} MXN", help=f"${total_value_usd:,.2f} USD")
    c2.metric("Costo Total", f"${total_cost_usd*rate:,.2f} MXN", help=f"${total_cost_usd:,.2f} USD")
    c3.metric(
        "Ganancia/Pérdida",
        f"${net_usd*rate:,.2f} MXN",
        f"{(net_usd/total_cost_usd*100) if total_cost_usd else 0:+.1f}%",
        help=f"${net_usd:,.2f} USD",
    )
    st.caption(f"Tipo de cambio: 1 USD ≈ ${rate:.2f} MXN")

    st.divider()

    with st.expander("➕ Agregar carta a la colección", expanded=len(collection) == 0):
        st.markdown("**Paso 1: busca tu carta para traer su precio real de mercado**")
        col_s1, col_s2 = st.columns([3, 1])
        search_query = col_s1.text_input("Nombre de la carta a buscar", key="collection_search_box")
        do_search = col_s2.button("🔍 Buscar", use_container_width=True)

        if do_search and search_query:
            with st.spinner("Buscando en todas las expansiones..."):
                st.session_state["collection_search_results"] = search_cards(name_query=search_query, page_size=15)

        results = st.session_state.get("collection_search_results", [])
        if results:
            options = {
                f"{c['name']} — {c['set']} (#{c['number']})": c for c in results if c["price"] is not None
            }
            if options:
                choice_label = st.selectbox("Resultados encontrados", list(options.keys()), key="collection_choice")
                chosen = options[choice_label]
                st.info(f"Precio real de mercado: **{format_dual_price(chosen['price'])}** — fuente: {chosen['price_source']}")
                if st.button("Usar esta carta para llenar el formulario", use_container_width=True):
                    st.session_state["prefill_card"] = chosen
                    st.rerun()
            else:
                st.warning("Se encontraron cartas pero ninguna tiene precio de mercado disponible ahora mismo.")

        prefill = st.session_state.get("prefill_card", {})
        st.markdown("**Paso 2: confirma o ajusta los datos**")
        with st.form("add_card_form", clear_on_submit=False):
            name = st.text_input("Nombre de la carta", value=prefill.get("name", ""))
            set_name = st.text_input("Expansión / Set", value=prefill.get("set", ""))
            card_number = st.text_input("Código / Número", value=prefill.get("number", ""))

            default_price_mxn = round(to_mxn(prefill.get("price")) or 0.0, 2)
            purchase_price_mxn = st.number_input(
                "Precio de Compra (MXN)", min_value=0.0, step=10.0, value=default_price_mxn,
                help="Se precarga con el precio real de mercado actual; ajústalo si pagaste otro monto.",
            )
            quantity = st.number_input("Cantidad", min_value=1, step=1, value=1)
            submitted = st.form_submit_button("Agregar", use_container_width=True)
            if submitted and name:
                purchase_price_usd = purchase_price_mxn / rate
                add_collection_card(name, set_name, card_number, purchase_price_usd, int(quantity))
                if prefill.get("price") is not None:
                    save_price_point(name, set_name, prefill["price"])
                st.session_state.pop("prefill_card", None)
                st.success(f"'{name}' agregada a tu colección.")
                st.rerun()

    st.divider()
    st.subheader("Tus cartas")

    if not collection:
        st.info("Todavía no has agregado cartas.")
        return

    for item in collection:
        rows = get_latest_two_prices(item["name"])
        current_price_usd = rows[0]["price"] if rows else item["purchase_price"]
        with st.container(border=True):
            st.markdown(f"**{item['name']}** — {item['set_name'] or 's/set'} #{item['card_number'] or '-'}")
            st.caption(
                f"Compra: {format_dual_price(item['purchase_price'])} × {item['quantity']}  \n"
                f"Precio actual de mercado: {format_dual_price(current_price_usd)}"
            )

            edit_key = f"edit_{item['id']}"
            if st.session_state.get(edit_key):
                with st.form(f"form_{item['id']}"):
                    new_name = st.text_input("Nombre", value=item["name"])
                    new_set = st.text_input("Set", value=item["set_name"] or "")
                    new_number = st.text_input("Número", value=item["card_number"] or "")
                    new_price_mxn = st.number_input(
                        "Precio de compra (MXN)", value=round(item["purchase_price"] * rate, 2), min_value=0.0
                    )
                    new_qty = st.number_input("Cantidad", value=int(item["quantity"]), min_value=1, step=1)
                    col_a, col_b = st.columns(2)
                    if col_a.form_submit_button("Guardar", use_container_width=True):
                        update_collection_card(
                            item["id"], new_name, new_set, new_number, new_price_mxn / rate, int(new_qty)
                        )
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
# UI: BUSCADOR DE CARTAS (TODAS LAS EXPANSIONES)
# ----------------------------------------------------------------------------

def page_search_all_cards():
    st.title("🔍 Buscador de Cartas")
    st.caption("Busca el precio real de mercado de cualquier carta, en cualquier expansión existente.")

    sets_data = fetch_all_sets()
    set_options = {"Todas las expansiones": None}
    for s in sets_data:
        label = f"{s.get('name')} ({s.get('series')}) — {s.get('releaseDate','')}"
        set_options[label] = s.get("id")

    col1, col2 = st.columns([2, 1])
    name_query = col1.text_input("Nombre de la carta (opcional)", placeholder="Ej. Charizard")
    set_label = col2.selectbox("Expansión", list(set_options.keys()))
    set_id = set_options[set_label]

    if st.button("🔍 Buscar en todas las expansiones", use_container_width=True):
        if not name_query and not set_id:
            st.warning("Escribe un nombre o elige una expansión específica para buscar.")
        else:
            with st.spinner("Consultando pokemontcg.io..."):
                st.session_state["global_search_results"] = search_cards(
                    name_query=name_query or None, set_id=set_id, page_size=30
                )

    results = st.session_state.get("global_search_results", [])
    if not results:
        st.info("Escribe el nombre de una carta y/o elige una expansión, luego presiona Buscar.")
        return

    st.subheader(f"{len(results)} resultado(s)")
    rate = get_usd_to_mxn_rate()
    for card in results:
        with st.container(border=True):
            col_img, col_info = st.columns([1, 3])
            if card.get("image"):
                col_img.image(card["image"], use_container_width=True)
            with col_info:
                st.markdown(f"**{card['name']}** — {card['set']} (#{card['number']})")
                if card["price"] is not None:
                    st.write(f"💰 {format_dual_price(card['price'])}")
                    st.caption(f"Fuente: {card['price_source']}")
                    if st.button("➕ Agregar a mi colección", key=f"add_{card['id']}"):
                        st.session_state["prefill_card"] = card
                        st.success("Carta lista para agregar — ve a 'Mi Colección' para confirmar.")
                else:
                    st.caption("Sin precio de mercado disponible por ahora.")


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
            st.success(f"Precio actual: {format_dual_price(info['price'])} ({info['price_source']})")
        else:
            st.warning("No se encontró precio para esa carta en este momento.")

    df = get_price_history(selected, days=30)

    if df.empty:
        st.info("Sin histórico de precios todavía para esta carta.")
        return

    rate = get_usd_to_mxn_rate()
    chart_df = df.set_index("recorded_at")[["price"]].rename(columns={"price": "Precio (USD)"})
    chart_df["Precio (MXN)"] = chart_df["Precio (USD)"] * rate
    st.line_chart(chart_df[["Precio (MXN)"]])
    st.caption("Gráfica en pesos mexicanos (MXN). Tipo de cambio: 1 USD ≈ $%.2f MXN" % rate)

    trend = predict_trend(df)
    if trend:
        st.subheader("Análisis Predictivo")
        st.write(f"**{trend['diagnosis']}**")
        col1, col2 = st.columns(2)
        col1.metric("Proyección a 7 días", f"${trend['pred_7']*rate:,.2f} MXN", help=f"${trend['pred_7']:.2f} USD")
        col2.metric("Proyección a 30 días", f"${trend['pred_30']*rate:,.2f} MXN", help=f"${trend['pred_30']:.2f} USD")
        st.caption(f"Variación estimada: ${trend['slope_per_day']*rate:.2f} MXN / día (regresión lineal simple)")
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
        ["🏠 Dashboard", "💼 Mi Colección", "🔍 Buscar Cartas", "📊 Predicciones", "⚙️ Alertas"],
        label_visibility="collapsed",
    )

    if page == "🏠 Dashboard":
        page_dashboard()
    elif page == "💼 Mi Colección":
        page_collection()
    elif page == "🔍 Buscar Cartas":
        page_search_all_cards()
    elif page == "📊 Predicciones":
        page_predictions()
    elif page == "⚙️ Alertas":
        page_settings()


if __name__ == "__main__":
    main()
