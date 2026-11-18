"""Unit testy adaptéra s falošným API — overujú, že pre každú verziu Zabbixu
sa pošlú správne polia/metódy a odpoveď sa normalizuje na jednotný tvar.
Beh: python -m unittest discover -s tests -v"""
import unittest

from _load import adapter


class FakeMethod:
    def __init__(self, result, calls, name):
        self.result, self.calls, self.name = result, calls, name

    def __call__(self, **params):
        self.calls.append((self.name, params))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeAPI:
    """api.<objekt>.<metóda>(**params) -> prednastavená odpoveď + záznam volaní."""

    def __init__(self, version, responses=None):
        self._version = version
        self.responses = responses or {}
        self.calls = []

    def api_version(self):
        return self._version

    def __getattr__(self, obj):
        outer = self

        class Namespace:
            def __getattr__(self, method):
                name = f'{obj}.{method}'
                return FakeMethod(outer.responses.get(name, []), outer.calls, name)

        return Namespace()

    def params(self, name):
        return next(p for n, p in self.calls if n == name)


class VersionParsing(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(adapter.parse_version('7.4.1'), (7, 4))
        self.assertEqual(adapter.parse_version('8.0.0beta1'), (8, 0))
        self.assertEqual(adapter.parse_version('6.0.40'), (6, 0))
        self.assertIsNone(adapter.parse_version('garbage'))
        self.assertIsNone(adapter.parse_version(None))

    def test_status(self):
        self.assertEqual(adapter.version_status((8, 0))[0], 'ok')
        self.assertEqual(adapter.version_status((7, 2))[0], 'untested')
        self.assertEqual(adapter.version_status((9, 0))[0], 'untested')
        self.assertEqual(adapter.version_status((5, 0))[0], 'unsupported')
        self.assertEqual(adapter.version_status(None)[0], 'untested')

    def test_tested_versions_are_parseable_and_supported(self):
        for v in adapter.TESTED_VERSIONS:
            parsed = adapter.parse_version(v)
            self.assertIsNotNone(parsed)
            self.assertGreaterEqual(parsed, adapter.MIN_SUPPORTED)


class CiMatrixInSync(unittest.TestCase):
    def test_ci_matrix_matches_tested_versions(self):
        import pathlib
        import re
        workflow = pathlib.Path(__file__).resolve().parent.parent / '.github' / 'workflows' / 'ci.yml'
        match = re.search(r'zabbix:\s*\[([^\]]+)\]', workflow.read_text(encoding='utf-8'))
        self.assertIsNotNone(match, 'zabbix matrix not found in ci.yml')
        in_ci = tuple(v.strip().strip("'\"") for v in match.group(1).split(','))
        self.assertEqual(in_ci, adapter.TESTED_VERSIONS)


class FetchHosts(unittest.TestCase):
    def test_modern_versions_use_proxy_groups_and_hostgroups(self):
        for version in ('7.0.5', '7.4.1', '8.0.0', '9.1.0'):
            api = FakeAPI(version, {'host.get': []})
            adapter.fetch_hosts(api)
            params = api.params('host.get')
            self.assertIn('monitored_by', params['output'], version)
            self.assertIn('proxy_groupid', params['output'], version)
            self.assertIn('active_available', params['output'], version)
            self.assertNotIn('proxy_hostid', params['output'], version)
            self.assertIn('selectHostGroups', params, version)
            self.assertNotIn('selectGroups', params, version)

    def test_legacy_6_0_uses_old_field_names(self):
        api = FakeAPI('6.0.40', {'host.get': []})
        adapter.fetch_hosts(api)
        params = api.params('host.get')
        self.assertIn('proxy_hostid', params['output'])
        self.assertNotIn('monitored_by', params['output'])
        self.assertNotIn('active_available', params['output'])
        self.assertIn('selectGroups', params)
        self.assertNotIn('selectHostGroups', params)

    def test_legacy_response_is_normalized(self):
        raw = [
            {'hostid': '1', 'host': 'a', 'proxy_hostid': '0', 'groups': [{'name': 'G'}]},
            {'hostid': '2', 'host': 'b', 'proxy_hostid': '55', 'groups': []},
        ]
        hosts = adapter.fetch_hosts(FakeAPI('6.0.1', {'host.get': raw}))
        self.assertEqual(hosts[0]['hostgroups'], [{'name': 'G'}])
        self.assertNotIn('groups', hosts[0])
        self.assertEqual((hosts[0]['monitored_by'], hosts[0]['proxyid']), ('0', '0'))
        self.assertEqual((hosts[1]['monitored_by'], hosts[1]['proxyid']), ('1', '55'))

    def test_modern_response_is_untouched(self):
        raw = [{'hostid': '1', 'monitored_by': '2', 'proxyid': '0',
                'proxy_groupid': '9', 'hostgroups': []}]
        hosts = adapter.fetch_hosts(FakeAPI('8.0.0', {'host.get': [dict(raw[0])]}))
        self.assertEqual(hosts, raw)

    def test_unknown_version_behaves_like_newest(self):
        api = FakeAPI('weird', {'host.get': []})
        adapter.fetch_hosts(api)
        self.assertIn('monitored_by', api.params('host.get')['output'])


class FetchProxyNames(unittest.TestCase):
    def test_modern_includes_groups(self):
        api = FakeAPI('7.4.0', {
            'proxy.get': [{'proxyid': '1', 'name': 'px'}],
            'proxygroup.get': [{'proxy_groupid': '2', 'name': 'pg'}],
        })
        self.assertEqual(adapter.fetch_proxy_names(api), {
            ('proxy', '1'): 'px', ('group', '2'): 'pg (proxy group)',
        })
        self.assertEqual(api.params('proxy.get')['output'], ['proxyid', 'name'])

    def test_proxygroup_failure_is_tolerated(self):
        api = FakeAPI('8.0.0', {
            'proxy.get': [{'proxyid': '1', 'name': 'px'}],
            'proxygroup.get': RuntimeError('boom'),
        })
        self.assertEqual(adapter.fetch_proxy_names(api), {('proxy', '1'): 'px'})

    def test_legacy_uses_host_field_and_skips_groups(self):
        api = FakeAPI('6.0.0', {'proxy.get': [{'proxyid': '1', 'host': 'old-px'}]})
        self.assertEqual(adapter.fetch_proxy_names(api), {('proxy', '1'): 'old-px'})
        self.assertEqual(api.params('proxy.get')['output'], ['proxyid', 'host'])
        self.assertFalse([n for n, _ in api.calls if n == 'proxygroup.get'])


class Problems(unittest.TestCase):
    def test_problem_params(self):
        api = FakeAPI('8.0.0', {'problem.get': []})
        adapter.fetch_problems(api, 3, False, hostids=[7])
        params = api.params('problem.get')
        self.assertEqual(params['severities'], [3, 4, 5])
        self.assertEqual(params['hostids'], [7])
        self.assertIs(params['suppressed'], False)

    def test_include_suppressed_omits_filter(self):
        api = FakeAPI('8.0.0', {'problem.get': []})
        adapter.fetch_problems(api, 2, True)
        self.assertNotIn('suppressed', api.params('problem.get'))
        self.assertNotIn('hostids', api.params('problem.get'))

    def test_trigger_hosts_mapping_and_empty(self):
        api = FakeAPI('8.0.0', {'trigger.get': [
            {'triggerid': '10', 'hosts': [{'hostid': '1'}, {'hostid': '2'}]},
        ]})
        self.assertEqual(adapter.fetch_trigger_hosts(api, [{'objectid': '10'}]),
                         {'10': ['1', '2']})
        self.assertEqual(adapter.fetch_trigger_hosts(FakeAPI('8.0.0'), []), {})

    def test_history_adds_r_clock(self):
        api = FakeAPI('7.0.0', {'event.get': [
            {'eventid': '1', 'r_eventid': '2', 'clock': '100'},
            {'eventid': '3', 'r_eventid': '0', 'clock': '200'},
        ]})
        # druhé volanie event.get (resolution) vráti tú istú odpoveď — stačí
        # na overenie, že sa r_clock doplní / default '0'
        events = adapter.fetch_problem_history(api, 5, 2, 0, 1000, 500)
        self.assertEqual(events[1]['r_clock'], '0')
        self.assertIn('r_clock', events[0])


class HostTags(unittest.TestCase):
    def test_missing_host_returns_none(self):
        self.assertIsNone(adapter.fetch_host_tags(FakeAPI('8.0.0', {'host.get': []}), 1))

    def test_inventory_empty_list_becomes_dict(self):
        api = FakeAPI('8.0.0', {'host.get': [{'inventory': []}]})
        self.assertEqual(adapter.fetch_host_inventory(api, 1), {})


if __name__ == '__main__':
    unittest.main()
