from rest_framework.views import APIView
from rest_framework import permissions, status
from rest_framework.response import Response
from django.db.models import Sum, Count, Q, F
from django.utils import timezone
from datetime import datetime
from decimal import Decimal

from emi.models import (
    CustomerEMIContract,
    EMIInstallment,
    InstallmentStatus,
    EMIContractStatus,
    PaymentMode
)
from accounts.models import User, AuditLog
from .utils import generate_csv_response


def get_downstream_retailer_ids(user):
    """Returns list of retailer user IDs belonging to this user's downline"""
    if user.role == 'SUPER_ADMIN':
        return list(User.objects.filter(role='RETAILER').values_list('id', flat=True))
    elif user.role == 'RETAILER':
        return [user.id]
    else:
        def get_all_subordinates(parent_user):
            subs = list(parent_user.children.all())
            for s in subs:
                subs.extend(get_all_subordinates(s))
            return subs
        return [u.id for u in get_all_subordinates(user) if u.role == 'RETAILER']


class CollectionsSummaryReportView(APIView):
    """
    Report 1: Collections Summary (Amount due vs collected, by day, retailer, and payment mode).
    Supports ?export=csv for instant spreadsheet download.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        retailer_ids = get_downstream_retailer_ids(request.user)
        
        # Base Query
        installments = EMIInstallment.objects.filter(contract__retailer_id__in=retailer_ids)
        
        # Date filtering
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        if start_date:
            installments = installments.filter(due_date__gte=start_date)
        if end_date:
            installments = installments.filter(due_date__lte=end_date)
            
        # Retailer filter
        retailer_id = request.query_params.get('retailer_id')
        if retailer_id:
            installments = installments.filter(contract__retailer_id=retailer_id)
            
        # Payment mode filter
        payment_mode = request.query_params.get('payment_mode')
        if payment_mode:
            installments = installments.filter(payment_mode=payment_mode.upper())

        # Totals
        total_due = installments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        total_collected = installments.filter(status=InstallmentStatus.PAID).aggregate(
            total=Sum('paid_amount')
        )['total'] or Decimal('0.00')
        total_pending = total_due - total_collected

        # Breakdown by Payment Mode
        mode_breakdown = installments.filter(status=InstallmentStatus.PAID).values('payment_mode').annotate(
            total=Sum('paid_amount'),
            count=Count('id')
        ).order_by('-total')

        # Breakdown by Retailer
        retailer_breakdown = installments.values(
            'contract__retailer__id',
            'contract__retailer__business_name',
            'contract__retailer__first_name',
            'contract__retailer__last_name'
        ).annotate(
            amount_due=Sum('amount'),
            amount_collected=Sum('paid_amount', filter=Q(status=InstallmentStatus.PAID)),
            installments_count=Count('id')
        ).order_by('-amount_collected')

        retailer_list = []
        for r in retailer_breakdown:
            collected = r['amount_collected'] or Decimal('0.00')
            due = r['amount_due'] or Decimal('0.00')
            store_name = r['contract__retailer__business_name'] or f"{r['contract__retailer__first_name']} {r['contract__retailer__last_name']}".strip()
            retailer_list.append({
                "retailer_id": r['contract__retailer__id'],
                "store_name": store_name,
                "amount_due": due,
                "amount_collected": collected,
                "amount_pending": due - collected,
                "collection_rate_pct": round((collected / due * 100), 1) if due > 0 else 0.0
            })

        # Check for CSV Export
        if request.query_params.get('export') == 'csv':
            headers = ["Store Name", "Amount Due (₹)", "Amount Collected (₹)", "Amount Pending (₹)", "Collection Rate (%)"]
            rows = [
                [r['store_name'], str(r['amount_due']), str(r['amount_collected']), str(r['amount_pending']), f"{r['collection_rate_pct']}%"]
                for r in retailer_list
            ]
            return generate_csv_response("collections_summary.csv", headers, rows)

        return Response({
            "summary": {
                "total_amount_due": total_due,
                "total_amount_collected": total_collected,
                "total_amount_pending": total_pending,
                "overall_collection_rate_pct": round((total_collected / total_due * 100), 1) if total_due > 0 else 0.0
            },
            "by_payment_mode": list(mode_breakdown),
            "by_retailer": retailer_list
        })


class OverdueAgeingReportView(APIView):
    """
    Report 2: Overdue Ageing (Grouped by 1–7, 8–15, 16–30, and 30+ days).
    Supports ?export=csv for instant spreadsheet download.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        today = timezone.localdate()
        retailer_ids = get_downstream_retailer_ids(request.user)

        # Update overdue flags
        EMIInstallment.objects.filter(
            contract__retailer_id__in=retailer_ids,
            status__in=[InstallmentStatus.UPCOMING, InstallmentStatus.DUE],
            due_date__lt=today
        ).update(status=InstallmentStatus.OVERDUE)

        overdue_qs = EMIInstallment.objects.filter(
            contract__retailer_id__in=retailer_ids,
            status=InstallmentStatus.OVERDUE
        ).select_related('contract', 'contract__customer', 'contract__retailer')

        # Buckets initialization
        buckets = {
            "1-7_days": {"count": 0, "amount": Decimal('0.00'), "label": "1–7 days"},
            "8-15_days": {"count": 0, "amount": Decimal('0.00'), "label": "8–15 days"},
            "16-30_days": {"count": 0, "amount": Decimal('0.00'), "label": "16–30 days"},
            "30+_days": {"count": 0, "amount": Decimal('0.00'), "label": "30+ days"},
        }

        detailed_rows = []
        for inst in overdue_qs:
            days = (today - inst.due_date).days
            bucket_key = "30+_days"
            if 1 <= days <= 7:
                bucket_key = "1-7_days"
            elif 8 <= days <= 15:
                bucket_key = "8-15_days"
            elif 16 <= days <= 30:
                bucket_key = "16-30_days"

            buckets[bucket_key]["count"] += 1
            buckets[bucket_key]["amount"] += inst.amount

            c = inst.contract.customer
            r = inst.contract.retailer
            customer_name = f"{c.first_name} {c.last_name}".strip() or c.username
            store_name = r.business_name or f"{r.first_name} {r.last_name}".strip()

            detailed_rows.append({
                "installment_id": inst.id,
                "contract_id": inst.contract.id,
                "customer_name": customer_name,
                "customer_phone": c.mobile_number,
                "device_imei": inst.contract.device_imei,
                "device_name": inst.contract.device_name or "Mobile",
                "device_status": inst.contract.status,
                "store_name": store_name,
                "due_date": str(inst.due_date),
                "overdue_amount": inst.amount,
                "days_overdue": days,
                "bucket": buckets[bucket_key]["label"]
            })

        # Check for CSV Export
        if request.query_params.get('export') == 'csv':
            headers = ["Customer Name", "Phone", "Store", "Device Name", "IMEI", "Due Date", "Overdue Amount (₹)", "Days Overdue", "Ageing Bucket", "Status"]
            rows = [
                [r['customer_name'], r['customer_phone'], r['store_name'], r['device_name'], r['device_imei'], r['due_date'], str(r['overdue_amount']), r['days_overdue'], r['bucket'], r['device_status']]
                for r in detailed_rows
            ]
            return generate_csv_response("overdue_ageing_report.csv", headers, rows)

        total_overdue_amount = sum(b['amount'] for b in buckets.values())
        total_overdue_count = sum(b['count'] for b in buckets.values())

        return Response({
            "kpis": {
                "total_overdue_count": total_overdue_count,
                "total_overdue_amount": total_overdue_amount,
                "buckets": buckets
            },
            "overdue_customers": detailed_rows
        })


