"""Adapter nad Zabbix API — JEDINÉ miesto, kde sa zostavujú API dopyty.

Všetko, čo sa medzi verziami Zabbixu líši (názvy polí, existencia metód),
je tu: tabuľky verzií nižšie + normalizácia odpovedí na jednotný tvar. Zvyšok
pluginu (sync.py, zabbix.py, views) pracuje len s normalizovaným tvarom a o
verzii Zabbixu nič nevie.

Modul je zámerne bez závislosti na Django/NetBoxe — berie `api` objekt
(zabbix_utils.ZabbixAPI) ako parameter. Vďaka tomu ho vie CI (tests/) spustiť
proti čistému Zabbixu každej podporovanej verzie bez NetBoxu.

Ako pridať novú verziu Zabbixu:
  1. Prejdi "Changes from X to Y" v API dokumentácii Zabbixu a porovnaj ju
     s metódami/poľami nižšie.
  2. Ak sa niečo zmenilo, uprav JEDNU funkciu/tabuľku tu (a normalizáciu).
  3. Pridaj verziu do TESTED_VERSIONS (CI matrix si ju berie odtiaľto).
Neznáma (novšia) verzia sa správa ako najnovšia známa — zrejme funguje, a
`version_status()` na to upozorní.
"""
import logging
import re

logger = logging.getLogger('netbox.plugins.netbox_zabbix_status')

# Najstaršia verzia, ktorú adapter vie obslúžiť (zabbix_utils: Zabbix 6.0+).
MIN_SUPPORTED = (6, 0)
# Verzie overené integračným testom v CI (tests/integration_check.py).
# Zdroj pravdy aj pre CI matrix a pre hlásenie v `zabbix_check`.
TESTED_VERSIONS = ('6.0', '7.0', '7.4', '8.0')

Version = tuple  # (major, minor)


def parse_version(raw) -> 'Version | None':
    """'7.4.1' / '8.0.0beta1' / APIVersion -> (7, 4). Nerozpoznané -> None."""
    match = re.match(r'\s*(\d+)\.(\d+)', str(raw or ''))
    return (int(match.group(1)), int(match.group(2))) if match else None


def detect_version(api) -> 'Version | None':
    """Verzia Zabbix API z už pripojeného klienta (zabbix_utils si ju pri
    pripojení sám načíta a drží, takže to nie je ďalší request)."""
    try:
        return parse_version(api.api_version())
    except Exception:
        logger.warning('Could not detect Zabbix API version', exc_info=True)
        return None


def version_status(version) -> 'tuple[str, str]':
    """(level, message) pre `zabbix_check` a log. level: ok | untested | unsupported."""
    if version is None:
        return 'untested', 'Zabbix API version could not be detected; assuming the newest known API.'
    label = f'{version[0]}.{version[1]}'
    if version < MIN_SUPPORTED:
        return 'unsupported', (
            f'Zabbix {label} is older than the minimum supported '
            f'{MIN_SUPPORTED[0]}.{MIN_SUPPORTED[1]}; sync will probably fail.'
        )
    tested = [parse_version(v) for v in TESTED_VERSIONS]
    if version in tested:
        return 'ok', f'Zabbix {label} is tested with this plugin.'
    return 'untested', (
        f'Zabbix {label} is not in the tested set ({", ".join(TESTED_VERSIONS)}); '
        'it will most likely work, but it has not been verified.'
    )


def _at_least(version, major, minor) -> bool:
    """Neznáma verzia = správaj sa ako najnovšia."""
    return version is None or version >= (major, minor)


def _ver(api, version):
    return version if version is not None else detect_version(api)


# --- hosty ---------------------------------------------------------------

def fetch_hosts(api, version=None) -> list:
    """host.get v normalizovanom tvare: vždy kľúče 'hostgroups', 'proxyid',
    'proxy_groupid', 'monitored_by' (0 server / 1 proxy / 2 proxy group)."""
    ver = _ver(api, version)
    output = ['hostid', 'host', 'name', 'status', 'maintenance_status']
    if _at_least(ver, 6, 2):
        output.append('active_available')  # pribudlo v 6.2
    if _at_least(ver, 7, 0):
        output += ['proxyid', 'proxy_groupid', 'monitored_by']  # proxy groups = 7.0
    else:
        output.append('proxy_hostid')

    params = dict(
        output=output,
        selectInterfaces=['type', 'ip', 'dns', 'port', 'useip', 'available'],
        selectParentTemplates=['name'],
        selectTags='extend',
    )
    # selectGroups sa v 6.2 premenovalo na selectHostGroups
    params['selectHostGroups' if _at_least(ver, 6, 2) else 'selectGroups'] = ['name']

    return [_normalize_host(h) for h in api.host.get(**params)]


def _normalize_host(host: dict) -> dict:
    if 'hostgroups' not in host and 'groups' in host:
        host['hostgroups'] = host.pop('groups')
    if 'monitored_by' not in host:
        proxy = host.pop('proxy_hostid', None)
        monitored = proxy not in (None, '', '0')
        host['monitored_by'] = '1' if monitored else '0'
        host['proxyid'] = proxy if monitored else '0'
        host['proxy_groupid'] = '0'
    return host


