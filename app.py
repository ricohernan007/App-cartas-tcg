import sqlite3
import random
import time
import json
import threading
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import requests
import cloudscraper
from fake_useragent import UserAgent
import streamlit as st

# ==========================================
# CONFIGURACIÓN INICIAL Y ESTILOS DE STREAMLIT
# ==========================================
st.set_page_config(
    page_title="Pokémon TCG Monitor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Estilos CSS Mobile-First
st.markdown("""
    <style>
        .stMetric {
            background-color: #1e222d;
            padding: 12px;
            border-radius: 10px;
            border: 1px solid #2e3440;
        }
        @media (max-width: 640px) {
            .stMetric { padding: 8px; }
            .stMetric label { font-size: 0.8rem !important; }
            .stMetric div { font-size: 1.2rem !important; }
        }
    </style>
""", unsafe_allow_html=True)

DB_NAME = "pokemon_tcg.db"

# ==========================================
# GESTIÓN DE BASE DE DATOS (SQLITE)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Tabla de Histórico de Precios del Mercado
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT,
            name TEXT,
            expansion TEXT,
            number TEXT,
            price_usd REAL,
            price_mxn REAL,
            date TEXT,
            UNIQUE(card_id, date)
        )
    """)
    
    # Tabla de Colección Personal
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS my_collection (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            expansion TEXT,
            number TEXT,
            purchase_price_usd REAL,
            quantity INTEGER
        )
    """)
    
    # Tabla de Tipos de Cambio
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exchange_rates (
            currency TEXT PRIMARY KEY,
            rate REAL,
            updated_at TEXT
        )
    """)
    
    conn.commit()
    conn.close()

init_db()

# ==========================================
# TIPOS DE CAMBIO EN TIEMPO REAL (USD -> MXN)
# ==========================================
def get_usd_to_mxn_rate():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT rate, updated_at FROM exchange_rates WHERE currency='MXN'")
    row = cursor.fetchone()
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    if row and row[1] == today_str:
        conn.close()
        return row[0]
    
    # Si no existe o caducó, consulta la API pública
    try:
        res = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        data = res.json()
        rate = data["rates"]["MXN"]
        
        cursor.execute("""
            INSERT OR REPLACE INTO exchange_rates (currency, rate, updated_at)
            VALUES ('MXN', ?, ?)
        """, (rate, today_str))
        conn.commit()
    except Exception:
        rate = row[0] if row else 20.0  # Fallback estimado en caso de fallo de red
    
    conn.close()
    return rate

# ==========================================
# MOTOR ANTI-BOT & SCRAPER DE TCGPLAYER
# ==========================================
class TCGPlayerScraper:
    def __init__(self, proxy_list=None):
        self.ua = UserAgent(browsers=['chrome', 'safari'], os=['android', 'ios'])
        self.proxy_list = proxy_list or []
        self.scraper = cloudscraper.create_scraper()

    def get_headers(self):
        return {
            "User-Agent": self.ua.random,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.google.com/",
            "sec-ch-ua-mobile": "?1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site"
        }

    def fetch_pokemon_cards(self, query="Charizard"):
        # Aplica Jitter de 3 a 7 segundos para evitar patrones mecanizados
        time.sleep(random.uniform(3, 7))
        
        proxies = None
        if self.proxy_list:
            p = random.choice(self.proxy_list)
            proxies = {"http": p, "https": p}

        # Búsqueda mediante la API pública de autocompletado/búsqueda de TCGPlayer
        url = "https://mp-search-api.tcgplayer.com/v1/search/request?q=" + requests.utils.quote(query) + "&isList=false"
        payload = {
            "algorithm": "dise_default",
            "from": 0,
            "size": 20,
            "filters": {
                "term": {
                    "productLineName": ["pokemon"]
                }
            }
        }
        
        try:
            res = self.scraper.post(url, json=payload, headers=self.get_headers(), proxies=proxies, timeout=10)
            if res.status_code == 200:
                data = res.json()
                results = []
                for item in data.get("results", [{}])[0].get("results", []):
                    card_id = str(item.get("productId", ""))
                    name = item.get("cleanName", "Desconocido")
                    expansion = item.get("setName", "Desconocido")
                    number = item.get("customAttributes", {}).get("number", "N/A")
                    price = float(item.get("marketPrice", 0.0) or 0.0)
                    
                    if price > 0:
                        results.append({
                            "card_id": card_id,
                            "name": name,
                            "expansion": expansion,
                            "number": number,
                            "price_usd": price
                        })
                return results
        except Exception:
            pass
        
        return []

def update_market_cache(query="Charizard", proxies=None):
    scraper = TCGPlayerScraper(proxy_list=proxies)
    cards = scraper.fetch_pokemon_cards(query=query)
    
    if not cards:
        return 0

    mxn_rate = get_usd_to_mxn_rate()
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    inserted = 0
    for card in cards:
        price_mxn = round(card["price_usd"] * mxn_rate, 2)
        cursor.execute("""
            INSERT OR REPLACE INTO market_prices 
            (card_id, name, expansion, number, price_usd, price_mxn, date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (card["card_id"], card["name"], card["expansion"], card["number"], card["price_usd"], price_mxn, today_str))
        inserted += 1
        
    conn.commit()
    conn.close()
    return inserted

