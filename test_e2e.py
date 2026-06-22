"""
End-to-End test: Upload IFC → Analyze → AI Design → Generate IFC → Validate
"""
import urllib.request, urllib.error, json, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8') if hasattr(sys.stdout, 'buffer') else sys.stdout

API = 'http://localhost:8765'
TEST_IFC = 'e:/01-claudecode/Building4AI/test_office.ifc'
PASS, FAIL, N = 0, 0, 0

def check(name, ok, detail=''):
    global PASS, FAIL, N; N += 1
    if ok: PASS += 1; print(f'  [{N}] ✅ {name}: {detail}')
    else: FAIL += 1; print(f'  [{N}] ❌ {name}: {detail}')

# ── Step 1: Health ──
print('── Step 1: Health Check ──')
r = urllib.request.urlopen(f'{API}/api/health')
data = json.loads(r.read())
check('Health API', data.get('status') == 'ok', f"version={data.get('version')}")

# ── Step 2: Upload IFC → Analyze ──
print('\n── Step 2: Upload & Analyze ──')
import io as _io
boundary = '---test-boundary-123'
filename = 'test_office.ifc'

with open(TEST_IFC, 'rb') as f:
    file_data = f.read()

body = b''
body += f'--{boundary}\r\n'.encode()
body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
body += b'Content-Type: application/octet-stream\r\n\r\n'
body += file_data
body += f'\r\n--{boundary}--\r\n'.encode()

req = urllib.request.Request(f'{API}/api/analyze', data=body,
    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
r = urllib.request.urlopen(req)
analysis = json.loads(r.read())
check('File name', analysis.get('file') == filename, analysis.get('file'))
check('Summary present', 'summary' in analysis, analysis.get('summary'))
check('Floor height', analysis.get('floorHeight', 0) > 0, str(analysis.get('floorHeight')))
check('Beam top', analysis.get('beamTop', 0) > 0, str(analysis.get('beamTop')))
check('Candidates', len(analysis.get('candidates', [])) > 0, str(len(analysis.get('candidates', []))))

# ── Step 3: AI Design (local algorithm) ──
print('\n── Step 3: AI Design ──')
cand = analysis['candidates'][0]
payload = json.dumps({
    'stairwell': {
        'length_mm': cand['l'],
        'width_mm': cand['w'],
        'floor_height_mm': analysis['floorHeight'],
        'beam_top': analysis['beamTop'],
        'beam_depth': analysis.get('beamDepth', 400)
    },
    'api_key': '',
    'model': 'deepseek-chat'
}).encode('utf-8')

req = urllib.request.Request(f'{API}/api/design', data=payload,
    headers={'Content-Type': 'application/json; charset=utf-8'})
r = urllib.request.urlopen(req)
design_resp = json.loads(r.read())
design = design_resp.get('design', {})
check('Design source', bool(design_resp.get('source')), design_resp.get('source', 'N/A'))
check('Has 2 flights', len(design.get('flights', [])) == 2, f"{len(design.get('flights', []))} flights")
check('Has 2 landings', len(design.get('landings', [])) == 2, f"{len(design.get('landings', []))} landings")
check('Has railings', bool(design.get('railings')), str(design.get('railings')))
for f in design.get('flights', []):
    check(f'{f["name"]} treads', f['n'] > 0, f'{f["n"]}x {f["riser"]}mm riser x {f["tread"]}mm tread')

# ── Step 4: Generate IFC ──
print('\n── Step 4: Generate IFC ──')
gen_payload = json.dumps({
    'flights': design['flights'],
    'landings': design['landings'],
    'stairwell': {'floor_height_mm': analysis['floorHeight']},
    'width_mm': 1200,
    'stairwell_width_mm': cand['w']
}).encode('utf-8')

req = urllib.request.Request(f'{API}/api/generate-ifc', data=gen_payload,
    headers={'Content-Type': 'application/json; charset=utf-8'})
r = urllib.request.urlopen(req)
ifc_data = r.read()
check('IFC not empty', len(ifc_data) > 100, f'{len(ifc_data)} bytes')
check('IFC header', ifc_data.startswith(b'ISO-10303-21'), 'Valid IFC STEP format')

# Save for validation
ifc_path = 'e:/01-claudecode/Building4AI/e2e_output.ifc'
with open(ifc_path, 'wb') as f:
    f.write(ifc_data)

# ── Step 5: Validate generated IFC ──
print('\n── Step 5: Validate Generated IFC ──')
import ifcopenshell
model = ifcopenshell.open(ifc_path)
stair = model.by_type('IfcStair')
flights = model.by_type('IfcStairFlight')
slabs = model.by_type('IfcSlab')
railings = model.by_type('IfcRailing')
check('IfcStair exists', len(stair) == 1, f'{len(stair)} stairs')
check('IfcStairFlight count', len(flights) == design['flights'][0]['n'] + design['flights'][1]['n'], f'{len(flights)} steps')
check('IfcSlab count', len(slabs) >= 3, f'{len(slabs)} slabs (original + 2 landings)')
check('IfcRailing present', len(railings) > 0, f'{len(railings)} railings')
check('Has geometry', len(model.by_type('IfcExtrudedAreaSolid')) > 0, f'{len(model.by_type("IfcExtrudedAreaSolid"))} extrusions')

# ── Result ──
print(f'\n{"="*40}')
print(f'Results: {PASS}/{N} passed, {FAIL} failed')
if FAIL: sys.exit(1)
print('END-TO-END TEST PASSED ✅')
