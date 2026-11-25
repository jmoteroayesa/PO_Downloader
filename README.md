# Búsqueda y descarga de logs de PO
### Búsqueda y descarga de logs de PO en entornos inferiores a POP 

## Web UI local (Streamlit)
`pip install -r requirements.txt` y luego `streamlit run app.py`. Elige ICO y rango de fechas; el texto es opcional (búsqueda local, literal, sin distinguir mayúsculas, solo en la primera versión del log). Solo se guardan los mensajes coincidentes. La contraseña no se guarda. La CLI (`sap_po_download.py`) sigue funcionando igual. Tests: `python -m unittest test_po_explorer`.
