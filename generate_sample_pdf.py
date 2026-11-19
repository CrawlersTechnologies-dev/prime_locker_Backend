import os
import pypdf
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable

def generate_sample_pdf(output_path="sample_agreement.pdf"):
    # A4: 595.27 x 841.89 pt. 26pt top/bottom margins => 531pt x 789.89pt full coverage.
    doc = SimpleDocTemplate(
        output_path,
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
        Paragraph("<b>AGREEMENT #: PLP-AGR-01042</b><br/>Date: 19 Nov 2026<br/>Status: <font color='#16a34a'><b>ACTIVE</b></font>", meta_style)
    ]]
    story.append(Table(h_data, colWidths=[340, 190], style=[('VALIGN', (0,0), (-1,-1), 'MIDDLE'), ('PADDING', (0,0), (-1,-1), 0)]))
    story.append(HRFlowable(width='100%', thickness=1.5, color=primary, spaceBefore=4, spaceAfter=6))

    # Preamble
    story.append(Paragraph("This Agreement is executed on <b>19 Nov 2026</b> between the Authorized Retailer and the Customer/Borrower as specified below:", preamble_style))
    story.append(Spacer(1, 6))

    # Section 1: Contracting Parties
    story.append(Paragraph("<b>1. CONTRACTING PARTIES</b>", sec_hdr_style))
    story.append(HRFlowable(width='100%', thickness=0.6, color=border_color, spaceBefore=2, spaceAfter=4))
    r_info = """<font color='#b45309'><b>FINANCING PARTNER / RETAILER:</b></font><br/>
    <b>Prime Mobile &amp; Electronics</b><br/>
    Authorized Rep: Suresh Patel<br/>
    Store Phone: +91 98765 43210 | GSTIN: 29ABCDE1234F1Z5<br/>
    Address: Shop 14, MG Road Market, Bangalore
    """
    c_info = """<font color='#b45309'><b>BORROWER / CUSTOMER:</b></font><br/>
    <b>Ramesh Kumar</b><br/>
    Mobile Number: +91 88675 38192<br/>
    Customer ID: PLP-CUST-8867 | ID: Aadhaar - XXXX-4921<br/>
    Address: Flat 204, Green View Apartments, Indiranagar
    """
    story.append(Table([[Paragraph(r_info, body_style), Paragraph(c_info, body_style)]], colWidths=[263, 268],
                       style=[('BACKGROUND', (0,0), (-1,-1), bg_subtle), ('BOX', (0,0), (-1,-1), 0.5, border_color),
                              ('PADDING', (0,0), (-1,-1), 7), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(Spacer(1, 8))

    # Section 2: Financed Device Specs
    story.append(Paragraph("<b>2. FINANCED ASSET &amp; COLLATERAL DEVICE DETAILS</b>", sec_hdr_style))
    story.append(HRFlowable(width='100%', thickness=0.6, color=border_color, spaceBefore=2, spaceAfter=4))
    d_left = """• <b>Financed Product:</b> Smartphone<br/>
    • <b>Brand &amp; Model:</b> Samsung Galaxy A15 5G (8GB/128GB)<br/>
    • <b>Product Serial / Ref:</b> SM-A156B-88392"""
    d_right = """• <b>Total Handset Price:</b> <font color='#b45309'><b>Rs. 18,500.00</b></font><br/>
    • <b>Collateral IMEI:</b> <font color='#0f172a'><b>866671060012345</b></font><br/>
    • <b>Device State / MDM:</b> <font color='#16a34a'><b>NEW (Knox / OEM Enrolled)</b></font>"""
    story.append(Table([[Paragraph(d_left, body_style), Paragraph(d_right, body_style)]], colWidths=[263, 268],
                       style=[('BACKGROUND', (0,0), (-1,-1), bg_subtle), ('BOX', (0,0), (-1,-1), 0.5, border_color),
                              ('PADDING', (0,0), (-1,-1), 7), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(Spacer(1, 8))

    # Section 3: Loan Facility & Repayment Terms
    story.append(Paragraph("<b>3. LOAN FACILITY &amp; REPAYMENT SCHEDULE</b>", sec_hdr_style))
    story.append(HRFlowable(width='100%', thickness=0.6, color=border_color, spaceBefore=2, spaceAfter=4))
    f_left = """• <b>Total Handset Price:</b> Rs. 18,500.00<br/>
    • <b>Down Payment Paid:</b> Rs. 3,500.00<br/>
    • <b>Financed Principal:</b> <font color='#0f172a'><b>Rs. 15,000.00</b></font><br/>
    • <b>Processing Fee:</b> Rs. 500.00"""
    f_right = """• <b>Loan Tenure:</b> 10 Monthly Installments<br/>
    • <b>Monthly Installment:</b> <font color='#b45309'><b>Rs. 1,500.00 / month</b></font><br/>
    • <b>First Due Date:</b> 05 Dec 2026<br/>
    • <b>Grace Period:</b> 3 Days (AutoPay Mandate: ACTIVE)"""
    story.append(Table([[Paragraph(f_left, body_style), Paragraph(f_right, body_style)]], colWidths=[263, 268],
                       style=[('BACKGROUND', (0,0), (-1,-1), bg_subtle), ('BOX', (0,0), (-1,-1), 0.5, border_color),
                              ('PADDING', (0,0), (-1,-1), 7), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(Spacer(1, 8))

    # Section 4: Terms & Device Locking Covenants
    story.append(Paragraph("<b>4. TERMS &amp; DEVICE LOCKING COVENANTS</b>", sec_hdr_style))
    story.append(HRFlowable(width='100%', thickness=0.6, color=border_color, spaceBefore=2, spaceAfter=4))
    clauses = [
        "<b>1. Repayment Undertaking:</b> The Borrower acknowledges having received the financed device in good working condition and undertakes to pay each monthly installment of <b>Rs. 1,500.00</b> on or before each scheduled due date for a tenure of <b>10 months</b>.",
        "<b>2. Collateral Security &amp; Device Management:</b> The Borrower authorizes Prime Locker Pro MDM application to be installed and provisioned on the collateral handset (IMEI: <b>866671060012345</b>) to secure this financing facility.",
        "<b>3. Irrevocable Remote Locking Consent:</b> In the event that any monthly installment remains unpaid past the permitted grace period of <b>3 days</b>, the Borrower grants unequivocal consent for the handset to be remotely locked, restricting normal phone usage until dues are settled.",
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
    sig_p = """<font size='7.5' color='#b45309'><b>BORROWER DIGITAL SIGNATURE</b></font><br/>
    <font size='15' color='#1e3a8a'><b><i>Ramesh Kumar</i></b></font><br/>
    ____________________________________________<br/>
    <b>Ramesh Kumar</b><br/>
    <font size='7' color='#64748b'>Digitally Signed via Prime Locker Pro MDM<br/>
    Date: 19 Nov 2026 | Ref: PLP-AGR-01042</font>"""

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
    story.append(Paragraph("This is an authenticated legal electronic record generated and archived by Prime Locker Pro Platform | Agreement: PLP-AGR-01042", footer_style))

    doc.build(story)
    r = pypdf.PdfReader(output_path)
    count = len(r.pages)
    print(f"Sample PDF successfully re-generated: {output_path} (Page count: {count})")
    return count

if __name__ == "__main__":
    generate_sample_pdf()