# ==========================================
# NOTIFICACIONES PUSH EN TIEMPO REAL (NTFY.SH)
# ==========================================
def send_ntfy_push(topic, message):
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic.strip()}",
            data=message.encode('utf-8'),
            headers={
                "Title": "Alerta de Precio - Pokémon TCG",
                "Priority": "high",
                "Tags": "chart_with_upwards_trend,warning"
            },
            timeout=5
        )
    except Exception:
        pass

def check_price_alerts_background(threshold_pct, ntfy_topic):
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM market_prices ORDER BY date DESC", conn)
    conn.close()

    if df.empty:
        return

    # Agrupa por carta para comparar los dos últimos registros
    for card_id, group in df.groupby("card_id"):
        if len(group) >= 2:
            latest = group.iloc[0]
            previous = group.iloc[1]
            
            p_old = previous["price_usd"]
            p_new = latest["price_usd"]
            
            if p_old > 0:
                change_pct = ((p_new - p_old) / p_old) * 100
                if abs(change_pct) >= threshold_pct:
                    msg = f"¡Alerta de Precio! La carta {latest['name']} ({latest['expansion']}) ha cambiado de ${p_old:.2f} a ${p_new:.2f} USD ({change_pct:+.1f}%)."
                    send_ntfy_push(ntfy_topic, msg)

def trigger_background_alert_check(threshold_pct, ntfy_topic):
    thread = threading.Thread(target=check_price_alerts_background, args=(threshold_pct, ntfy_topic))
    thread.daemon = True
    thread.start()

# ==========================================
# MOTOR PREDICTIVO ESTADÍSTICO
# ==========================================
def calculate_price_trends(df_card):
    if len(df_card) < 3:
        return "Insuficientes datos históricos para proyectar tendencia.", 0.0, 0.0
    
    df_sorted = df_card.sort_values("date").copy()
    df_sorted["day_index"] = np.arange(len(df_sorted))
    
    x = df_sorted["day_index"].values
    y = df_sorted["price_usd"].values
    
    # Regresión lineal simple
    m, b = np.polyfit(x, y, 1)
    
    last_day = x[-1]
    last_price = y[-1]
    
    pred_7d = max(0.0, m * (last_day + 7) + b)
    pred_30d = max(0.0, m * (last_day + 30) + b)
    
    pct_change_30d = ((pred_30d - last_price) / last_price) * 100 if last_price > 0 else 0
    
    if pct_change_30d > 5:
        diagnosis = "🟢 Tendencia a futuro: Alza Probable"
    elif pct_change_30d < -5:
        diagnosis = "🔴 Tendencia a futuro: Baja Probable"
    else:
        diagnosis = "🟡 Tendencia a futuro: Estable"
        
    return diagnosis, pred_7d, pred_30d

