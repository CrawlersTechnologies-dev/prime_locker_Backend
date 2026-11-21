"""
Prime Locker - EMI Commission & Retailer Wallet Service (98% / 2% Model).

Core Financial Rules:
  - Total EMI Split:
      - 2% Platform Fee goes to Prime-Locker Admin.
      - 98% Net Collection goes to the Retailer.
  - Scenario A (Online Payment):
      - 100% arrives in Prime-Locker master gateway account.
      - 98% is automatically credited to the Retailer's In-App Digital Wallet.
      - 2% is retained by Prime-Locker Admin as software platform revenue.
  - Scenario B (Offline Cash Payment):
      - Customer hands 100% physical cash to the retailer at the shop.
      - 2% Admin Fee is deducted from the Retailer's In-App Wallet balance.
  - Key Purchases & Payouts:
      - Retailers can withdraw wallet balances to their registered Bank / UPI.
      - Retailers can purchase device keys directly using their wallet balance.
"""
from decimal import Decimal
import random
import string
import uuid
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from accounts.models import PlatformSettings, AuditLog
from emi.models import (
    EMIInstallment,
    InstallmentStatus,
    RetailerWallet,
    WalletTransaction,
    WalletTransactionType,
    PayoutRequest,
    PayoutStatus,
    PayoutMethod
)


