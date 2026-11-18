"""Načíta zabbix_adapter priamo zo súboru — bez importu balíka
netbox_zabbix_status (jeho __init__ vyžaduje NetBox). Adapter je zámerne
bez závislosti na Django/NetBoxe, takže testy bežia v čistom Pythone."""
import importlib.util
import pathlib

_PATH = pathlib.Path(__file__).resolve().parent.parent / 'netbox_zabbix_status' / 'zabbix_adapter.py'
_spec = importlib.util.spec_from_file_location('zabbix_adapter', _PATH)
adapter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adapter)