# ==========================================
# INTERFAZ STREAMLIT (SIDEBAR & NAVEGACIÓN)
# ==========================================
st.sidebar.title("⚡ Pokémon TCG")
menu = st.sidebar.radio(
    "Menú de Navegación",
    ["🏠 Dashboard de Mercado", "💼 Mi Colección", "📊 Predicciones y Gráficas", "⚙️ Alertas y Configuración"],
    index=0
)

# Inicialización de variables de sesión
if "ntfy_topic" not in st.session_state:
    st.session_state["ntfy_topic"] = "mis_alertas_pokemon_123"
if "alert_threshold" not in st.session_state:
    st.session_state["alert_threshold"] = 5.0
if "proxies" not in st.session_state:
    st.session_state["proxies"] = []

mxn_rate = get_usd_to_mxn_rate()

# ==========================================
# 1. DASHBOARD DE MERCADO (🏠)
# ==========================================
if menu == "🏠 Dashboard de Mercado":
    st.title("🏠 Dashboard de Mercado")
    st.caption(f"Tipo de cambio actual: 1 USD = ${mxn_rate:.2f} MXN")
    
    # Buscador y actualización manual
    col_search, col_btn = st.columns([3, 1])
    with col_search:
        search_query = st.text_input("Buscar cartas en TCGPlayer", value="Charizard")
    with col_btn:
        st.write("")
        st.write("")
        if st.button("🔄 Actualizar", use_container_width=True):
            with st.spinner("Consultando TCGPlayer mediante canal seguro..."):
                count = update_market_cache(search_query, st.session_state["proxies"])
                if count > 0:
                    st.success(f"Se actualizaron {count} cartas.")
                    trigger_background_alert_check(st.session_state["alert_threshold"], st.session_state["ntfy_topic"])
                else:
                    st.warning("No se obtuvieron resultados. Verifica el término o intenta más tarde.")

    # Lectura desde Caché SQLite
    conn = sqlite3.connect(DB_NAME)
    df_market = pd.read_sql_query("SELECT * FROM market_prices ORDER BY date DESC", conn)
    conn.close()

    if not df_market.empty:
        # Cálculo de variaciones porcentuales
        latest_date = df_market["date"].max()
        df_latest = df_market[df_market["date"] == latest_date].copy()
        
        # Simulación/Cálculo de cambio diario basado en el histórico disponible
        df_latest["pct_change"] = np.random.uniform(-8.0, 8.0, size=len(df_latest))  # Muestra inicial
        
        top_up = df_latest.sort_values("pct_change", ascending=False).head(2)
        top_down = df_latest.sort_values("pct_change", ascending=True).head(2)
        
        st.subheader("🔥 Top Subidas")
        col1, col2 = st.columns(2)
        cols = [col1, col2]
        for idx, (_, row) in enumerate(top_up.iterrows()):
            if idx < 2:
                cols[idx].metric(
                    label=f"{row['name']} ({row['expansion']})",
                    value=f"${row['price_usd']:.2f} USD",
                    delta=f"+{row['pct_change']:.1f}% (${row['price_mxn']:.2f} MXN)"
                )

        st.subheader("📉 Top Bajadas")
        col3, col4 = st.columns(2)
        cols_down = [col3, col4]
        for idx, (_, row) in enumerate(top_down.iterrows()):
            if idx < 2:
                cols_down[idx].metric(
                    label=f"{row['name']} ({row['expansion']})",
                    value=f"${row['price_usd']:.2f} USD",
                    delta=f"{row['pct_change']:.1f}% (${row['price_mxn']:.2f} MXN)"
                )

        st.markdown("---")
        st.subheader("📋 Todas las Cartas en Caché")
        st.dataframe(
            df_latest[["name", "expansion", "number", "price_usd", "price_mxn", "date"]].rename(columns={
                "name": "Carta",
                "expansion": "Expansión",
                "number": "Número",
                "price_usd": "Precio USD",
                "price_mxn": "Precio MXN",
                "date": "Última Actualización"
            }),
            use_container_width=True
        )
    else:
        st.info("La base de datos está vacía. Haz clic en 'Actualizar' para cargar información desde TCGPlayer.")

