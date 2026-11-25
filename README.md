# Búsqueda y descarga de logs de PO
### Búsqueda y descarga de logs de PO en entornos inferiores a POP 

## Web UI local (Streamlit)
`python -m pip install -r requirements.txt` y luego `python -m streamlit run app.py` (no requiere que `streamlit` esté en el PATH; si `python` no se reconoce, usa `py`). Elige ICO y rango de fechas; el texto es opcional (búsqueda local, literal, sin distinguir mayúsculas, solo en la primera versión del log). Solo se guardan los mensajes coincidentes. La contraseña no se guarda. La CLI (`sap_po_download.py`) sigue funcionando igual. Tests: `python -m unittest test_po_explorer`.