class RetailerPerformanceReportView(APIView):
    """
    Report 3: Retailer Performance (Devices sold, collection rate, overdue count per store).
    Supports ?export=csv for instant spreadsheet download.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        retailer_ids = get_downstream_retailer_ids(request.user)
        retailers = User.objects.filter(id__in=retailer_ids, role='RETAILER')

        today = timezone.localdate()
        performance_data = []

        for r in retailers:
            contracts = r.retailer_emi_contracts.all()
            devices_sold = contracts.count()
            total_financed = contracts.aggregate(total=Sum('financed_amount'))['total'] or Decimal('0.00')

            installments = EMIInstallment.objects.filter(contract__retailer=r)
            total_due = installments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            total_collected = installments.filter(status=InstallmentStatus.PAID).aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')
            overdue_count = installments.filter(status=InstallmentStatus.OVERDUE).count()

            collection_rate = round((total_collected / total_due * 100), 1) if total_due > 0 else 0.0

            store_name = r.business_name or f"{r.first_name} {r.last_name}".strip()
            owner_name = f"{r.first_name} {r.last_name}".strip() or r.username

            performance_data.append({
                "retailer_id": r.id,
                "store_name": store_name,
                "owner_name": owner_name,
                "mobile_number": r.mobile_number,
                "city": r.city,
                "state": r.state,
                "devices_sold": devices_sold,
                "total_financed_amount": total_financed,
                "total_collected": total_collected,
                "collection_rate_pct": collection_rate,
                "overdue_count": overdue_count
            })

        # Sort by devices sold descending
        performance_data.sort(key=lambda x: x['devices_sold'], reverse=True)

        if request.query_params.get('export') == 'csv':
            headers = ["Store Name", "Owner Name", "Phone", "City", "State", "Devices Sold", "Total Financed (₹)", "Total Collected (₹)", "Collection Rate (%)", "Overdue Count"]
            rows = [
                [p['store_name'], p['owner_name'], p['mobile_number'], p['city'], p['state'], p['devices_sold'], str(p['total_financed_amount']), str(p['total_collected']), f"{p['collection_rate_pct']}%", p['overdue_count']]
                for p in performance_data
            ]
            return generate_csv_response("retailer_performance_report.csv", headers, rows)

        return Response({
            "total_stores_evaluated": len(performance_data),
            "stores": performance_data
        })


class PaymentsByModeReportView(APIView):
    """
    Report 4: Payments by Mode (Online gateway, UPI, Cash, Card with breakdown and percentage share).
    Supports ?export=csv for instant spreadsheet download.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        retailer_ids = get_downstream_retailer_ids(request.user)
        paid_installments = EMIInstallment.objects.filter(
            contract__retailer_id__in=retailer_ids,
            status=InstallmentStatus.PAID
        )

        total_collected = paid_installments.aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')

        mode_stats = paid_installments.values('payment_mode').annotate(
            total_amount=Sum('paid_amount'),
            transaction_count=Count('id')
        ).order_by('-total_amount')

        results = []
        for m in mode_stats:
            mode = m['payment_mode'] or 'OTHER'
            amount = m['total_amount'] or Decimal('0.00')
            count = m['transaction_count']
            pct = round((amount / total_collected * 100), 1) if total_collected > 0 else 0.0

            results.append({
                "payment_mode": mode,
                "transaction_count": count,
                "total_amount": amount,
                "percentage_share": pct
            })

        if request.query_params.get('export') == 'csv':
            headers = ["Payment Mode", "Transaction Count", "Total Amount (₹)", "Share (%)"]
            rows = [
                [r['payment_mode'], r['transaction_count'], str(r['total_amount']), f"{r['percentage_share']}%"]
                for r in results
            ]
            return generate_csv_response("payments_by_mode.csv", headers, rows)

        return Response({
            "total_revenue_collected": total_collected,
            "modes": results
        })


