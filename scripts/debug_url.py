import urllib.request, urllib.error
url = 'https://ftp.ncbi.nlm.nih.gov/genomes/all/GCA/000/412/225/GCA_000412225.2_ASM41222v2/GCA_000412225.2_ASM41222v2_assembly_report.txt'
req = urllib.request.Request(url, headers={'User-Agent': 'alias-mapper/0.1'})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read().decode('utf-8', errors='replace')
        print('OK, got', len(data), 'characters')
        print('First 200 chars:')
        print(data[:200])
except Exception as e:
    print('FAILED:', type(e).__name__, '-', e)