import streamlit as st
import pandas as pd
import time
import os
import subprocess
from bs4 import BeautifulSoup

# --- OBLIGATORIO PARA DEPLOY EN STREAMLIT CLOUD ---
# Streamlit Cloud necesita instalar los navegadores de Playwright la primera vez que arranca.
@st.cache_resource
def install_playwright():
    try:
        os.system("playwright install chromium")
        # En Streamlit Cloud Linux a veces faltan dependencias, `install-deps` ayuda
        os.system("playwright install-deps chromium") 
    except Exception as e:
        st.error(f"Error instalando navegadores de Playwright: {e}")

install_playwright()

from playwright.sync_api import sync_playwright

# --- CONFIGURACIÓN ---
# DEBES CAMBIAR ESTA LLAVE POR LA QUE ENCUENTRES EN EL CÓDIGO FUENTE DE LA PÁGINA
SITEKEY_CAPTCHA = "6LeGXnkUAAAAAGHv-jMgqrOMx4eqHCh3_fEeP9wR" 
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
        importe = extraer(r"Importe:\s*\$\s*([0-9.,]+)", texto)
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
            "Vencimiento": vencimiento, "Importe": importe, "Estado cupón": estado_cupon,
            "Estado causa": estado_causa, "Código": codigo, "Descripción": descripcion,
            "Ubicación": ubicacion, "Radicación": radicacion,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates().reset_index(drop=True)
    return df

def scraping_multas(cuit, g_recaptcha_response):
    with sync_playwright() as p:
        # IMPORTANTE: En Streamlit Cloud SIEMPRE debe ser headless=True
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(URL_PBA, wait_until="networkidle")

        # Ir a la pestaña validación por documento
        page.click("a[href='#x-document']")
        page.select_option("#filtroIdTipoDocumento", "4") # CUIT
        page.fill("#filtroNroDocumento", cuit)
        
        # ACA ESTÁ LA MAGIA: Inyectamos el token que resolvemos en Streamlit
        # hacia el navegador invisible en la nube
        st.write("🔑 Inyectando token en el sistema de PBA...")
        page.evaluate(f"document.getElementById('g-recaptcha-response').innerHTML = '{g_recaptcha_response}';")
        
        # Simular que pasamos el recaptcha para que habilite cosas en el front si es necesario
        # page.evaluate(f"onSubmitCaptcha('{g_recaptcha_response}');") # A veces tienen una funcion asi
        
        # Click en buscar
        page.click("#btnConsultar.hidden-xs") # o el selector del botón buscar
        
        # Esperar resultados
        st.write("⏳ Esperando respuesta del gobierno (puede tardar 15s)...")
        timeout_seg = 30
        inicio = time.time()
        resultado_detectado = False
        while time.time() - inicio < timeout_seg:
            html = page.content()
            if "Nº de Acta:" in html or "Estado CAUSA:" in html or "No posee infracciones" in html or "no registra infracciones" in html.lower():
                resultado_detectado = True
                break
            time.sleep(2)
            
        if not resultado_detectado:
            browser.close()
            return None, "Tiempo de espera agotado. Probablemente el Captcha fue rechazado."

        # Extraer todo
        todas_las_multas = []
        pagina_actual = 1
        
        while True:
            # Expandir todo
            try:
                botones = page.locator("a.expand")
                for i in range(botones.count()):
                    botones.nth(i).click(timeout=1000)
            except:
                pass
            time.sleep(2)
            
            df_parcial = extraer_multas_desde_html(page.content())
            if not df_parcial.empty:
                todas_las_multas.append(df_parcial)

            # Siguiente pagina
            siguiente_btn = page.locator(f"a:text-is('{pagina_actual + 1}'), button:text-is('{pagina_actual + 1}')").first
            try:
                if siguiente_btn.count() > 0 and siguiente_btn.is_visible():
                    siguiente_btn.click(timeout=3000)
                    time.sleep(3)
                    pagina_actual += 1
                else:
                    break
            except:
                break
                
        browser.close()
        
        if todas_las_multas:
            return pd.concat(todas_las_multas, ignore_index=True), "Mostrando Multas"
        else:
            return pd.DataFrame(), "No se encontraron multas."

# --- INTERFAZ DE STREAMLIT ---
st.set_page_config(page_title="Consultor PBA", page_icon="🚔", layout="centered")

st.title("🚔 Consultor Multas PBA en la Nube")
st.write("Para evitar bloqueos, por favor resuelve el captcha manualmente a continuación:")

cuit = st.text_input("Ingresa el CUIT de la persona/empresa:")

# INYECTAMOS EL HTML PARA QUE SE CREE EL CAPTCHA VISUAL
st.components.v1.html(f'''
    <html>
        <head>
            <script src="https://www.google.com/recaptcha/api.js" async defer></script>
            <script>
                function captchaCallback(token) {{
                    // Cuando resuelva el captcha, se lo mostramos para que lo copie
                    document.getElementById("txd").value = token;
                    navigator.clipboard.writeText(token); // Intenta copiarlo automaticamente
                    document.getElementById("msg").innerHTML = "✅ ¡Resuelto! El token ha sido copiado a tu portapapeles (Ctrl+V). Opcional: cópialo de aquí abajo.";
                }}
            </script>
        </head>
        <body style="font-family: sans-serif;">
            <div class="g-recaptcha" data-sitekey="{SITEKEY_CAPTCHA}" data-callback="captchaCallback"></div>
            <p id="msg" style="color: green; font-weight: bold; font-size: 14px;"></p>
            <textarea id="txd" style="width:100%; height:50px;" readonly placeholder="El token secreto aparecerá aquí..."></textarea>
        </body>
    </html>
''', height=200)

st.write("---")
token = st.text_area("Pega el Token aquí (Haz clic y presiona Ctrl+V):", height=100, help="Es un texto larguísimo que demuestra que no eres un robot.")

if st.button("Buscar en la Nube", type="primary"):
    if not cuit:
        st.warning("Debes ingresar un CUIT.")
    elif not token or len(token) < 50:
        st.error("Debes resolver el captcha y pegar el Token arriba.")
    else:
        with st.spinner("Conectando servidor remoto e inyectando Token..."):
            df, msj = scraping_multas(cuit, token)
            
            if df is None:
                st.error(msj)
            elif df.empty:
                st.success("Búsqueda exitosa. La patente no registra infracciones.")
            else:
                st.success(f"¡Se encontraron {len(df)} infracciones!")
                st.dataframe(df)
                
                # Botón para descargar Excel simple
                from io import BytesIO
                output = BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Multas')
                excel_data = output.getvalue()
                
                st.download_button(
                    label="📥 Descargar Reporte Completo en Excel",
                    data=excel_data,
                    file_name=f"multas_{cuit}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