class LocksUnlocksReportView(APIView):
    """
    Report 5: Locks & Unlocks Audit (Every lock, unlock action with device IMEI and who did it).
    Supports ?export=csv for instant spreadsheet download.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        retailer_ids = get_downstream_retailer_ids(request.user)
        contracts = CustomerEMIContract.objects.filter(retailer_id__in=retailer_ids)

        # Build list of devices with their lock status and history
        devices_list = []
        for c in contracts:
            customer_name = f"{c.customer.first_name} {c.customer.last_name}".strip() or c.customer.username
            store_name = c.retailer.business_name or f"{c.retailer.first_name} {c.retailer.last_name}".strip()

            devices_list.append({
                "contract_id": c.id,
                "device_imei": c.device_imei,
                "device_name": c.device_name or "Mobile",
                "customer_name": customer_name,
                "customer_phone": c.customer.mobile_number,
                "store_name": store_name,
                "current_status": c.status,
                "auto_lock_enabled": c.auto_lock_after_grace,
                "grace_period_days": c.grace_period_days,
                "last_updated": c.updated_at.strftime('%Y-%m-%d %H:%M:%S')
            })

        if request.query_params.get('export') == 'csv':
            headers = ["Device IMEI", "Device Name", "Customer Name", "Phone", "Store", "Current Status", "Auto Lock Enabled", "Grace Period (Days)", "Last Updated"]
            rows = [
                [d['device_imei'], d['device_name'], d['customer_name'], d['customer_phone'], d['store_name'], d['current_status'], d['auto_lock_enabled'], d['grace_period_days'], d['last_updated']]
                for d in devices_list
            ]
            return generate_csv_response("locks_and_unlocks_report.csv", headers, rows)

        locked_count = sum(1 for d in devices_list if d['current_status'] == EMIContractStatus.LOCKED)
        active_count = sum(1 for d in devices_list if d['current_status'] == EMIContractStatus.ACTIVE)

        return Response({
            "summary": {
                "total_monitored_devices": len(devices_list),
                "locked_devices_count": locked_count,
                "active_devices_count": active_count
            },
            "devices": devices_list
        })


class CustomerKYCReportView(APIView):
    """
    Report 6: Customer KYC Report (Customers with missing or unverified documents).
    Supports ?export=csv for instant spreadsheet download.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        retailer_ids = get_downstream_retailer_ids(request.user)
        
        # Customers onboarded under these retailers
        customers = User.objects.filter(role='CUSTOMER', parent_id__in=retailer_ids)

        complete_kyc_count = 0
        missing_kyc_count = 0
        customer_rows = []

        for c in customers:
            has_photo = bool(c.customer_photo)
            has_signature = bool(c.customer_signature)
            has_alt_phone = bool(c.alternate_mobile_number)

            is_complete = has_photo and has_signature and has_alt_phone
            if is_complete:
                complete_kyc_count += 1
                kyc_status = "COMPLETE"
            elif not has_photo and not has_signature:
                missing_kyc_count += 1
                kyc_status = "MISSING"
            else:
                missing_kyc_count += 1
                kyc_status = "PARTIAL"

            customer_name = f"{c.first_name} {c.last_name}".strip() or c.username
            parent_store = c.parent.business_name if c.parent else "N/A"

            customer_rows.append({
                "customer_id": c.id,
                "customer_name": customer_name,
                "primary_phone": c.mobile_number,
                "alternate_phone": c.alternate_mobile_number or "Missing",
                "customer_photo_uploaded": has_photo,
                "customer_signature_uploaded": has_signature,
                "registered_store": parent_store,
                "kyc_status": kyc_status,
                "date_joined": c.date_joined.strftime('%Y-%m-%d')
            })

        # Filter by status if requested (e.g. ?status=MISSING or ?status=PARTIAL)
        status_filter = request.query_params.get('status')
        if status_filter:
            customer_rows = [r for r in customer_rows if r['kyc_status'] == status_filter.upper()]

        if request.query_params.get('export') == 'csv':
            headers = ["Customer Name", "Primary Phone", "Alternate Phone", "Photo Uploaded", "Signature Uploaded", "Store", "KYC Status", "Registered Date"]
            rows = [
                [
                    r['customer_name'], 
                    r['primary_phone'], 
                    r['alternate_phone'], 
                    "Yes" if r['customer_photo_uploaded'] else "No",
                    "Yes" if r['customer_signature_uploaded'] else "No",
                    r['registered_store'], 
                    r['kyc_status'], 
                    r['date_joined']
                ]
                for r in customer_rows
            ]
            return generate_csv_response("customer_kyc_report.csv", headers, rows)

        return Response({
            "kpis": {
                "total_customers": len(customers),
                "complete_kyc_count": complete_kyc_count,
                "missing_or_partial_kyc_count": missing_kyc_count
            },
            "customers": customer_rows
        })
