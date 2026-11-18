"""Integračný test adaptéra proti ŽIVÉMU Zabbixu (bez NetBoxu).

Zabbix API validuje parametre aj keď je výsledok prázdny, takže už samotné
zavolanie každého dopytu odhalí premenované/odstránené polia a metódy. Test
navyše vytvorí skúšobný host a overí zápis/čítanie tagov (host.update).

Premenné prostredia:
  ZABBIX_URL      napr. http://localhost:8080  (default)
  ZABBIX_USER     Admin     (default)
  ZABBIX_PASSWORD zabbix    (default)
  EXPECT_VERSION  napr. 8.0 — test zlyhá, ak server hlási inú verziu

Beh: pip install zabbix_utils && python tests/integration_check.py
"""
import os
import sys
import time

from _load import adapter
from zabbix_utils import ZabbixAPI

URL = os.environ.get('ZABBIX_URL', 'http://localhost:8080').rstrip('/')
USER = os.environ.get('ZABBIX_USER', 'Admin')
PASSWORD = os.environ.get('ZABBIX_PASSWORD', 'zabbix')
EXPECT = os.environ.get('EXPECT_VERSION')
WAIT_SECONDS = int(os.environ.get('WAIT_SECONDS', '420'))

# Kľúče, ktoré zvyšok pluginu (sync.py) z normalizovaného hosta číta
REQUIRED_HOST_KEYS = {
    'hostid', 'host', 'name', 'status', 'maintenance_status', 'hostgroups',
    'interfaces', 'parentTemplates', 'tags', 'monitored_by', 'proxyid', 'proxy_groupid',
}


def connect():
    """Zabbix po štarte kontajnerov dlho inicializuje DB — čakaj na API."""
    deadline = time.monotonic() + WAIT_SECONDS
    last_error = None
    while time.monotonic() < deadline:
        try:
            return ZabbixAPI(url=URL, user=USER, password=PASSWORD, validate_certs=False)
        except Exception as e:  # noqa: BLE001 — čakáme na štart servera
            last_error = e
            time.sleep(5)
    sys.exit(f'Zabbix API at {URL} not ready after {WAIT_SECONDS}s: {last_error}')


def check(condition, message):
    if not condition:
        sys.exit(f'FAIL: {message}')
    print(f'  ok: {message}')


def main():
    api = connect()
    version = adapter.detect_version(api)
    print(f'Connected to Zabbix API {api.api_version()} (parsed: {version})')

    if EXPECT:
        check(version == adapter.parse_version(EXPECT),
              f'server reports {EXPECT}.x (got {api.api_version()})')
    level, message = adapter.version_status(version)
    check(level == 'ok', message)

    group_id = api.hostgroup.create(name='nbzs-integration-test')['groupids'][0]
    host_id = None
    try:
        host_id = api.host.create(
            host='nbzs-integration-test',
            groups=[{'groupid': group_id}],
            interfaces=[{
                'type': 1, 'main': 1, 'useip': 1,
                'ip': '127.0.0.1', 'dns': '', 'port': '10050',
            }],
            tags=[{'tag': 'class', 'value': 'keep'}],
        )['hostids'][0]

        hosts = adapter.fetch_hosts(api, version)
        mine = next((h for h in hosts if h['hostid'] == host_id), None)
        check(mine is not None, 'fetch_hosts returns the created host')
        check(REQUIRED_HOST_KEYS <= set(mine), f'host has all normalized keys {sorted(REQUIRED_HOST_KEYS)}')
        check([g['name'] for g in mine['hostgroups']] == ['nbzs-integration-test'],
              'hostgroups normalized')
        check(mine['monitored_by'] == '0', 'monitored_by normalized (server)')

        names = adapter.fetch_proxy_names(api, version)
        check(isinstance(names, dict), 'fetch_proxy_names works')

        problems = adapter.fetch_problems(api, 2, False)
        check(isinstance(problems, list), 'fetch_problems works')
        check(isinstance(adapter.fetch_problems(api, 2, True, hostids=[int(host_id)]), list),
              'fetch_problems with hostids/include_suppressed works')
        check(adapter.fetch_trigger_hosts(api, problems) is not None, 'fetch_trigger_hosts works')

        now = int(time.time())
        history = adapter.fetch_problem_history(api, int(host_id), 2, now - 86400, now, 500)
        check(isinstance(history, list), 'fetch_problem_history works')

        check(isinstance(adapter.fetch_host_inventory(api, int(host_id)), dict),
              'fetch_host_inventory works')

        # zápis tagov: existujúce ostávajú, nový sa pridá (ako update_host_tags)
        current = adapter.fetch_host_tags(api, int(host_id))
        check(current is not None and any(t['tag'] == 'class' for t in current),
              'fetch_host_tags reads existing tag')
        merged = [{'tag': t['tag'], 'value': t['value']} for t in current]
        merged.append({'tag': 'nbzs', 'value': 'written'})
        adapter.set_host_tags(api, int(host_id), merged)
        after = {t['tag']: t['value'] for t in adapter.fetch_host_tags(api, int(host_id))}
        check(after == {'class': 'keep', 'nbzs': 'written'}, 'set_host_tags round-trip')
    finally:
        if host_id:
            api.host.delete([host_id])
        api.hostgroup.delete([group_id])

    print('ALL CHECKS PASSED')


if __name__ == '__main__':
    main()
