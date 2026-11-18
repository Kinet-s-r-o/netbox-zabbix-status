from django.core.management.base import BaseCommand, CommandError

from netbox_zabbix_status import zabbix_adapter as adapter
from netbox_zabbix_status.zabbix import ZabbixConfigError, get_client, get_config


class Command(BaseCommand):
    help = (
        'Checks the connection to the Zabbix API and prints the version, '
        'whether it is a tested version, and basic counts.'
    )

    def handle(self, *args, **options):
        cfg = get_config()
        try:
            api = get_client()
        except ZabbixConfigError as e:
            raise CommandError(str(e))

        try:
            raw_version = api.api_version()
            version = adapter.parse_version(raw_version)
            host_count = api.host.get(countOutput=True)
            problem_count = api.problem.get(countOutput=True)
        except Exception as e:
            raise CommandError(f'Zabbix API call failed: {e}')

        self.stdout.write(self.style.SUCCESS(f"Zabbix API OK: {cfg['api_url']}"))
        self.stdout.write(f'  API version:     {raw_version}')
        self.stdout.write(f'  hosts:           {host_count}')
        self.stdout.write(f'  active problems: {problem_count}')

        level, message = adapter.version_status(version)
        if level == 'ok':
            self.stdout.write(f'  compatibility:   {message}')
        else:
            self.stdout.write(self.style.WARNING(f'  compatibility:   {message}'))
