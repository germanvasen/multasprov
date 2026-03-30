import streamlit as st
import pandas as pd
import time
import os
from bs4 import BeautifulSoup

# --- INSTALACIÓN DE PLAYWRIGHT EN STREAMLIT CLOUD ---
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

def scraping_multas(cuit):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # IMPORTANTE: Aquí vamos a "robarle" la sesión al navegador si es posible
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = context.new_page()

        page.goto(URL_PBA, wait_until="networkidle")

        st.write("📊 Procesando tu consulta en segundo plano...")
        
        # Simular búsqueda con el CUIT que ingresó el empleado
        page.click("a[href='#x-document']")
        page.select_option("#filtroIdTipoDocumento", "4") 
        page.fill("#filtroNroDocumento", cuit)
        
        # Aquí el sistema espera a que APAREZCAN RESULTADOS en la nube.
        # Como no inyectamos token, el empleado debe haber resuelto el captcha
        # y pulsado buscar en la ventana incrustada de arriba.
        timeout_seg = 30
        inicio = time.time()
        resultado_detectado = False
        while time.time() - inicio < timeout_seg:
            html = page.content()
            if "Nº de Acta:" in html or "Estado CAUSA:" in html:
                resultado_detectado = True
                break
            if "no posee infracciones" in html.lower() or "no registra infracciones" in html.lower():
                resultado_detectado = True
                break
            time.sleep(2)
            
        if not resultado_detectado:
            browser.close()
            return None, "No se detectaron multas. ¿Pulsaste 'Buscar' dentro del recuadro oficial?"

        todas_las_multas = []
        df_final = extraer_multas_desde_html(page.content())
        browser.close()
        return df_final, "Búsqueda completa."

# --- INTERFAZ STREAMLIT ---
st.set_page_config(page_title="Gestor Multas PBA", page_icon="🚔", layout="wide")

st.title("🚔 Gestor Online Multas PBA")
st.write("Completa el formulario en el recuadro gris de abajo y luego pulsa **'Descargar Excel'**.")

col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("1. Completa los datos en la Web Oficial:")
    # INCRUSTAMOS LA WEB OFICIAL EN UN IFRAME
    st.components.v1.iframe(URL_PBA, height=600, scrolling=True)

with col2:
    st.subheader("2. Obtener Reporte:")
    cuit = st.text_input("Vuelve a escribir el CUIT consultado:")
    
    if st.button("🚀 Generar Excel", type="primary"):
        if not cuit:
            st.warning("Escribe el CUIT para confirmar.")
        else:
            with st.spinner("Leyendo información remota..."):
                df, msj = scraping_multas(cuit)
                
                if df is None:
                    st.error(msj)
                elif df.empty:
                    st.success(f"✅ {msj}")
                else:
                    st.success(f"📈 ¡Éxito! Se encontraron {len(df)} infracciones.")
                    st.dataframe(df)
                    
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
