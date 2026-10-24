"""
Management command to auto-lock devices with overdue EMI installments past the grace period.
Can be executed as a daily cron job or Celery beat task.
Usage: python manage.py auto_lock_overdue
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from emi.models import CustomerEMIContract, EMIInstallment, InstallmentStatus, EMIContractStatus
from devices.models import Device, DeviceLockStatus, DeviceCommand, CommandType, CommandStatus, LockProvider
from devices.services.rocketpay_service import RocketPayService
from accounts.models import PlatformSettings, AuditLog, NotificationType
from accounts.views import send_system_notification


class Command(BaseCommand):
    help = "Checks all active contracts for overdue installments and automatically locks devices whose grace period has expired."

    def handle(self, *args, **options):
        settings = PlatformSettings.get_settings()
        if not settings.auto_lock_enabled:
            self.stdout.write(self.style.WARNING("Auto-lock is disabled in Platform Settings. Aborting."))
            return

        today = timezone.localdate()
        self.stdout.write(f"Running auto-lock check for date: {today}...")

        # 1. Update any upcoming/due installments whose due date has passed to OVERDUE
        marked_overdue = EMIInstallment.objects.filter(
            due_date__lt=today,
            status__in=[InstallmentStatus.UPCOMING, InstallmentStatus.DUE]
        ).update(status=InstallmentStatus.OVERDUE)
        self.stdout.write(f"Updated {marked_overdue} pending/due installments to OVERDUE status.")

        # 2. Find all active contracts that have overdue installments
        active_contracts = CustomerEMIContract.objects.filter(
            status=EMIContractStatus.ACTIVE
        ).prefetch_related('installments')

        locked_count = 0

        for contract in active_contracts:
            grace_period = contract.plan_template.grace_period_days if contract.plan_template else settings.default_grace_period_days
            cutoff_date = today - timedelta(days=grace_period)

            # Check if there is an overdue installment past the grace period
            past_grace_installments = contract.installments.filter(
                status=InstallmentStatus.OVERDUE,
                due_date__lt=cutoff_date
            )

            if past_grace_installments.exists():
                contract.status = EMIContractStatus.LOCKED
                contract.save(update_fields=['status'])

                # Find associated device
                device = Device.objects.filter(emi_contract=contract).first() or Device.objects.filter(imei=contract.device_imei).first()
                if device and device.status != DeviceLockStatus.LOCKED:
                    device.status = DeviceLockStatus.LOCKED
                    device.save(update_fields=['status'])

                    # Queue local device command
                    DeviceCommand.objects.create(
                        device=device,
                        command_type=CommandType.LOCK,
                        status=CommandStatus.PENDING,
                        parameters={"reason": f"Auto-lock: EMI overdue past {grace_period} days grace period"}
                    )

                    # If RocketPay DLC controlled, invoke RocketPay OEM lock
                    if device.lock_provider == LockProvider.ROCKETPAY:
                        RocketPayService.lock_device(
                            device=device,
                            reason=f"EMI installment overdue past {grace_period} days grace period"
                        )

                    # Send in-app notification to retailer and customer
                    send_system_notification(
                        recipient=contract.retailer,
                        title="Device Auto-Locked",
                        message=f"Device {device.imei} ({contract.customer.username}) was auto-locked due to overdue payment.",
                        notification_type=NotificationType.DEVICE_LOCK,
                        reference_id=device.id
                    )
                    send_system_notification(
                        recipient=contract.customer,
                        title="Device Locked",
                        message=f"Your device ({device.imei}) has been locked because payment is past the grace period.",
                        notification_type=NotificationType.DEVICE_LOCK,
                        reference_id=device.id
                    )

                    AuditLog.objects.create(
                        action="AUTO_LOCK_DEVICE",
                        details={
                            "contract_id": contract.id,
                            "device_imei": device.imei,
                            "grace_period_days": grace_period,
                            "reason": "Overdue past grace period"
                        }
                    )
                    locked_count += 1

        self.stdout.write(self.style.SUCCESS(f"Auto-lock completed. Locked {locked_count} devices."))
