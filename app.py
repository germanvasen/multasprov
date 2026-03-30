import streamlit as st
import pandas as pd
import time
import os
from bs4 import BeautifulSoup

# --- INSTALACIÓN DE PLAYWRIGHT EN LA NUBE ---
@st.cache_resource
def install_playwright():
    try:
        os.system("playwright install chromium")
        os.system("playwright install-deps chromium") 
    except Exception as e:
        st.error(f"Error instalando navegadores: {e}")

install_playwright()

from playwright.sync_api import sync_playwright

# --- CONFIGURACIÓN ---
URL_PBA = "https://infraccionesba.gba.gob.ar/consulta-infraccion"

def extraer_multas_desde_html(html: str) -> pd.DataFrame:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    panels = soup.select(".panel.panel-default")
    
    for panel in panels:
        texto = panel.get_text(" ", strip=True)
        if "Nº de Acta:" not in texto and "N° de Acta:" not in texto:
            continue

        def extraer(regex, text, default=""):
            import re
            m = re.search(regex, text, re.IGNORECASE)
            return m.group(1).strip() if m else default

        acta = extraer(r"N[º°]\s*de\s*Acta:\s*([A-Z0-9\-]+)", texto)
        dominio = extraer(r"Dominio:\s*([A-Z0-9]+)", texto)
        generacion = extraer(r"Generaci[oó]n:\s*([0-9/]+)", texto)
        vencimiento = extraer(r"Vencimiento:\s*([0-9/]+)", texto)
        importe_raw = extraer(r"Importe:\s*\$\s*([0-9.,]+)", texto)
        estado_cupon = extraer(r"Estado\s+CUP[ÓO]N:\s*(.*?)(?:Estado\s+CAUSA:|Importe:)", texto)
        estado_causa = extraer(r"Estado\s+CAUSA:\s*(.*?)(?:Importe:|$)", texto)

        codigo = ""
        descripcion = ""
        ubicacion = ""
        radicacion = ""

        body = panel.select_one(".panel-collapse, .panel-body")
        if body:
            textos_body = list(body.stripped_strings)
            body_text = " ".join(textos_body)
            radicacion = extraer(r"Radicaci[oó]n\s+de\s+la\s+causa:\s*([A-ZÁÉÍÓÚÑa-záéíóúñ ]+)", body_text)
            
            if textos_body:
                primer_texto = textos_body[0]
                import re
                m_cod = re.match(r"^(\d+)\s*[-]\s*(.*)", primer_texto)
                if m_cod:
                    codigo = m_cod.group(1).strip()
                    descripcion = m_cod.group(2).strip()
                else:
                    descripcion = primer_texto.strip()
                    
            for i, txt in enumerate(textos_body):
                txt_lower = txt.lower()
                if "lugar" in txt_lower or "ubicaci" in txt_lower or "lugar de" in txt_lower:
                    if ":" in txt and len(txt.split(":", 1)[1].strip()) > 3:
                        ubicacion = txt.split(":", 1)[1].strip()
                    elif i + 1 < len(textos_body):
                        sig = textos_body[i+1].strip()
                        if not sig.lower().startswith("impresi") and not sig.lower().startswith("fecha") and ":" not in sig:
                            ubicacion = sig
                    break

        rows.append({
            "Acta": acta, "Dominio": dominio, "Generación": generacion,
            "Vencimiento": vencimiento, "Importe": importe_raw, "Estado cupón": estado_cupon,
            "Estado causa": estado_causa, "Código": codigo, "Descripción": descripcion,
            "Ubicación": ubicacion, "Radicación": radicacion,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates().reset_index(drop=True)
    return df

def scraping_multas(cuit, g_recaptcha_response):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(URL_PBA, wait_until="networkidle")

        # Configurar búsqueda por CUIT
        page.click("a[href='#x-document']")
        page.select_option("#filtroIdTipoDocumento", "4") 
        page.fill("#filtroNroDocumento", cuit)
        
        # INYECCIÓN DEL TOKEN CAPTURADO MANUALMENTE
        st.write("🔑 Inyectando token en el servidor de PBA...")
        page.evaluate(f"document.getElementById('g-recaptcha-response').innerHTML = '{g_recaptcha_response}';")
        
        # Click en el botón de búsqueda (usando el id correcto de PBA)
        page.click("#btnConsultar") 
        
        # Esperar resultados o mensaje de error
        st.write("⏳ Procesando consulta remota...")
        timeout_seg = 25
        inicio = time.time()
        resultado_detectado = False
        while time.time() - inicio < timeout_seg:
            html = page.content()
            if "Nº de Acta:" in html or "Estado CAUSA:" in html or "Error" in html:
                resultado_detectado = True
                break
            if "no posee infracciones" in html.lower() or "no registra infracciones" in html.lower():
                resultado_detectado = True
                break
            time.sleep(2)
            
        if not resultado_detectado:
            browser.close()
            return None, "El servidor de la provincia no respondió. Es posible que el token haya expirado (duran 2 minutos)."

        todas_las_multas = []
        pagina_actual = 1
        
        # Bucle de paginación
        while True:
            # Expandir detalles
            try:
                botones = page.locator("a.expand")
                for i in range(botones.count()):
                    botones.nth(i).click(timeout=1000)
            except: pass
            
            time.sleep(1.5)
            df_parcial = extraer_multas_desde_html(page.content())
            if not df_parcial.empty:
                todas_las_multas.append(df_parcial)

            # Intentar ir a la siguiente página
            siguiente_btn = page.locator(f"a:text-is('{pagina_actual + 1}'), button:text-is('{pagina_actual + 1}')").first
            try:
                if siguiente_btn.count() > 0 and siguiente_btn.is_visible():
                    siguiente_btn.click(timeout=3000)
                    time.sleep(3)
                    pagina_actual += 1
                else: break
            except: break
                
        browser.close()
        
        if todas_las_multas:
            return pd.concat(todas_las_multas, ignore_index=True), "Búsqueda completa."
        else:
            return pd.DataFrame(), "No se encontraron infracciones para este CUIT."

# --- INTERFAZ STREAMLIT ---
st.set_page_config(page_title="Consultor PBA Nube", page_icon="🚔")

st.title("🚔 Consultor Multas PBA (Nube)")
st.info("💡 **Instrucciones para el empleado:**\n1. Ve a la [Web de PBA](https://infraccionesba.gba.gob.ar/consulta-infraccion) y resuelve el 'No soy un robot'.\n2. Abre la consola (F12) y pega esto: `copy(document.getElementById('g-recaptcha-response').value)`\n3. Pega el resultado aquí abajo y dale a Buscar.")

cuit = st.text_input("Ingresa el CUIT:", placeholder="30714561762")
token = st.text_area("Pega el Token de Google aquí (Ctrl+V):", height=100)

if st.button("🚀 Iniciar Scraping en la Nube", type="primary"):
    if not cuit or not token:
        st.warning("Faltan datos (CUIT o Token).")
    else:
        with st.spinner("Ejecutando navegador invisible en el servidor..."):
            df, msj = scraping_multas(cuit, token)
            
            if df is None:
                st.error(msj)
            elif df.empty:
                st.success(f"✅ {msj}")
            else:
                st.success(f"📈 ¡Éxito! Se encontraron {len(df)} infracciones.")
                st.dataframe(df)
                
                # Descargar Excel
                from io import BytesIO
                output = BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False)
                st.download_button(
                    label="📥 Descargar Reporte (.xlsx)",
                    data=output.getvalue(),
                    file_name=f"multas_{cuit}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
