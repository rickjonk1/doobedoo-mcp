"""
mcp_server/content.py — sleutel voor eigen MCP-inhoudsblokken
==============================================================
Staat apart zodat zowel de protocollaag als de tools hem kunnen gebruiken
zonder elkaar te importeren: protocol.py importeert tools.py, en tools.py
importeert documents.py.

Een tool die iets anders dan tekst wil teruggeven (een pagina als afbeelding,
bijvoorbeeld) zet de MCP-blokken onder deze sleutel in zijn resultaat. De
protocollaag haalt ze eruit en stuurt de rest als samenvattende JSON mee.
"""

CONTENT_KEY = "_mcp_content"
