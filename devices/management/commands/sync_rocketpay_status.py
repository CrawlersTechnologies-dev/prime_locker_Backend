"""
Management command to poll RocketPay SuperKey API for status, lock state, and action completion.
RP2, RP3, RP4: Polls GET /superkey/keys/v5/<key_id> and GET /superkey/keys/<key_id>/actions.
Can be executed as a periodic cron job (every 5-10 minutes) or Celery beat task.
Usage: python manage.py sync_rocketpay_status
"""
from django.core.management.base import BaseCommand
from devices.models import Device, LockProvider
from devices.services.rocketpay_service import RocketPayService


class Command(BaseCommand):
    help = "Polls RocketPay SuperKey v5 API and action queue to synchronize real device lock status and enrollment journey."

    def add_arguments(self, parser):
        parser.add_argument('--imei', type=str, help='Sync a specific device by IMEI')

    def handle(self, *args, **options):
        imei = options.get('imei')
        qs = Device.objects.filter(lock_provider=LockProvider.ROCKETPAY).exclude(provider_device_id__isnull=True).exclude(provider_device_id='')
        if imei:
            qs = qs.filter(imei=imei)

        total = qs.count()
        self.stdout.write(f"Starting RocketPay status sync for {total} device(s)...")

        synced = 0
        failed = 0

        for dev in qs:
            res = RocketPayService.sync_device_status(dev)
            if res.get('success'):
                synced += 1
                self.stdout.write(self.style.SUCCESS(
                    f"[{dev.imei}] Synced: status={res.get('device_status')}, provider_status={res.get('provider_status')}, journey={res.get('journey')}"
                ))
            else:
                failed += 1
                self.stdout.write(self.style.WARNING(
                    f"[{dev.imei}] Sync failed: {res.get('error')}"
                ))

        self.stdout.write(self.style.SUCCESS(f"Completed sync. {synced} synced, {failed} failed."))
