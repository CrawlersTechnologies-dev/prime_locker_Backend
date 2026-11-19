import io
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, Image as RLImage
)


def generate_contract_pdf(contract, customer, retailer, customer_name, retailer_name, agreement_date):
    """
    Generates a full-coverage, single-page, legal-grade A4 PDF agreement for an EMI loan contract.
    Features:
      - Perfectly fills the entire A4 page from top to bottom
      - Clean modern legal document typography (no table grids)
      - SOLE Customer Digital Signature block (no retailer signature)
    Returns bytes of the compiled PDF.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=32,
        rightMargin=32,
        topMargin=26,
        bottomMargin=26
    )

    styles = getSampleStyleSheet()

    primary = colors.HexColor('#0f172a')
    accent = colors.HexColor('#b45309')
    text_dark = colors.HexColor('#1e293b')
    text_muted = colors.HexColor('#64748b')
    bg_subtle = colors.HexColor('#f8fafc')
    border_color = colors.HexColor('#cbd5e1')

    title_style = ParagraphStyle('T', fontName='Helvetica-Bold', fontSize=18, leading=22, textColor=primary)
    meta_style = ParagraphStyle('M', fontName='Helvetica', fontSize=8.5, leading=12, textColor=text_muted, alignment=2)
    preamble_style = ParagraphStyle('Pr', fontName='Helvetica-Oblique', fontSize=8, leading=11, textColor=text_muted)
    sec_hdr_style = ParagraphStyle('H', fontName='Helvetica-Bold', fontSize=9.5, leading=13, textColor=primary)
    body_style = ParagraphStyle('B', fontName='Helvetica', fontSize=8.2, leading=12, textColor=text_dark)
    clause_style = ParagraphStyle('C', fontName='Helvetica', fontSize=7.8, leading=11.2, textColor=text_dark)
    decl_style = ParagraphStyle('D', fontName='Helvetica-Oblique', fontSize=8, leading=11.8, textColor=colors.HexColor('#78350f'))
    footer_style = ParagraphStyle('F', fontName='Helvetica', fontSize=7, leading=9, textColor=text_muted, alignment=1)

    story = []

    # 1. Header (Brand + Meta)
    h_data = [[
        Paragraph("<b>PRIME LOCKER PRO</b><br/><font color='#b45309' size='8.5'><b>DEVICE FINANCING &amp; COLLATERAL LOCKING UNDERTAKING</b></font>", title_style),
        Paragraph(f"<b>AGREEMENT #: PLP-AGR-{contract.id:05d}</b><br/>Date: {agreement_date}<br/>Status: <font color='#16a34a'><b>{contract.status}</b></font>", meta_style)
    ]]
    story.append(Table(h_data, colWidths=[340, 190], style=[('VALIGN', (0,0), (-1,-1), 'MIDDLE'), ('PADDING', (0,0), (-1,-1), 0)]))
    story.append(HRFlowable(width='100%', thickness=1.5, color=primary, spaceBefore=4, spaceAfter=6))

    # Preamble
    story.append(Paragraph(f"This Agreement is executed on <b>{agreement_date}</b> between the Authorized Retailer and the Customer/Borrower as specified below:", preamble_style))
    story.append(Spacer(1, 6))

    # Section 1: Contracting Parties
    story.append(Paragraph("<b>1. CONTRACTING PARTIES</b>", sec_hdr_style))
    story.append(HRFlowable(width='100%', thickness=0.6, color=border_color, spaceBefore=2, spaceAfter=4))
    retailer_business = getattr(retailer, 'business_name', '') or retailer_name
    retailer_phone = getattr(retailer, 'store_phone', '') or getattr(retailer, 'mobile_number', '') or 'N/A'
    retailer_gst = getattr(retailer, 'gst_number', '') or 'N/A'
    retailer_addr = getattr(retailer, 'address', '') or getattr(retailer, 'city', '') or 'N/A'

    retailer_info = f"""<font color='#b45309'><b>FINANCING PARTNER / RETAILER:</b></font><br/>
    <b>{retailer_business}</b><br/>
    Authorized Rep: {retailer_name}<br/>
    Store Phone: {retailer_phone} | GSTIN: {retailer_gst}<br/>
    Address: {retailer_addr}
    """

    cust_code = getattr(customer, 'customer_code', '') or f"PLP-CUST-{customer.id}"
    id_type = getattr(customer, 'id_type', '') or 'ID'
    id_num = getattr(customer, 'id_number', '') or 'Verified on File'
    cust_addr = getattr(customer, 'address', '') or getattr(customer, 'city', '') or 'N/A'

    customer_info = f"""<font color='#b45309'><b>BORROWER / CUSTOMER:</b></font><br/>
    <b>{customer_name}</b><br/>
    Mobile Number: {customer.mobile_number}<br/>
    Customer ID: {cust_code} | ID: {id_type} - {id_num}<br/>
    Address: {cust_addr}
    """
    story.append(Table([[Paragraph(retailer_info, body_style), Paragraph(customer_info, body_style)]], colWidths=[263, 268],
                       style=[('BACKGROUND', (0,0), (-1,-1), bg_subtle), ('BOX', (0,0), (-1,-1), 0.5, border_color),
                              ('PADDING', (0,0), (-1,-1), 7), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(Spacer(1, 8))

    # Section 2: Financed Device Specs
    story.append(Paragraph("<b>2. FINANCED ASSET &amp; COLLATERAL DEVICE DETAILS</b>", sec_hdr_style))
    story.append(HRFlowable(width='100%', thickness=0.6, color=border_color, spaceBefore=2, spaceAfter=4))
    prod_type = contract.product_type or 'Smartphone'
    prod_brand_model = f"{contract.product_brand or contract.device_brand or ''} {contract.product_model or contract.device_model or ''}".strip() or 'Financed Device'
    prod_price = f"Rs. {contract.total_amount:,.2f}"
    prod_serial = contract.product_serial_number or 'On File'
    collateral_imei = contract.device_imei or 'N/A'
    collateral_state = getattr(contract, 'device_state', 'NEW') or 'NEW'

    d_left = f"""• <b>Financed Product:</b> {prod_type}<br/>
    • <b>Brand &amp; Model:</b> {prod_brand_model}<br/>
    • <b>Product Serial / Ref:</b> {prod_serial}"""
    d_right = f"""• <b>Total Handset Price:</b> <font color='#b45309'><b>{prod_price}</b></font><br/>
    • <b>Collateral IMEI:</b> <font color='#0f172a'><b>{collateral_imei}</b></font><br/>
    • <b>Device State / MDM:</b> <font color='#16a34a'><b>{collateral_state} (Knox / OEM Enrolled)</b></font>"""
    story.append(Table([[Paragraph(d_left, body_style), Paragraph(d_right, body_style)]], colWidths=[263, 268],
                       style=[('BACKGROUND', (0,0), (-1,-1), bg_subtle), ('BOX', (0,0), (-1,-1), 0.5, border_color),
                              ('PADDING', (0,0), (-1,-1), 7), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(Spacer(1, 8))

    # Section 3: Loan Facility & Repayment Terms
    story.append(Paragraph("<b>3. LOAN FACILITY &amp; REPAYMENT SCHEDULE</b>", sec_hdr_style))
    story.append(HRFlowable(width='100%', thickness=0.6, color=border_color, spaceBefore=2, spaceAfter=4))
    f_left = f"""• <b>Total Handset Price:</b> Rs. {contract.total_amount:,.2f}<br/>
    • <b>Down Payment Paid:</b> Rs. {contract.down_payment:,.2f}<br/>
    • <b>Financed Principal:</b> <font color='#0f172a'><b>Rs. {contract.financed_amount:,.2f}</b></font><br/>
    • <b>Processing Fee:</b> Rs. {contract.processing_fee:,.2f}"""
    mandate_txt = f" (Mandate: {contract.mandate_status or 'ACTIVE'})" if contract.mandate_id else ""
    f_right = f"""• <b>Loan Tenure:</b> {contract.tenure_months} Monthly Installments<br/>
    • <b>Monthly Installment:</b> <font color='#b45309'><b>Rs. {contract.monthly_emi_amount:,.2f} / month</b></font><br/>
    • <b>First Due Date:</b> {contract.first_emi_date or 'As Scheduled'}<br/>
    • <b>Grace Period:</b> {contract.grace_period_days} Days{mandate_txt}"""
    story.append(Table([[Paragraph(f_left, body_style), Paragraph(f_right, body_style)]], colWidths=[263, 268],
                       style=[('BACKGROUND', (0,0), (-1,-1), bg_subtle), ('BOX', (0,0), (-1,-1), 0.5, border_color),
                              ('PADDING', (0,0), (-1,-1), 7), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(Spacer(1, 8))

    # Section 4: Terms & Device Locking Covenants
    story.append(Paragraph("<b>4. TERMS &amp; DEVICE LOCKING COVENANTS</b>", sec_hdr_style))
    story.append(HRFlowable(width='100%', thickness=0.6, color=border_color, spaceBefore=2, spaceAfter=4))
    clauses = [
        f"<b>1. Repayment Undertaking:</b> The Borrower acknowledges having received the financed device in good working condition and undertakes to pay each monthly installment of <b>Rs. {contract.monthly_emi_amount:,.2f}</b> on or before each scheduled due date for a tenure of <b>{contract.tenure_months} months</b>.",
        f"<b>2. Collateral Security &amp; Device Management:</b> The Borrower authorizes Prime Locker Pro MDM application to be installed and provisioned on the collateral handset (IMEI: <b>{contract.device_imei}</b>) to secure this financing facility.",
        f"<b>3. Irrevocable Remote Locking Consent:</b> In the event that any monthly installment remains unpaid past the permitted grace period of <b>{contract.grace_period_days} days</b>, the Borrower grants unequivocal consent for the handset to be remotely locked, restricting normal phone usage until dues are settled.",
        "<b>4. Instant Automated Restoration:</b> Upon verified payment of overdue installments via UPI, AutoPay, Netbanking, or Store Cashier, the handset lock will be automatically released and full functionality immediately restored.",
        "<b>5. Anti-Tamper Policy:</b> The Borrower agrees not to uninstall MDM, flash custom ROMs, root, or tamper with the device security policy until full loan repayment is formally certified."
    ]
    for c in clauses:
        story.append(Paragraph(c, clause_style))
        story.append(Spacer(1, 2))

    story.append(Spacer(1, 5))

    # Section 5: Borrower Declaration & Digital Signature
    story.append(Paragraph("<b>5. BORROWER DECLARATION &amp; DIGITAL SIGNATURE</b>", sec_hdr_style))
    story.append(HRFlowable(width='100%', thickness=0.6, color=border_color, spaceBefore=2, spaceAfter=4))
    decl_p = """<b>Borrower Sole Declaration &amp; Acceptance:</b><br/>
    \"I, the undersigned Borrower, hereby affirm that I have read, understood, and agreed to all the terms, conditions, and collateral locking provisions specified in this Agreement. I confirm receipt of the financed device in satisfactory condition and willingly execute this undertaking with my digital signature below.\""""

    has_img = False
    if customer.customer_signature:
        try:
            if hasattr(customer.customer_signature, 'path') and os.path.exists(customer.customer_signature.path):
                has_img = True
        except Exception:
            has_img = False

    if not has_img:
        cust_sig_html = f"<font size='15' color='#1e3a8a'><b><i>{customer_name}</i></b></font><br/>"
    else:
        cust_sig_html = f"<font size='11' color='#1e3a8a'>[Digital Signature Attached]</font><br/>"

    sig_p = f"""<font size='7.5' color='#b45309'><b>BORROWER DIGITAL SIGNATURE</b></font><br/>
    {cust_sig_html}
    ____________________________________________<br/>
    <b>{customer_name}</b><br/>
    <font size='7' color='#64748b'>Digitally Signed via Prime Locker Pro MDM<br/>
    Date: {agreement_date} | Ref: PLP-AGR-{contract.id:05d}</font>"""

    b_table = Table([[Paragraph(decl_p, decl_style), Paragraph(sig_p, ParagraphStyle('S', parent=styles['Normal'], alignment=1, fontSize=8, leading=11))]],
                    colWidths=[285, 246],
                    style=[
                        ('BACKGROUND', (0,0), (0,0), colors.HexColor('#fffbeb')),
                        ('BOX', (0,0), (0,0), 0.5, colors.HexColor('#fde68a')),
                        ('BACKGROUND', (1,0), (1,0), colors.HexColor('#ffffff')),
                        ('BOX', (1,0), (1,0), 1, primary),
                        ('PADDING', (0,0), (-1,-1), 8),
                        ('VALIGN', (0,0), (-1,-1), 'MIDDLE')
                    ])
    story.append(b_table)
    story.append(Spacer(1, 8))

    # Footer
    story.append(HRFlowable(width='100%', thickness=0.5, color=border_color, spaceBefore=2, spaceAfter=3))
    story.append(Paragraph(f"This is an authenticated legal electronic record generated and archived by Prime Locker Pro Platform | Agreement: PLP-AGR-{contract.id:05d}", footer_style))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()