class WalletService:

    @classmethod
    def get_commission_rates(cls):
        """Returns the admin commission rate (default 2.00%) and retailer rate (default 98.00%)."""
        settings = PlatformSettings.get_settings()
        admin_rate = getattr(settings, 'emi_admin_fee_percentage', Decimal('2.00')) or Decimal('2.00')
        admin_rate = Decimal(str(admin_rate))
        retailer_rate = Decimal('100.00') - admin_rate
        return admin_rate, retailer_rate

    @classmethod
    def calculate_emi_split(cls, amount):
        """
        Splits gross EMI installment amount into Admin Fee (2%) and Retailer Share (98%).
        """
        amt = Decimal(str(amount))
        admin_rate, retailer_rate = cls.get_commission_rates()
        admin_fee = (amt * admin_rate / Decimal('100.00')).quantize(Decimal('0.01'))
        retailer_share = (amt - admin_fee).quantize(Decimal('0.01'))
        return admin_fee, retailer_share

    @classmethod
    def process_online_emi_split(cls, installment, gross_amount, payment_id):
        """
        Scenario A: Online Payment via Cashfree / UPI / Payment Link.
          - 100% gross arrives in Prime-Locker master bank account.
          - 98% net share is credited to Retailer Digital Wallet.
          - 2% admin fee is retained as platform revenue.
        """
        admin_fee, retailer_share = cls.calculate_emi_split(gross_amount)
        gross_amt = Decimal(str(gross_amount))

        with transaction.atomic():
            # Update installment with financial split audit
            installment.admin_commission = admin_fee
            installment.retailer_share = retailer_share
            installment.save(update_fields=['admin_commission', 'retailer_share'])

            retailer = installment.contract.retailer
            wallet = RetailerWallet.get_or_create_wallet(retailer)

            # Credit retailer 98%
            wallet.balance = Decimal(str(wallet.balance)) + retailer_share
            wallet.total_collected = Decimal(str(wallet.total_collected)) + gross_amt
            wallet.total_admin_fee_paid = Decimal(str(wallet.total_admin_fee_paid)) + admin_fee
            wallet.save(update_fields=['balance', 'total_collected', 'total_admin_fee_paid', 'updated_at'])

            customer_name = installment.contract.customer.get_full_name() or installment.contract.customer.username
            desc = (
                f"Online EMI Paid - Customer: {customer_name} ({installment.contract.device_imei}) | "
                f"Gross EMI: ₹{gross_amt:,.2f} | Admin Fee (2%): -₹{admin_fee:,.2f} | Net Credit: +₹{retailer_share:,.2f}"
            )

            WalletTransaction.objects.create(
                wallet=wallet,
                transaction_type=WalletTransactionType.EMI_ONLINE_CREDIT,
                amount=retailer_share,
                gross_amount=gross_amt,
                admin_fee=admin_fee,
                balance_after=wallet.balance,
                installment=installment,
                reference_id=str(payment_id),
                description=desc
            )

        return admin_fee, retailer_share

    @classmethod
    def process_offline_emi_split(cls, installment, gross_amount, user=None):
        """
        Scenario B: Offline Cash Payment at Retailer's Shop.
          - Customer hands 100% physical cash to Retailer.
          - 2% Admin Platform Fee is deducted from Retailer's In-App Wallet balance.
        """
        admin_fee, retailer_share = cls.calculate_emi_split(gross_amount)
        gross_amt = Decimal(str(gross_amount))

        with transaction.atomic():
            # Update installment with financial split audit
            installment.admin_commission = admin_fee
            installment.retailer_share = retailer_share
            installment.save(update_fields=['admin_commission', 'retailer_share'])

            retailer = installment.contract.retailer
            wallet = RetailerWallet.get_or_create_wallet(retailer)

            # Deduct 2% admin fee
            wallet.balance = Decimal(str(wallet.balance)) - admin_fee
            wallet.total_collected = Decimal(str(wallet.total_collected)) + gross_amt
            wallet.total_admin_fee_paid = Decimal(str(wallet.total_admin_fee_paid)) + admin_fee
            wallet.save(update_fields=['balance', 'total_collected', 'total_admin_fee_paid', 'updated_at'])

            customer_name = installment.contract.customer.get_full_name() or installment.contract.customer.username
            ref_id = installment.transaction_ref or f"CASH-{installment.id}"
            desc = (
                f"Cash EMI Collected at Shop - Customer: {customer_name} ({installment.contract.device_imei}) | "
                f"Gross EMI: ₹{gross_amt:,.2f} (Retailer holds cash) | Admin Platform Fee (2%): -₹{admin_fee:,.2f}"
            )

            WalletTransaction.objects.create(
                wallet=wallet,
                transaction_type=WalletTransactionType.EMI_OFFLINE_FEE_DEBIT,
                amount=-admin_fee,
                gross_amount=gross_amt,
                admin_fee=admin_fee,
                balance_after=wallet.balance,
                installment=installment,
                reference_id=ref_id,
                description=desc
            )

        return admin_fee, retailer_share

    @classmethod
    def request_payout(cls, retailer, amount, payout_method=PayoutMethod.BANK_TRANSFER,
                       bank_data=None):
        """
        Retailer requests withdrawal to their registered Bank Account or UPI ID.
        Deducts balance immediately from wallet to prevent double-spending.
        """
        amt = Decimal(str(amount))
        if amt <= Decimal('0.00'):
            raise ValidationError("Withdrawal amount must be greater than zero.")

        wallet = RetailerWallet.get_or_create_wallet(retailer)
        if wallet.balance < amt:
            raise ValidationError(
                f"Insufficient wallet balance. You requested ₹{amt:,.2f}, but available balance is ₹{wallet.balance:,.2f}."
            )

        # Pull bank details from RetailerBankAccount if available
        bank_name = ""
        acc_holder = retailer.get_full_name() or retailer.username
        acc_num = ""
        ifsc = ""
        upi = ""

        bank_acc = getattr(retailer, 'bank_account', None)
        if bank_acc:
            bank_name = bank_acc.bank_name
            acc_holder = bank_acc.account_holder_name or acc_holder
            acc_num = bank_acc.account_number
            ifsc = bank_acc.ifsc_code
            upi = bank_acc.upi_id or ""

        # Allow explicit override from request
        if bank_data and isinstance(bank_data, dict):
            bank_name = bank_data.get('bank_name') or bank_name
            acc_holder = bank_data.get('account_holder_name') or acc_holder
            acc_num = bank_data.get('account_number') or acc_num
            ifsc = bank_data.get('ifsc_code') or ifsc
            upi = bank_data.get('upi_id') or upi

        if payout_method == PayoutMethod.UPI and not upi:
            raise ValidationError("A valid UPI ID is required for UPI withdrawals.")
        if payout_method == PayoutMethod.BANK_TRANSFER and (not acc_num or not ifsc):
            raise ValidationError("Account number and IFSC code are required for Bank Transfer withdrawals.")

        with transaction.atomic():
            # Deduct wallet balance
            wallet.balance = Decimal(str(wallet.balance)) - amt
            wallet.save(update_fields=['balance', 'updated_at'])

            payout = PayoutRequest.objects.create(
                retailer=retailer,
                amount=amt,
                payout_method=payout_method,
                account_holder_name=acc_holder,
                bank_name=bank_name,
                account_number=acc_num,
                ifsc_code=ifsc,
                upi_id=upi,
                status=PayoutStatus.PENDING
            )

            dest_str = f"UPI {upi}" if payout_method == PayoutMethod.UPI else f"{bank_name} (A/C: {acc_num[-4:] if len(acc_num) >= 4 else acc_num})"
            desc = f"Payout Request #{payout.id} - Withdrawn to {dest_str} (Pending Admin Approval)"

            WalletTransaction.objects.create(
                wallet=wallet,
                transaction_type=WalletTransactionType.PAYOUT_WITHDRAWAL,
                amount=-amt,
                gross_amount=amt,
                admin_fee=Decimal('0.00'),
                balance_after=wallet.balance,
                reference_id=f"PAYOUT-{payout.id}",
                description=desc
            )

        return payout

    @classmethod
    def approve_payout(cls, payout, admin_user, utr_number="", notes=""):
        """
        Super Admin approves / processes a withdrawal request with bank UTR number.
        """
        if payout.status != PayoutStatus.PENDING:
            raise ValidationError(f"Payout #{payout.id} is already in {payout.status} state.")

        with transaction.atomic():
            payout.status = PayoutStatus.PROCESSED
            payout.utr_number = utr_number or f"UTR-{payout.id}-{timezone.now().strftime('%Y%m%d%H%M')}"
            payout.admin_notes = notes
            payout.processed_by = admin_user
            payout.processed_at = timezone.now()
            payout.save(update_fields=['status', 'utr_number', 'admin_notes', 'processed_by', 'processed_at', 'updated_at'])

            wallet = RetailerWallet.get_or_create_wallet(payout.retailer)
            wallet.total_withdrawn = Decimal(str(wallet.total_withdrawn)) + Decimal(str(payout.amount))
            wallet.save(update_fields=['total_withdrawn', 'updated_at'])

            # Send in-app notification to retailer
            try:
                from accounts.views import send_system_notification
                from accounts.models import NotificationType
                send_system_notification(
                    recipient=payout.retailer,
                    title="Payout Transferred Successfully",
                    message=f"₹{payout.amount:,.2f} has been transferred to your account (UTR: {payout.utr_number}).",
                    notification_type=NotificationType.SYSTEM_ALERT,
                    reference_id=payout.id,
                    sender=admin_user
                )
            except Exception:
                pass

        return payout

    @classmethod
    def reject_payout(cls, payout, admin_user, reason=""):
        """
        Super Admin rejects a withdrawal request and immediately refunds funds to the wallet.
        """
        if payout.status != PayoutStatus.PENDING:
            raise ValidationError(f"Payout #{payout.id} is already in {payout.status} state.")

        with transaction.atomic():
            rejection_text = reason or "Withdrawal rejected by administrator."
            payout.status = PayoutStatus.REJECTED
            payout.rejection_reason = rejection_text
            payout.processed_by = admin_user
            payout.processed_at = timezone.now()
            payout.save(update_fields=['status', 'rejection_reason', 'processed_by', 'processed_at', 'updated_at'])

            wallet = RetailerWallet.get_or_create_wallet(payout.retailer)
            wallet.balance = Decimal(str(wallet.balance)) + Decimal(str(payout.amount))
            wallet.save(update_fields=['balance', 'updated_at'])

            desc = f"Payout #{payout.id} Rejected ({rejection_text}) - ₹{payout.amount:,.2f} refunded back to wallet."

            WalletTransaction.objects.create(
                wallet=wallet,
                transaction_type=WalletTransactionType.PAYOUT_REVERSAL,
                amount=payout.amount,
                gross_amount=payout.amount,
                admin_fee=Decimal('0.00'),
                balance_after=wallet.balance,
                reference_id=f"REVERSAL-{payout.id}",
                description=desc
            )

            # Send in-app notification to retailer
            try:
                from accounts.views import send_system_notification
                from accounts.models import NotificationType
                send_system_notification(
                    recipient=payout.retailer,
                    title="Payout Request Rejected",
                    message=f"Payout #{payout.id} for ₹{payout.amount:,.2f} was rejected: {rejection_text}. Funds have been refunded to your wallet.",
                    notification_type=NotificationType.SYSTEM_ALERT,
                    reference_id=payout.id,
                    sender=admin_user
                )
            except Exception:
                pass

        return payout

    @classmethod
    def buy_keys_with_wallet(cls, retailer, device_type="MOBILE", quantity=1, os_type="ANDROID"):
        """
        Allows retailer to purchase device license keys directly using their in-app wallet balance.
        Generates keys instantly into DeviceKey and assigns current_owner=retailer.
        """
        qty = int(quantity)
        if qty <= 0:
            raise ValidationError("Key quantity must be at least 1.")

        settings = PlatformSettings.get_settings()
        dt_upper = str(device_type).upper()

        if dt_upper == 'TV':
            unit_price = getattr(settings, 'key_price_tv', Decimal('700.00')) or Decimal('700.00')
            prefix = "TV-"
        elif dt_upper == 'IOS':
            unit_price = getattr(settings, 'key_price_mobile', Decimal('500.00')) or Decimal('500.00')
            prefix = "IOS-"
        elif dt_upper == 'DLC':
            unit_price = getattr(settings, 'key_price_mobile', Decimal('500.00')) or Decimal('500.00')
            prefix = "DLC-"
        else:
            unit_price = getattr(settings, 'key_price_mobile', Decimal('500.00')) or Decimal('500.00')
            prefix = "AND-" if str(os_type).upper() == 'ANDROID' else "MOB-"

        unit_price = Decimal(str(unit_price))
        total_cost = (unit_price * qty).quantize(Decimal('0.01'))

        wallet = RetailerWallet.get_or_create_wallet(retailer)
        if Decimal(str(wallet.balance)) < total_cost:
            raise ValidationError(
                f"Insufficient wallet balance. Total cost for {qty} {dt_upper} keys is ₹{total_cost:,.2f}, but your available balance is ₹{Decimal(str(wallet.balance)):,.2f}."
            )

        from keys.models import DeviceKey, KeyStatus, KeyTransaction, KeyAction

        with transaction.atomic():
            # Deduct wallet
            wallet.balance = Decimal(str(wallet.balance)) - total_cost
            wallet.save(update_fields=['balance', 'updated_at'])

            batch_id = uuid.uuid4()
            created_keys = []

            for _ in range(qty):
                while True:
                    rand_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                    key_code = f"{prefix}{rand_str}"
                    if not DeviceKey.objects.filter(key_string=key_code).exists():
                        break

                key_obj = DeviceKey.objects.create(
                    key_string=key_code,
                    device_type=dt_upper,
                    status=KeyStatus.AVAILABLE,
                    current_owner=retailer,
                    batch_id=batch_id
                )
                created_keys.append(key_obj)

                KeyTransaction.objects.create(
                    batch_id=batch_id,
                    key=key_obj,
                    from_user=None,
                    to_user=retailer,
                    action=KeyAction.GENERATED,
                    notes=f"Purchased using In-App Wallet balance (₹{unit_price:,.2f} each)"
                )

            desc = f"Purchased {qty} {dt_upper} license keys using wallet balance (₹{unit_price:,.2f} per key)."

            WalletTransaction.objects.create(
                wallet=wallet,
                transaction_type=WalletTransactionType.KEY_PURCHASE,
                amount=-total_cost,
                gross_amount=total_cost,
                admin_fee=Decimal('0.00'),
                balance_after=wallet.balance,
                reference_id=str(batch_id),
                description=desc
            )

        return created_keys, total_cost, wallet.balance

    @classmethod
    def get_revenue_dashboard_stats(cls):
        """
        Calculates live platform revenue statistics (2% fee) across all retailers:
          - Daily, Monthly, Yearly, and All-Time 2% Revenue
          - Gross Collections & Split Breakdown (Online vs Offline)
          - Total Wallet Liabilities and Payout Summary
        """
        from django.db.models import Sum, Count, Q

        now = timezone.now()
        today = timezone.localdate()
        start_of_month = today.replace(day=1)
        start_of_year = today.replace(month=1, day=1)

        paid_qs = EMIInstallment.objects.filter(status=InstallmentStatus.PAID)

        # 2% Admin Revenue
        all_time_rev = paid_qs.aggregate(total=Sum('admin_commission'))['total'] or Decimal('0.00')
        today_rev = paid_qs.filter(collection_date__date=today).aggregate(total=Sum('admin_commission'))['total'] or Decimal('0.00')
        month_rev = paid_qs.filter(collection_date__date__gte=start_of_month).aggregate(total=Sum('admin_commission'))['total'] or Decimal('0.00')
        year_rev = paid_qs.filter(collection_date__date__gte=start_of_year).aggregate(total=Sum('admin_commission'))['total'] or Decimal('0.00')

        # Gross Volumes
        all_time_volume = paid_qs.aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')
        month_volume = paid_qs.filter(collection_date__date__gte=start_of_month).aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')
        today_volume = paid_qs.filter(collection_date__date=today).aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')

        # Split: Online vs Offline
        online_qs = paid_qs.filter(payment_mode='ONLINE_GATEWAY')
        offline_qs = paid_qs.exclude(payment_mode='ONLINE_GATEWAY')

        online_gross = online_qs.aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')
        online_admin = online_qs.aggregate(total=Sum('admin_commission'))['total'] or Decimal('0.00')
        online_retailer = online_qs.aggregate(total=Sum('retailer_share'))['total'] or Decimal('0.00')
        online_count = online_qs.count()

        offline_gross = offline_qs.aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')
        offline_admin = offline_qs.aggregate(total=Sum('admin_commission'))['total'] or Decimal('0.00')
        offline_count = offline_qs.count()

        # Retailer Wallet Liabilities
        wallet_aggs = RetailerWallet.objects.aggregate(
            total_balance=Sum('balance'),
            total_collected=Sum('total_collected'),
            total_withdrawn=Sum('total_withdrawn'),
            total_admin_fees=Sum('total_admin_fee_paid')
        )
        total_wallet_liability = wallet_aggs['total_balance'] or Decimal('0.00')
        total_withdrawn_all_time = wallet_aggs['total_withdrawn'] or Decimal('0.00')

        # Payout Requests Summary
        pending_payouts_qs = PayoutRequest.objects.filter(status=PayoutStatus.PENDING)
        pending_payouts_count = pending_payouts_qs.count()
        pending_payouts_amount = pending_payouts_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        return {
            "platform_commission_percentage": float(cls.get_commission_rates()[0]),
            "retailer_share_percentage": float(cls.get_commission_rates()[1]),
            "revenue": {
                "all_time_admin_revenue": float(all_time_rev),
                "today_admin_revenue": float(today_rev),
                "this_month_admin_revenue": float(month_rev),
                "this_year_admin_revenue": float(year_rev),
            },
            "volume": {
                "all_time_gross_emi_volume": float(all_time_volume),
                "this_month_gross_emi_volume": float(month_volume),
                "today_gross_emi_volume": float(today_volume),
                "total_paid_installments_count": paid_qs.count(),
            },
            "channel_breakdown": {
                "online": {
                    "count": online_count,
                    "gross_collected": float(online_gross),
                    "admin_revenue_2pct": float(online_admin),
                    "retailer_wallet_credited_98pct": float(online_retailer),
                },
                "offline_cash": {
                    "count": offline_count,
                    "gross_collected": float(offline_gross),
                    "admin_fee_deducted_2pct": float(offline_admin),
                    "retailer_cash_retained_98pct": float(offline_gross - offline_admin),
                }
            },
            "wallets_and_payouts": {
                "total_retailer_wallet_balances": float(total_wallet_liability),
                "total_funds_withdrawn_to_bank": float(total_withdrawn_all_time),
                "pending_payouts_count": pending_payouts_count,
                "pending_payouts_amount": float(pending_payouts_amount),
            }
        }