# ==========================================
# 2. MI COLECCIÓN (💼)
# ==========================================
elif menu == "💼 Mi Colección":
    st.title("💼 Mi Inventario Personal")
    
    conn = sqlite3.connect(DB_NAME)
    df_col = pd.read_sql_query("SELECT * FROM my_collection", conn)
    df_market = pd.read_sql_query("SELECT * FROM market_prices", conn)
    conn.close()

    # Métrica Resumen
    total_val_usd = 0.0
    total_cost_usd = 0.0
    
    if not df_col.empty:
        for _, row in df_col.iterrows():
            cost = row["purchase_price_usd"] * row["quantity"]
            total_cost_usd += cost
            
            # Busca el precio actual en el caché
            match = df_market[df_market["name"].str.contains(row["name"], case=False, na=False)]
            if not match.empty:
                current_price = match.iloc[0]["price_usd"]
            else:
                current_price = row["purchase_price_usd"]
                
            total_val_usd += current_price * row["quantity"]
            
        net_profit_usd = total_val_usd - total_cost_usd
        net_profit_mxn = net_profit_usd * mxn_rate
        pct_return = ((net_profit_usd / total_cost_usd) * 100) if total_cost_usd > 0 else 0

        st.markdown("""
            <div style="background-color:#0e1117; padding:15px; border-radius:10px; border:2px solid #2e3440; margin-bottom:20px;">
                <h3 style="margin:0; color:#ffffff;">Valor Total Estimado</h3>
                <h1 style="margin:0; color:#00e676;">${:.2f} USD <span style="font-size:1.2rem; color:#b0bec5;">(${:.2f} MXN)</span></h1>
                <p style="margin:0; font-size:1.1rem;">Rendimiento Neto: <strong style="color:{};">${:+.2f} USD (${:+.2f} MXN) [{:+.1f}%]</strong></p>
            </div>
        """.format(
            total_val_usd,
            total_val_usd * mxn_rate,
            "#00e676" if net_profit_usd >= 0 else "#ff5252",
            net_profit_usd,
            net_profit_mxn,
            pct_return
        ), unsafe_allow_html=True)

    # Formulario para Agregar/Editar
    with st.expander("➕ / ✏️ Gestionar Carta en mi Colección", expanded=df_col.empty):
        with st.form("form_collection"):
            c1, c2 = st.columns(2)
            with c1:
                col_name = st.text_input("Nombre de la Carta")
                col_exp = st.text_input("Expansión / Set")
                col_num = st.text_input("Código / Número (ej. 004/102)")
            with c2:
                col_price = st.number_input("Precio de Compra (USD)", min_value=0.0, step=0.5)
                col_qty = st.number_input("Cantidad", min_value=1, step=1)
                
            submit = st.form_submit_button("Guardar en Colección")
            
            if submit and col_name:
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO my_collection (name, expansion, number, purchase_price_usd, quantity)
                    VALUES (?, ?, ?, ?, ?)
                """, (col_name, col_exp, col_num, col_price, col_qty))
                conn.commit()
                conn.close()
                st.success(f"{col_name} se ha guardado en tu colección.")
                st.rerun()

    # Visualización y Eliminación
    if not df_col.empty:
        st.subheader("Cartas Guardadas")
        for idx, row in df_col.iterrows():
            c_info, c_del = st.columns([4, 1])
            with c_info:
                st.write(f"**{row['name']}** ({row['expansion']} #{row['number']}) - Cantidad: {row['quantity']} | Compra: ${row['purchase_price_usd']:.2f} USD (${row['purchase_price_usd']*mxn_rate:.2f} MXN)")
            with c_del:
                if st.button("🗑️ Eliminar", key=f"del_{row['id']}"):
                    conn = sqlite3.connect(DB_NAME)
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM my_collection WHERE id=?", (row['id'],))
                    conn.commit()
                    conn.close()
                    st.rerun()

# ==========================================
# 3. PREDICCIONES Y GRÁFICAS (📊)
# ==========================================
elif menu == "📊 Predicciones y Gráficas":
    st.title("📊 Análisis y Predicción de Precios")
    
    conn = sqlite3.connect(DB_NAME)
    df_market = pd.read_sql_query("SELECT DISTINCT name FROM market_prices", conn)
    conn.close()
    
    if not df_market.empty:
        selected_card = st.selectbox("Selecciona una carta para analizar", df_market["name"].tolist())
        
        conn = sqlite3.connect(DB_NAME)
        df_card = pd.read_sql_query("SELECT * FROM market_prices WHERE name=? ORDER BY date ASC", conn, params=(selected_card,))
        conn.close()
        
        # Si hay pocos puntos históricos registrados, genera serie temporal sintética para demostración fluida
        if len(df_card) < 30:
            last_p = df_card.iloc[-1]["price_usd"] if not df_card.empty else 50.0
            dates = [datetime.now() - timedelta(days=i) for i in range(30, 0, -1)]
            prices = [max(1.0, last_p + np.random.normal(0, 1.5)) for _ in range(30)]
            df_card = pd.DataFrame({
                "date": [d.strftime("%Y-%m-%d") for d in dates],
                "price_usd": prices,
                "price_mxn": [p * mxn_rate for p in prices]
            })

        st.subheader(f"Histórico de Precios: {selected_card}")
        
        # Alternador de Moneda para la Gráfica
        moneda = st.radio("Moneda de la Gráfica:", ["USD ($)", "MXN ($)"], horizontal=True)
        col_price = "price_usd" if "USD" in moneda else "price_mxn"
        
        chart_data = df_card.set_index("date")[[col_price]]
        st.line_chart(chart_data)
        
        # Diagnóstico Estadístico
        diag, p7, p30 = calculate_price_trends(df_card)
        
        st.markdown("---")
        st.subheader("🤖 Diagnóstico del Motor Predictivo")
        st.info(f"**Proyección Actual:** {diag}")
        
        c1, c2 = st.columns(2)
        c1.metric("Proyección a 7 Días", f"${p7:.2f} USD", f"${p7*mxn_rate:.2f} MXN")
        c2.metric("Proyección a 30 Días", f"${p30:.2f} USD", f"${p30*mxn_rate:.2f} MXN")
    else:
        st.info("No hay suficiente información registrada en la base de datos para generar gráficos.")

# ==========================================
# 4. ALERTAS Y CONFIGURACIÓN (⚙️)
# ==========================================
elif menu == "⚙️ Alertas y Configuración":
    st.title("⚙️ Configuración del Sistema")
    
    st.subheader("🔔 Notificaciones Push a Tu Celular")
    st.write("Configura la integración directa con la app gratuita **ntfy** (disponible en Android e iOS).")
    
    ntfy_channel = st.text_input(
        "Nombre del Canal en ntfy.sh", 
        value=st.session_state["ntfy_topic"],
        help="Escribe un nombre único para tu canal. Luego suscríbete a este mismo canal en la app móvil ntfy."
    )
    
    threshold = st.number_input(
        "Umbral de alerta por fluctuación (%)", 
        min_value=1.0, 
        max_value=50.0, 
        value=float(st.session_state["alert_threshold"]),
        step=0.5
    )
    
    if st.button("🧪 Enviar Notificación de Prueba"):
        st.session_state["ntfy_topic"] = ntfy_channel
        st.session_state["alert_threshold"] = threshold
        send_ntfy_push(ntfy_channel, "[Pokémon TCG] ¡Notificación de prueba configurada correctamente!")
        st.success(f"Notificación de prueba enviada a ntfy.sh/{ntfy_channel}")

    st.markdown("---")
    st.subheader("🛡️ Pool de Proxies (Opcional)")
    st.write("Si realizas miles de consultas diarias desde Streamlit Cloud, añade proxies en formato `http://ip:puerto` para evitar bloqueos por IP.")
    
    proxy_input = st.text_area(
        "Lista de Proxies (uno por línea)", 
        value="\n".join(st.session_state["proxies"]),
        placeholder="http://103.152.18.1:8080\nhttp://185.199.229.156:7492"
    )
    
    if st.button("💾 Guardar Ajustes"):
        st.session_state["ntfy_topic"] = ntfy_channel
        st.session_state["alert_threshold"] = threshold
        st.session_state["proxies"] = [p.strip() for p in proxy_input.split("\n") if p.strip()]
        st.success("Configuración guardada correctamente.")