def fetch_proxy_names(api, version=None) -> dict:
    """{('proxy', id): name, ('group', id): 'name (proxy group)'}."""
    ver = _ver(api, version)
    names = {}
    if _at_least(ver, 7, 0):
        for proxy in api.proxy.get(output=['proxyid', 'name']):
            names[('proxy', proxy['proxyid'])] = proxy['name']
        try:
            for group in api.proxygroup.get(output=['proxy_groupid', 'name']):
                names[('group', group['proxy_groupid'])] = f"{group['name']} (proxy group)"
        except Exception:
            logger.debug('proxygroup.get failed — skipping proxy groups', exc_info=True)
    else:
        # do 6.4 má proxy názov v poli 'host', 'name' pribudlo v 7.0
        for proxy in api.proxy.get(output=['proxyid', 'host']):
            names[('proxy', proxy['proxyid'])] = proxy['host']
    return names


def fetch_host_inventory(api, hostid: int) -> dict:
    """GPS súradnice hosta z inventory; {} keď je inventory vypnuté."""
    hosts = api.host.get(
        hostids=[hostid],
        output=[],
        selectInventory=['location_lat', 'location_lon'],
    )
    # Zabbix vráti 'inventory': [] (nie {}) keď má host vypnuté inventory
    return (hosts[0].get('inventory') or {}) if hosts else {}


def fetch_host_tags(api, hostid: int) -> 'list | None':
    """Tagy hosta ako ich vráti Zabbix (vrátane poľa 'automatic');
    None = host neexistuje."""
    hosts = api.host.get(hostids=[hostid], output=[], selectTags='extend')
    if not hosts:
        return None
    return hosts[0].get('tags') or []


def set_host_tags(api, hostid: int, tags: list) -> None:
    """Nahradí CELÉ pole tagov hosta (host.update tak funguje vo všetkých
    verziách). `tags`: [{'tag': ..., 'value': ...}] — bez poľa 'automatic'."""
    api.host.update(hostid=hostid, tags=tags)


# --- problémy ------------------------------------------------------------

def fetch_problems(api, min_severity: int, include_suppressed: bool, hostids=None) -> list:
    params = dict(
        # objectid = triggerid (source=trigger je jediný typ problémov, ktoré
        # sledujeme) — potrebné na mapovanie na hosta a na odkaz tr_events.php
        output=['eventid', 'objectid', 'name', 'severity', 'acknowledged',
                'suppressed', 'clock', 'opdata'],
        selectTags='extend',
        severities=list(range(min_severity, 6)),
        recent=False,
        sortfield='eventid',
    )
    if hostids is not None:
        params['hostids'] = hostids
    if not include_suppressed:
        # Parita so Zabbix UI: problémy hostov v maintenance sa defaultne skrývajú
        params['suppressed'] = False
    return api.problem.get(**params)


def fetch_trigger_hosts(api, problems: list) -> dict:
    """{triggerid: [hostid, ...]} — problem.objectid je triggerid."""
    trigger_ids = list({p['objectid'] for p in problems})
    if not trigger_ids:
        return {}
    triggers = api.trigger.get(
        triggerids=trigger_ids,
        output=['triggerid'],
        selectHosts=['hostid'],
    )
    return {
        t['triggerid']: [h['hostid'] for h in t.get('hosts', [])]
        for t in triggers
    }


def fetch_problem_history(api, hostid: int, min_severity: int,
                          time_from: int, time_till: int, limit: int) -> list:
    """História problémov hosta vzniknutých v okne [time_from, time_till].

    event.get s value=1 vráti jeden riadok na vznik problému a jeho r_eventid
    (id "resolution" udalosti, 0 = stále aktívny) — ale nie priamo čas
    vyriešenia (r_clock nie je platné output pole event.get). Čas vyriešenia
    sa preto dotiahne druhým, dávkovým dopytom a doplní sa ako 'r_clock'."""
    events = api.event.get(
        hostids=[hostid],
        source=0,  # EVENT_SOURCE_TRIGGERS
        object=0,  # EVENT_OBJECT_TRIGGER
        value=1,   # riadky vzniku problému
        time_from=time_from,
        time_till=time_till,
        output=['eventid', 'objectid', 'name', 'severity', 'clock', 'r_eventid',
                'acknowledged'],
        selectTags='extend',
        severities=list(range(min_severity, 6)),
        sortfield=['clock'],
        sortorder='DESC',
        limit=limit,
    )

    resolution_ids = sorted({
        e['r_eventid'] for e in events if e.get('r_eventid') not in (None, '0')
    })
    resolution_clocks = {}
    if resolution_ids:
        resolutions = api.event.get(eventids=resolution_ids, output=['eventid', 'clock'])
        resolution_clocks = {r['eventid']: r['clock'] for r in resolutions}
    for e in events:
        e['r_clock'] = resolution_clocks.get(e.get('r_eventid'), '0')
    return events
