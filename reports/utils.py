import csv
from django.http import HttpResponse

def generate_csv_response(filename, headers, rows):
    """
    Utility to stream a CSV response compatible with Microsoft Excel.
    """
    if not filename.endswith('.csv'):
        filename += '.csv'
        
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    # Write UTF-8 BOM so Excel opens special characters correctly
    response.write('\ufeff')
    
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
        
    return response
