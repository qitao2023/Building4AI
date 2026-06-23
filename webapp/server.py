"""
StructureAI Backend — Zero-dependency HTTP server
Upload IFC → Analyze → AI Design → Download IFC with stair + original structure
"""
import json, urllib.request, urllib.error, tempfile, shutil, re, time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from http.client import HTTPSConnection

import ifcopenshell
from ifcopenshell.api import run

PORT = 8765
LAST_IFC = None  # stored uploaded IFC path
LAST_IFC_MODEL = None  # cached ifcopenshell model (avoid repeated open)
LAST_IFC_CONTEXT = None  # cached extract_ifc_context result
LAST_STAIR_DESIGN = None  # {flights, landings, stairwell, sw_mm, well_w} for 3D overlay
LAST_IFC_NAME_MAP = None  # {GlobalId: fixed_name} for CJK names decoded from raw IFC

# ═══════════════════════════ IFC Name Decoding ══

def _decode_ifc_text(text):
    """Decode IFC STEP \\X2\\...\\X0\\ escape sequences to Unicode (non-regex)."""
    if not text:
        return text
    # Decode \\X2\\hhhh...\\X0\\ sequences
    result = []
    i = 0
    while i < len(text):
        if text.startswith('\\X2\\', i):
            end = text.find('\\X0\\', i + 4)
            if end > i:
                hex_str = text[i+4:end]
                for j in range(0, len(hex_str), 4):
                    cp = int(hex_str[j:j+4], 16)
                    result.append(chr(cp))
                i = end + 4
                continue
        elif text.startswith('\\X\\', i) and i + 6 <= len(text):
            hh = text[i+3:i+5]
            try:
                result.append(chr(int(hh, 16)))
                i += 5
                continue
            except ValueError:
                pass
        result.append(text[i])
        i += 1
    return ''.join(result)

def _build_name_map(ifc_path):
    """Read raw IFC file and build {GlobalId: decoded_name} for entities with CJK names."""
    nmap = {}
    # \X2\ in bytes
    X2_MARK = b'\\X2\\'
    try:
        with open(ifc_path, 'rb') as f:
            for line in f:
                if X2_MARK not in line:
                    continue
                try:
                    txt = line.decode('utf-8', errors='surrogateescape')
                except:
                    continue
                # Split on single quotes: #N= TYPE('gid',...,'name',...)
                parts = txt.split("'")
                if len(parts) < 3:
                    continue
                gid = parts[1]
                # Find first meaningful name (skip $, empty, #refs)
                for p in parts[2:]:
                    p = p.strip()
                    if not p or p == '$' or p.startswith('#'):
                        continue
                    if any(c.isalpha() for c in p):
                        nmap[gid] = _decode_ifc_text(p)
                        break
    except Exception:
        pass
    return nmap

# ═══════════════════════════════════ IFC Analysis ══
def _fix_storey_names(ctx, name_map):
    """Fix garbled storey names using decoded names from raw IFC."""
    if not name_map:
        return
    for s in ctx.get("storeys", []):
        gid = s.get("global_id")
        if gid and gid in name_map:
            s["name"] = name_map[gid]

def _get_entity_z(e):
    """Quickly get Z coordinate from an entity's ObjectPlacement (no tessellation)."""
    try:
        op = getattr(e, "ObjectPlacement", None)
        if op and hasattr(op, "RelativePlacement") and hasattr(op.RelativePlacement, "Location"):
            lc = op.RelativePlacement.Location
            cs = lc.Coordinates if hasattr(lc, 'Coordinates') else lc
            return float(cs[2])
    except:
        pass
    return 0.0

def extract_ifc_context(fp):
    m = ifcopenshell.open(fp)
    ctx = {"storeys":[],"beams":[],"columns":[],"slabs":[],"walls":[],"floor_heights":{},"column_positions":[],"beam_elevations":set()}
    for s in m.by_type("IfcBuildingStorey"):
        e = getattr(s,"Elevation",None); eh = e.wrappedValue if hasattr(e,'wrappedValue') else (float(e) if e else 0)
        ctx["storeys"].append({"global_id":s.GlobalId,"name":s.Name or "?","elevation":eh})
    for b in m.by_type("IfcBeam"):
        info = ent_info(b); ctx["beams"].append(info)
        if info["pos"][2]: ctx["beam_elevations"].add(round(info["pos"][2]/100)*100)
    for c in m.by_type("IfcColumn"):
        info = ent_info(c); ctx["columns"].append(info); ctx["column_positions"].append(info["pos"])
    for s in m.by_type("IfcSlab"): ctx["slabs"].append(ent_info(s))
    for w in m.by_type("IfcWall"): ctx["walls"].append(ent_info(w))
    ss = sorted(ctx["storeys"], key=lambda s:s["elevation"])
    for i,s in enumerate(ss):
        ctx["floor_heights"][s["name"]] = round(ss[i+1]["elevation"]-s["elevation"]) if i+1<len(ss) else 4500
    cx=sorted(set(round(c["pos"][0]/100)*100 for c in ctx["columns"] if c["pos"][0]))
    cy=sorted(set(round(c["pos"][1]/100)*100 for c in ctx["columns"] if c["pos"][1]))
    ctx["col_x"]=cx; ctx["col_y"]=cy
    ctx["beam_elevations"]=sorted(ctx["beam_elevations"])
    ctx["candidates"]=detect_candidates(ctx)
    return ctx

def ent_info(e):
    info={"global_id":e.GlobalId,"name":e.Name or "","type":e.is_a(),"pos":(0,0,0),"size":(200,200,3000)}
    for rel in getattr(e,"IsDefinedBy",[]) or []:
        if not rel.is_a("IfcRelDefinesByProperties"): continue
        ps=rel.RelatingPropertyDefinition
        if not ps: continue
        props={}
        for p in getattr(ps,"HasProperties",[]) or []:
            if hasattr(p,"NominalValue") and p.NominalValue:
                props[p.Name]=p.NominalValue.wrappedValue if hasattr(p.NominalValue,'wrappedValue') else p.NominalValue
        w=props.get("Width") or props.get("width") or 200
        d=props.get("Depth") or props.get("depth") or 200
        l=props.get("Length") or props.get("length") or 3000
        info["size"]=(float(w),float(d),float(l))
    op=getattr(e,"ObjectPlacement",None)
    if op and hasattr(op,"RelativePlacement") and hasattr(op.RelativePlacement,"Location"):
        lc=op.RelativePlacement.Location; cs=lc.Coordinates if hasattr(lc,'Coordinates') else lc
        try: info["pos"]=(float(cs[0]),float(cs[1]),float(cs[2]))
        except: pass
    return info

def detect_candidates(ctx):
    gx,gy=ctx["col_x"],ctx["col_y"]; cands=[]
    if len(gx)<2 or len(gy)<2:
        ax=[c["pos"][0] for c in ctx["columns"] if c["pos"][0]]
        ay=[c["pos"][1] for c in ctx["columns"] if c["pos"][1]]
        if ax and ay: cands.append({"name":"全区域","length_mm":round(max(ax)-min(ax)),"width_mm":round(max(ay)-min(ay))})
        return cands
    for i in range(len(gx)-1):
        if gx[i+1]-gx[i]>2000:
            for j in range(len(gy)-1):
                if gy[j+1]-gy[j]>2000:
                    cands.append({"name":f"X{gx[i]}-{gx[i+1]} Y{gy[j]}-{gy[j+1]}","length_mm":gx[i+1]-gx[i],"width_mm":gy[j+1]-gy[j]})
    if not cands: cands.append({"name":"全区域","length_mm":max(gx)-min(gx),"width_mm":max(gy)-min(gy)})
    return cands

# ═══════════════════════════════════ AI ══
PROMPT = """Design a double-run stair per GB 50010. Output ONLY valid JSON.
Stairwell: {sw}
Rules: riser≤175, tread≥260, landing≥1200, width≥1100, headroom≥2200, railing=900.
JSON: {{"type":"double_run","width_mm":N,"flights":[{{"id":"F1","name":"第一跑","tread_count":N,"riser_height_mm":N,"tread_depth_mm":N,"start_at_bottom":true,"length_mm":N,"height_mm":N}},{{"id":"F2","name":"第二跑","tread_count":N,"riser_height_mm":N,"tread_depth_mm":N,"start_at_bottom":false,"length_mm":N,"height_mm":N}}],"landings":[{{"id":"L1","name":"中间平台","length_mm":N,"width_mm":N,"thickness_mm":150,"elevation_mm":N}},{{"id":"L2","name":"楼层平台","length_mm":N,"width_mm":N,"thickness_mm":150,"elevation_mm":N}}],"railings":[{{"id":"R1","name":"左侧栏杆","height_mm":900}},{{"id":"R2","name":"右侧栏杆","height_mm":900}}]}}"""

def call_ai(sw, key, model, endpoint):
    p = PROMPT.format(sw=json.dumps(sw, indent=2, ensure_ascii=False))
    payload = json.dumps({"model":model,"messages":[{"role":"system","content":"Output only valid JSON, no markdown."},{"role":"user","content":p}],"temperature":0.3,"max_tokens":4096}, ensure_ascii=False).encode('utf-8')
    try:
        u = urlparse(endpoint)
        conn = HTTPSConnection(u.hostname, u.port or 443, timeout=120)
        conn.request("POST","/v1/chat/completions",body=payload,
            headers={"Content-Type":"application/json; charset=utf-8","Authorization":("Bearer "+key).encode('ascii','ignore').decode('ascii')})
        r = conn.getresponse(); raw = r.read(); conn.close()
        if r.status != 200: return {"ok":False,"err":f"HTTP {r.status}: {raw.decode('utf-8','replace')[:300]}"}
        data = json.loads(raw)
        msg = data.get("choices",[{}])[0].get("message",{})
        content = msg.get("content","")
        if not content and msg.get("reasoning_content"):
            rc = msg["reasoning_content"]; s = rc.rfind('{'); e = rc.rfind('}')
            if s>=0 and e>s: content = rc[s:e+1]
        if not content: return {"ok":False,"err":"Empty response","debug":json.dumps(data,ensure_ascii=False)[:500]}
        content = content.strip()
        # Extract JSON
        depth = 0; start = content.find('{')
        if start < 0: return {"ok":False,"err":"No JSON found","debug":content[:500]}
        for i in range(start, len(content)):
            if content[i]=='{': depth+=1
            elif content[i]=='}': depth-=1
            if depth==0: content=content[start:i+1]; break
        design = json.loads(content)
        # Normalize
        for f in design.get("flights",[]):
            f.setdefault("tread_depth_mm",280); f.setdefault("riser_height_mm",150)
            f.setdefault("length_mm",f.get("tread_count",10)*f.get("tread_depth_mm",280))
            f.setdefault("height_mm",f.get("tread_count",10)*f.get("riser_height_mm",150))
            f.setdefault("start_at_bottom",f.get("id")=="F1")
        for ld in design.get("landings",[]): ld.setdefault("thickness_mm",150); ld.setdefault("width_mm",sw.get("width_mm",2700))
        design.setdefault("type","double_run"); design.setdefault("width_mm",sw.get("width_mm",1200)-300)
        return {"ok":True,"design":design,"source":f"DeepSeek AI ({model})","raw":content[:2000]}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"ok":False,"err":str(e)}

def algo_design(sw):
    L,W,H=sw["length_mm"],sw["width_mm"],sw["floor_height_mm"]
    # Stair width
    sw2=min(W-300,1200); sw2=max(sw2,1100)
    td=280  # tread depth
    # Total risers from ideal 150mm rise
    tr=round(H/150)
    rh=H/tr
    # Available X space (columns at both ends)
    col_margin=250
    avail=L-2*col_margin
    # Landing: min 1200mm, proportional to available space
    ll=max(round(avail/5),1200)
    # Max flight length that fits
    max_fl=(avail-ll)/2
    # Constrain steps per flight to fit
    ideal_rpf=tr//2
    max_rpf=int(max_fl/td)
    rpf=min(ideal_rpf,max_rpf)
    rpf=max(rpf,3)  # at least 3 steps
    # Recalculate with constrained steps
    fl=rpf*td
    f2_steps=tr-rpf
    # Second flight: constrain to same max, or adjust if less
    if f2_steps*td>max_fl:
        f2_steps=max_rpf
    fl2=f2_steps*td
    fh1=round(rh*rpf)
    fh2=round(rh*f2_steps)
    return {"type":"double_run","width_mm":sw2,"flights":[
        {"id":"F1","name":"第一跑","tread_count":rpf,"riser_height_mm":round(rh),"tread_depth_mm":td,"start_at_bottom":True,"length_mm":fl,"height_mm":fh1},
        {"id":"F2","name":"第二跑","tread_count":f2_steps,"riser_height_mm":round(rh),"tread_depth_mm":td,"start_at_bottom":False,"length_mm":fl2,"height_mm":fh2}
    ],"landings":[
        {"id":"L1","name":"中间平台","length_mm":ll,"width_mm":W,"thickness_mm":150,"elevation_mm":fh1},
        {"id":"L2","name":"楼层平台","length_mm":ll,"width_mm":W,"thickness_mm":150,"elevation_mm":H}
    ],"railings":[
        {"id":"R1","name":"左侧栏杆","height_mm":900},
        {"id":"R2","name":"右侧栏杆","height_mm":900}
    ]}

def check_rules(d, sw):
    errs=[]
    if d.get("width_mm",0)<1100: errs.append({"rule":"width","msg":f"宽度<1100"})
    for f in d.get("flights",[]):
        if f.get("riser_height_mm",0)>175: errs.append({"rule":"riser","loc":f.get("id"),"msg":"踏步高>175"})
        if f.get("tread_depth_mm",0)<260: errs.append({"rule":"tread","loc":f.get("id"),"msg":"踏步深<260"})
    for ld in d.get("landings",[]):
        if ld.get("length_mm",0)<1200: errs.append({"rule":"landing","loc":ld.get("id"),"msg":"平台深<1200"})
    bb=sw.get("beam_position_top_mm",sw["floor_height_mm"])-sw.get("beam_depth_mm",400)
    for ld in d.get("landings",[]):
        if "楼层" in ld.get("name",""): continue
        hr=bb-ld.get("elevation_mm",0)
        if hr<2200: errs.append({"rule":"headroom","loc":ld.get("id"),"msg":f"净高{hr}<2200"})
    return errs

# ═══════════════════════════════════ IFC Generation ══
def generate_stair_ifc(base_ifc_path, flights_data, landings_data, stairwell, sw_mm, well_w_mm=2700):
    """Load base IFC, add stair entities with 3D geometry, return temp path."""
    model = ifcopenshell.open(base_ifc_path)

    # Find existing storey
    storeys = model.by_type("IfcBuildingStorey")
    sty = storeys[0] if storeys else run("root.create_entity", model, ifc_class="IfcBuildingStorey", name="F1")

    stair = run("root.create_entity", model, ifc_class="IfcStair", name="AI楼梯")
    run("spatial.assign_container", model, products=[stair], relating_structure=sty)
    # IfcStair needs its own placement (even if identity)
    stair.ObjectPlacement = model.create_entity('IfcLocalPlacement',
        PlacementRelTo=sty.ObjectPlacement,
        RelativePlacement=model.create_entity('IfcAxis2Placement3D',
            Location=model.create_entity('IfcCartesianPoint', Coordinates=[0.,0.,0.]),
            Axis=model.create_entity('IfcDirection', DirectionRatios=[0.,0.,1.]),
            RefDirection=model.create_entity('IfcDirection', DirectionRatios=[1.,0.,0.])))

    # Geometric context — use main context, NOT SubContext (viewer compatibility)
    proj = model.by_type("IfcProject")[0]
    if not getattr(proj, "RepresentationContexts", None):
        gctx = model.create_entity('IfcGeometricRepresentationContext',
            ContextType='Model', ContextIdentifier='Body',
            CoordinateSpaceDimension=3, Precision=0.001,
            WorldCoordinateSystem=model.create_entity('IfcAxis2Placement3D',
                Location=model.create_entity('IfcCartesianPoint', Coordinates=[0.,0.,0.]),
                Axis=model.create_entity('IfcDirection', DirectionRatios=[0.,0.,1.]),
                RefDirection=model.create_entity('IfcDirection', DirectionRatios=[1.,0.,0.])))
        proj.RepresentationContexts = [gctx]
    else:
        gctx = proj.RepresentationContexts[0]

    # Ensure storey has ObjectPlacement and CompositionType
    if not sty.ObjectPlacement:
        sty.ObjectPlacement = model.create_entity('IfcLocalPlacement',
            RelativePlacement=model.create_entity('IfcAxis2Placement3D',
                Location=model.create_entity('IfcCartesianPoint', Coordinates=[0.,0.,0.]),
                Axis=model.create_entity('IfcDirection', DirectionRatios=[0.,0.,1.]),
                RefDirection=model.create_entity('IfcDirection', DirectionRatios=[1.,0.,0.])))
    if not getattr(sty, 'CompositionType', None):
        sty.CompositionType = 'ELEMENT'

    def mk_place(e, x, y, z):
        e.ObjectPlacement = model.create_entity('IfcLocalPlacement',
            PlacementRelTo=sty.ObjectPlacement,
            RelativePlacement=model.create_entity('IfcAxis2Placement3D',
                Location=model.create_entity('IfcCartesianPoint', Coordinates=[float(x),float(y),float(z)]),
                Axis=model.create_entity('IfcDirection', DirectionRatios=[0.,0.,1.]),
                RefDirection=model.create_entity('IfcDirection', DirectionRatios=[1.,0.,0.])))

    def add_geom(e, w, d, l, ox=0, oy=0, oz=0):
        r = model.create_entity('IfcRectangleProfileDef', ProfileType='AREA', XDim=float(w), YDim=float(d))
        s = model.create_entity('IfcExtrudedAreaSolid', SweptArea=r,
            Position=model.create_entity('IfcAxis2Placement3D',
                Location=model.create_entity('IfcCartesianPoint', Coordinates=[float(ox),float(oy),float(oz)]),
                Axis=model.create_entity('IfcDirection', DirectionRatios=[0.,0.,1.]),
                RefDirection=model.create_entity('IfcDirection', DirectionRatios=[1.,0.,0.])),
            ExtrudedDirection=model.create_entity('IfcDirection', DirectionRatios=[0.,0.,1.]), Depth=float(l))
        e.Representation = model.create_entity('IfcProductDefinitionShape',
            Representations=[model.create_entity('IfcShapeRepresentation', ContextOfItems=gctx,
                RepresentationIdentifier='Body', RepresentationType='SweptSolid', Items=[s])])

    f1 = flights_data[0] if flights_data else {}
    f2 = flights_data[1] if len(flights_data) > 1 else {}
    fl1 = f1.get("len", 4200); fh1 = f1.get("h", 2250)
    ll_val = landings_data[0].get("l", 1200) if landings_data else 1200
    mid_el = fh1; H = stairwell.get("floor_height_mm", 4500)

    # ── Direction & coordinate system ──
    length_mm = stairwell.get("length_mm", 6000)
    width_mm  = stairwell.get("width_mm", 2700)
    along_z = width_mm > length_mm  # True = stair runs along IFC Y

    ox = stairwell.get("origin_x", 0)
    oy = stairwell.get("origin_y", 0)
    storey_elev = stairwell.get("storey_elevation", 0)

    col_half = 250
    along0 = col_half

    if along_z:
        ac_span, al_span = length_mm, width_mm
        def ics(e, along, across, z):
            mk_place(e, ox+across, oy+along, storey_elev+z)
        def igm(e, along_d, across_d, z_d):
            add_geom(e, across_d, along_d, z_d)
    else:
        ac_span, al_span = width_mm, length_mm
        def ics(e, along, across, z):
            mk_place(e, ox+along, oy+across, storey_elev+z)
        def igm(e, along_d, across_d, z_d):
            add_geom(e, along_d, across_d, z_d)

    ac_margin = (ac_span - sw_mm) // 2

    # Collect all new elements for batch spatial containment
    _stair_elements = []

    # Landings
    for ld in landings_data:
        slab = run("root.create_entity", model, ifc_class="IfcSlab", name=ld.get("name","Landing"))
        run("aggregate.assign_object", model, products=[slab], relating_object=stair)
        _stair_elements.append(slab)
        ll=ld.get("l",1200); lw=ld.get("w",well_w_mm); lt=ld.get("t",150); el=ld.get("el",0)
        along_off = along0 if "楼层" in ld.get("name","") else along0 + fl1
        ics(slab, along_off, ac_margin, el)
        igm(slab, ll, ac_span, lt)

    # Flight 1
    if flights_data:
        f = flights_data[0]; n=f.get("n",10); td=f.get("tread",280); rh=f.get("riser",150)
        for s in range(n):
            step = run("root.create_entity", model, ifc_class="IfcStairFlight", name=f"{f.get('name','F1')}_s{s+1}")
            run("aggregate.assign_object", model, products=[step], relating_object=stair)
            _stair_elements.append(step)
            ics(step, along0 + s*td, ac_margin, s*rh)
            igm(step, td, sw_mm, rh)

    # Flight 2
    if len(flights_data) > 1:
        f = flights_data[1]; n=f.get("n",10); td=f.get("tread",280); rh=f.get("riser",150)
        along_start2 = fl1 + ll_val
        for s in range(n):
            step = run("root.create_entity", model, ifc_class="IfcStairFlight", name=f"{f.get('name','F2')}_s{s+1}")
            run("aggregate.assign_object", model, products=[step], relating_object=stair)
            _stair_elements.append(step)
            ics(step, along0 + along_start2 - (s+1)*td, ac_margin, mid_el+s*rh)
            igm(step, td, sw_mm, rh)

    # Railings
    for fi, f in enumerate(flights_data):
        n=f.get("n",10); td=f.get("tread",280); rh_s=f.get("riser",150)
        if fi==0: al_start, z_start, al_dir = along0, 0, 1
        else:     al_start, z_start, al_dir = along0+fl1+ll_val, mid_el, -1
        for side, ac_off in [("L",ac_margin+50),("R",ac_margin+sw_mm-50)]:
            for si in range(0,n+1,3):
                step_idx = min(si,n-1)
                p_al = al_start + al_dir*step_idx*td + al_dir*td//2
                pz   = z_start + step_idx*rh_s
                post=run("root.create_entity",model,ifc_class="IfcRailing",name=f"Post_{fi}_{side}_{si}")
                run("aggregate.assign_object",model,products=[post],relating_object=stair)
                _stair_elements.append(post)
                ics(post, p_al-20, ac_off-20, pz); igm(post, 40, 40, 900)
            fp = al_start + al_dir*td//2
            lp = al_start + al_dir*(n-1)*td + al_dir*td//2
            rail=run("root.create_entity",model,ifc_class="IfcRailing",name=f"Rail_{fi}_{side}")
            run("aggregate.assign_object",model,products=[rail],relating_object=stair)
            _stair_elements.append(rail)
            ics(rail, min(fp,lp), ac_off-20, z_start+(n//2)*rh_s+900)
            igm(rail, abs(lp-fp)+40, 40, 40)

    # Batch spatial containment: all stair elements belong to the storey
    if _stair_elements:
        run("spatial.assign_container", model, products=_stair_elements, relating_structure=sty)

    tmp = tempfile.NamedTemporaryFile(suffix=".ifc", delete=False)
    model.write(tmp.name); tmp.close()
    return tmp.name

# ═══════════════════════════ Stair 3D Mesh (additive overlay) ══
def build_stair_mesh_elements(flights, landings, stairwell, sw_mm, well_w_mm):
    """Generate 3D stair mesh elements directly from design params.
    Returns list of elements in the same format as _geometry() output — additive overlay.
    All units in mm. No IFC round-trip needed for visualization."""
    elements = []
    f1 = flights[0] if flights else {}
    f2 = flights[1] if len(flights) > 1 else {}
    fl1 = f1.get("len", 2800)
    fh1 = f1.get("h", 1500)
    ll_val = landings[0].get("l", 1200) if landings else 1200

    # ── Direction & coordinate system ──
    # Stair runs along the longer side of the stairwell
    length_mm = stairwell.get("length_mm", 6000)
    width_mm  = stairwell.get("width_mm", 2700)
    along_z = width_mm > length_mm  # True = stair runs along IFC Y (world Z)

    # Absolute position & storey elevation
    ox = stairwell.get("origin_x", 0)
    oy = stairwell.get("origin_y", 0)
    storey_elev = stairwell.get("storey_elevation", 0)

    col_half = 250
    along0 = col_half  # start offset from stairwell edge along flight

    # Axis mapping helpers — swap X↔Y when stair runs along Z (IFC Y)
    if along_z:
        ac_span = length_mm       # across span = X (shorter side)
        al_span = width_mm        # along span  = Y (longer side)
        def mkpos(along, across, z):
            return [round(ox + across), round(oy + along), round(storey_elev + z)]
        def mksize(along_dim, across_dim, z_dim):
            return [round(across_dim), round(along_dim), round(z_dim)]
    else:
        ac_span = width_mm        # across span = Y
        al_span = length_mm       # along span  = X
        def mkpos(along, across, z):
            return [round(ox + along), round(oy + across), round(storey_elev + z)]
        def mksize(along_dim, across_dim, z_dim):
            return [round(along_dim), round(across_dim), round(z_dim)]

    ac_margin = (ac_span - sw_mm) // 2
    ac_center = ac_span // 2

    # ── Landings ──
    for ld in landings:
        ll = ld.get("l", 1200)
        lt = ld.get("t", 150)
        el = ld.get("el", 0)
        is_floor = "楼层" in ld.get("name", "")
        along_off = along0 if is_floor else along0 + fl1
        elements.append({
            "global_id": f"stair_landing_{ld.get('name','')}",
            "name": ld.get("name", "Landing"),
            "type": "IfcSlab",
            "pos": mkpos(along_off + ll/2, ac_center, el),
            "size": mksize(ll, ac_span, max(lt, 150)),
            "color": "#aabbcc"
        })

    # ── Flight 1 ──
    if flights:
        f = flights[0]; n = f.get("n", 9); td = f.get("tread", 280); rh = f.get("riser", 150)
        for s in range(n):
            elements.append({
                "global_id": f"stair_F1_s{s+1}",
                "name": f"{f.get('name','F1')}_s{s+1}",
                "type": "IfcStairFlight",
                "pos": mkpos(along0 + s*td + td/2, ac_center, s*rh),
                "size": mksize(td, sw_mm, rh),
                "color": "#c8d6e5"
            })

    # ── Flight 2 ──
    if len(flights) > 1:
        f = flights[1]; n = f.get("n", 9); td = f.get("tread", 280); rh = f.get("riser", 150)
        along_start2 = fl1 + ll_val
        for s in range(n):
            elements.append({
                "global_id": f"stair_F2_s{s+1}",
                "name": f"{f.get('name','F2')}_s{s+1}",
                "type": "IfcStairFlight",
                "pos": mkpos(along0 + along_start2 - (s+1)*td + td/2, ac_center, fh1 + s*rh),
                "size": mksize(td, sw_mm, rh),
                "color": "#d6e0f0"
            })

    # ── Railing posts ──
    for fi, f in enumerate(flights):
        n = f.get("n", 9); td = f.get("tread", 280); rh_s = f.get("riser", 150)
        if fi == 0:
            al_start, z_start, al_dir = along0, 0, 1
        else:
            al_start, z_start, al_dir = along0 + fl1 + ll_val, fh1, -1
        for side, ac_off in [("L", 50), ("R", sw_mm - 50)]:
            for si in range(0, n + 1, 3):
                step_idx = min(si, n - 1)
                p_al = al_start + al_dir * step_idx * td + al_dir * td // 2
                pz   = z_start + step_idx * rh_s
                elements.append({
                    "global_id": f"stair_post_{fi}_{side}_{si}",
                    "name": f"Post_F{fi+1}_{side}_{si}",
                    "type": "IfcRailing",
                    "pos": mkpos(p_al, ac_margin + ac_off, pz),
                    "size": mksize(40, 40, 900),
                    "color": "#888888"
                })

    return elements

# ═══════════════════════════════════ HTTP ══

def _get_ifc_color(element):
    """Extract RGB color from IFC material chain. Returns [r,g,b] or None."""
    for assoc in getattr(element, 'HasAssociations', []) or []:
        if not assoc.is_a('IfcRelAssociatesMaterial'): continue
        mat_rel = assoc.RelatingMaterial
        if not mat_rel: continue
        if mat_rel.is_a('IfcMaterialLayerSetUsage'):
            ls = mat_rel.ForLayerSet
            if ls:
                for layer in getattr(ls, 'MaterialLayers', []) or []:
                    m = getattr(layer, 'Material', None)
                    if m:
                        c = _color_from_material(m)
                        if c: return c
        elif mat_rel.is_a('IfcMaterial'):
            c = _color_from_material(mat_rel)
            if c: return c
    # Try type definition
    for rel in getattr(element, 'IsDefinedBy', []) or []:
        if rel.is_a('IfcRelDefinesByType') and rel.RelatingType:
            c = _get_ifc_color(rel.RelatingType)
            if c: return c
    return None

def _color_from_material(mat):
    for rep in getattr(mat, 'HasRepresentation', []) or []:
        if not rep.is_a('IfcMaterialDefinitionRepresentation'): continue
        for r in getattr(rep, 'Representations', []) or []:
            for item in getattr(r, 'Items', []) or []:
                if not item.is_a('IfcStyledItem'): continue
                for sa in getattr(item, 'Styles', []) or []:
                    for style in getattr(sa, 'Styles', []) or []:
                        if not style.is_a('IfcSurfaceStyle'): continue
                        for ss in getattr(style, 'Styles', []) or []:
                            if ss.is_a('IfcSurfaceStyleRendering'):
                                c = ss.SurfaceColour
                                if c:
                                    r = getattr(c, 'Red', None)
                                    g = getattr(c, 'Green', None)
                                    b = getattr(c, 'Blue', None)
                                    if r is not None:
                                        return [int(r*255), int(g*255), int(b*255)]
    return None

# ═══════════════════════════════ Geometry Extraction (shared helper) ══

def _process_element(e, settings, opening_to_wall):
    """Process a single IFC element: tessellate geometry, compute bbox, return element info dict.
    Returns None for entities without valid geometry."""
    import ifcopenshell.geom as geom
    info = {"global_id": e.GlobalId, "name": e.Name or "", "type": e.is_a()}
    # Normalize type name
    for bt in ('IfcWall', 'IfcBeam', 'IfcColumn', 'IfcSlab', 'IfcStair', 'IfcStairFlight', 'IfcRailing', 'IfcOpeningElement'):
        if e.is_a(bt):
            info["type"] = bt
            break
    # Extract IFC material color
    try:
        c = _get_ifc_color(e)
        if c: info["color"] = c
    except: pass
    # Tessellate geometry
    try:
        shape = geom.create_shape(settings, e)
    except Exception:
        return None  # skip entities without valid geometry
    verts = list(shape.geometry.verts)
    m4 = shape.transformation.matrix
    xs, ys, zs = [], [], []
    for i in range(0, len(verts), 3):
        lx, ly, lz = verts[i], verts[i+1], verts[i+2]
        wx = m4[0]*lx + m4[4]*ly + m4[8]*lz  + m4[12]
        wy = m4[1]*lx + m4[5]*ly + m4[9]*lz  + m4[13]
        wz = m4[2]*lx + m4[6]*ly + m4[10]*lz + m4[14]
        xs.append(wx); ys.append(wy); zs.append(wz)
    sx_w = round((max(xs) - min(xs)) * 1000)
    sy_w = round((max(ys) - min(ys)) * 1000)
    sz_w = round((max(zs) - min(zs)) * 1000)
    cx_w = round((min(xs) + max(xs)) / 2 * 1000)
    cy_w = round((min(ys) + max(ys)) / 2 * 1000)
    cz_min = round(min(zs) * 1000)
    cz_max = round(max(zs) * 1000)
    if info["type"] == 'IfcColumn':
        spans = [(sx_w, min(xs)*1000, max(xs)*1000),
                 (sy_w, min(ys)*1000, max(ys)*1000),
                 (sz_w, cz_min, cz_max)]
        spans.sort(key=lambda x: x[0], reverse=True)
        height, bot, top = spans[0]
        info["size"] = [spans[1][0], spans[2][0], height]
        info["pos"] = [cx_w, cy_w, round(bot)]
    elif info["type"] in ('IfcBeam', 'IfcWall'):
        if sx_w >= sy_w:
            info["dir"] = "x"
            info["size"] = [sy_w, sz_w, sx_w]
        else:
            info["dir"] = "y"
            info["size"] = [sx_w, sz_w, sy_w]
        info["pos"] = [cx_w, cy_w, cz_min]
        # Note: name dimension replacement moved to _geometry() paths with steelProfile guard
    elif info["type"] == 'IfcOpeningElement':
        sp = sorted([sx_w, sy_w, sz_w])
        info["size"] = [sp[0], sp[2], sp[1]]
        info["dir"] = "x" if sx_w >= sy_w else "y"
        info["pos"] = [cx_w, cy_w, cz_min]
        info["parent_wall"] = opening_to_wall.get(e.GlobalId)
    elif info["type"] == 'IfcSlab':
        info["pos"] = [cx_w, cy_w, cz_min]
        info["size"] = [sx_w, sy_w, max(sz_w, 150)]
    else:
        info["pos"] = [cx_w, cy_w, cz_min]
        info["size"] = [sx_w, sy_w, sz_w]
    # Strip trailing :number for all element types (e.g. "混凝土墙_300mm:485346" → "混凝土墙_300mm")
    info["name"] = re.sub(r':\d+$', '', info["name"])
    # ── Detect H-shaped steel beams/columns from name ──
    _detect_steel_profile(info)
    return info


# ── Standard Chinese I-beam (工字钢) section table ──
# GB/T 706-2016 hot-rolled I-beam: model → (H, B, tw, tf) in mm
_I_BEAM_TABLE = {
    'I10':   (100, 68,  4.5,  7.6),
    'I12.6': (126, 74,  5.0,  8.4),
    'I12':   (120, 74,  5.0,  8.4),  # alias
    'I14':   (140, 80,  5.5,  9.1),
    'I16':   (160, 88,  6.0,  9.9),
    'I18':   (180, 94,  6.5, 10.7),
    'I20a':  (200, 100,  7.0, 11.4),
    'I20b':  (200, 102,  9.0, 11.4),
    'I20':   (200, 100,  7.0, 11.4),  # alias → I20a
    'I22a':  (220, 110,  7.5, 12.3),
    'I22b':  (220, 112,  9.5, 12.3),
    'I22':   (220, 110,  7.5, 12.3),  # alias → I22a
    'I25a':  (250, 116,  8.0, 13.0),
    'I25b':  (250, 118, 10.0, 13.0),
    'I25':   (250, 116,  8.0, 13.0),  # alias → I25a
    'I28a':  (280, 122,  8.5, 13.7),
    'I28b':  (280, 124, 10.5, 13.7),
    'I28':   (280, 122,  8.5, 13.7),  # alias → I28a
    'I32a':  (320, 130,  9.5, 15.0),
    'I32b':  (320, 132, 11.5, 15.0),
    'I32c':  (320, 134, 13.5, 15.0),
    'I32':   (320, 130,  9.5, 15.0),  # alias → I32a
    'I36a':  (360, 136, 10.0, 15.8),
    'I36b':  (360, 138, 12.0, 15.8),
    'I36c':  (360, 140, 14.0, 15.8),
    'I36':   (360, 136, 10.0, 15.8),  # alias → I36a
    'I40a':  (400, 142, 10.5, 16.5),
    'I40b':  (400, 144, 12.5, 16.5),
    'I40c':  (400, 146, 14.5, 16.5),
    'I40':   (400, 142, 10.5, 16.5),  # alias → I40a
    'I45a':  (450, 150, 11.5, 18.0),
    'I45b':  (450, 152, 13.5, 18.0),
    'I45c':  (450, 154, 15.5, 18.0),
    'I45':   (450, 150, 11.5, 18.0),  # alias → I45a
    'I50a':  (500, 158, 12.0, 20.0),
    'I50b':  (500, 160, 14.0, 20.0),
    'I50c':  (500, 162, 16.0, 20.0),
    'I50':   (500, 158, 12.0, 20.0),  # alias → I50a
    'I56a':  (560, 166, 12.5, 21.0),
    'I56b':  (560, 168, 14.5, 21.0),
    'I56c':  (560, 170, 16.5, 21.0),
    'I56':   (560, 166, 12.5, 21.0),  # alias → I56a
    'I63a':  (630, 176, 13.0, 22.0),
    'I63b':  (630, 178, 15.0, 22.0),
    'I63c':  (630, 180, 17.0, 22.0),
    'I63':   (630, 176, 13.0, 22.0),  # alias → I63a
}


def _detect_steel_profile(info):
    """Detect steel profile type from element name, and parse profile params.
    Sets info['steelProfile'] = {'type':'H', 'H':..., 'B':..., ...} or {'type':'CHS','D':...,'t':...}
    or just a type string if params cannot be parsed."""
    if info["type"] not in ('IfcBeam', 'IfcColumn'):
        return
    decoded = _decode_ifc_text(info["name"])

    # ── Circular Hollow Section (圆管 / ΦDxt) ──
    if '圆管' in decoded or re.search(r'[Φϕ][\d.]+', decoded):
        m = re.search(r'[Φϕ]?(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)', decoded)
        if m:
            D = float(m.group(1))
            t = float(m.group(2))
            info["steelProfile"] = {"type": "CHS", "D": D, "t": t}
        else:
            info["steelProfile"] = "CHS"
        return

    # ── I-beam (工字钢) — standard sections from GB/T 706 lookup table ──
    if '工字钢' in decoded:
        m = re.search(r'\b(I\d+(?:\.\d+)?[a-c]?)\b', decoded, re.IGNORECASE)
        if not m:
            # Also try raw name
            m = re.search(r'\b(I\d+(?:\.\d+)?[a-c]?)\b', info["name"], re.IGNORECASE)
        if m:
            key = m.group(1).upper()
            # Try case-insensitive lookup
            match_key = None
            for k in _I_BEAM_TABLE:
                if k.upper() == key:
                    match_key = k
                    break
            if match_key:
                H, B, tw, tf = _I_BEAM_TABLE[match_key]
                info["steelProfile"] = {"type": "H", "H": H, "B": B, "tw": tw, "tf": tf}
                return
        info["steelProfile"] = "H"  # I-beam detected but model not in table
        return

    # ── H-shaped steel (H型钢 / H形钢 / HdimXdim...) ──
    is_h_steel = False
    if 'H型钢' in decoded or 'H形钢' in decoded:
        is_h_steel = True
    elif re.search(r'(?:^|:)H[\(（]?\d+', decoded, re.IGNORECASE):
        is_h_steel = True
    elif re.search(r'\bH\d+\s*[xX×]\s*\d+', info["name"], re.IGNORECASE):
        is_h_steel = True

    if not is_h_steel:
        return

    # Parse H profile dimensions: H(H1/H2)XBXtwXtf or HHeightXBXtwXtf
    m = re.search(r'H[\(（]?(\d+(?:\.\d+)?)(?:[/／](\d+(?:\.\d+)?))?[\)）]?\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)',
                  decoded, re.IGNORECASE)
    if not m:
        m = re.search(r'H[\(（]?(\d+(?:\.\d+)?)(?:[/／](\d+(?:\.\d+)?))?[\)）]?\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)',
                      info["name"], re.IGNORECASE)

    if m:
        h1 = float(m.group(1))
        h2 = float(m.group(2)) if m.group(2) else None
        b  = float(m.group(3))
        tw = float(m.group(4))
        tf = float(m.group(5))
        info["steelProfile"] = {
            "type": "H",
            "H": h1 if h2 is None else max(h1, h2),
            "H1": h1,
            "H2": h2,
            "B": b,
            "tw": tw,
            "tf": tf,
        }
    else:
        info["steelProfile"] = "H"


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","*")
    def _json(self, data, code=200):
        self.send_response(code); self._cors(); self.send_header("Content-Type","application/json; charset=utf-8"); self.end_headers()
        self.wfile.write(json.dumps(data,ensure_ascii=False).encode())
    def _file(self, path, code=200):
        self.send_response(code); self._cors(); self.send_header("Content-Type","application/octet-stream")
        self.send_header("Content-Disposition","attachment; filename=stair_design.ifc"); self.end_headers()
        with open(path,"rb") as f: self.wfile.write(f.read())
    def do_OPTIONS(self): self.send_response(200); self._cors(); self.end_headers()
    def _static(self, path):
        """Serve static files from webapp directory."""
        import os as _os, mimetypes as _mt
        safe = path.lstrip('/').split('?')[0] or 'index.html'
        fp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), safe)
        if not _os.path.isfile(fp):
            return self._json({"error":"not found"}, 404)
        self.send_response(200); self._cors()
        ct = _mt.guess_type(fp)[0] or 'application/octet-stream'
        self.send_header("Content-Type", ct)
        if ct.startswith('text/') or ct in ('application/javascript','application/json'):
            self.send_header("Content-Type", ct+"; charset=utf-8")
        self.end_headers()
        with open(fp, 'rb') as f:
            self.wfile.write(f.read())

    def do_GET(self):
        if self.path=="/api/health": return self._json({"status":"ok","version":"0.7.0"})
        if self.path=="/api/geometry": return self._geometry()
        if self.path=="/api/geometry/stream": return self._geometry_stream()
        if self.path=="/api/stair-overlay": return self._stair_overlay()
        if self.path.startswith("/api/"): return self._json({"error":"not found"},404)
        return self._static(self.path)

    def do_POST(self):
        if self.path=="/api/analyze": return self._analyze()
        if self.path=="/api/design": return self._design()
        if self.path=="/api/generate-ifc": return self._gen_ifc()
        self._json({"error":"not found"},404)

    _geom_cache = None  # class-level cache for geometry (cleared on new upload)

    def _stair_overlay(self):
        """Return only the stair overlay mesh elements (for incremental rendering after design)."""
        if not LAST_STAIR_DESIGN:
            return self._json({"elements": []})
        sd = LAST_STAIR_DESIGN
        stair_els = build_stair_mesh_elements(
            sd["flights"], sd["landings"], sd["stairwell"],
            sd["sw_mm"], sd["well_w"])
        return self._json({"elements": stair_els})

    def _geometry(self):
        global LAST_IFC, LAST_IFC_MODEL, LAST_IFC_CONTEXT, LAST_STAIR_DESIGN
        if not LAST_IFC or not Path(LAST_IFC).exists():
            return self._json({"error":"No IFC uploaded yet"}, 400)
        # ── Cache key includes stair design to invalidate when design changes ──
        stair_key = hash(json.dumps(LAST_STAIR_DESIGN, sort_keys=True)) if LAST_STAIR_DESIGN else None
        cached = getattr(Handler, '_geom_cache', None)
        if cached and cached.get('_file') == LAST_IFC and cached.get('_stair_key') == stair_key:
            return self._json(cached)
        try:
            import ifcopenshell.geom as geom
            m = (LAST_IFC_MODEL if LAST_IFC_MODEL else ifcopenshell.open(LAST_IFC))
            settings = geom.settings()
            elements = []

            # Build opening -> wall mapping
            opening_to_wall = {}
            for rel in m.by_type('IfcRelVoidsElement'):
                try:
                    w_gid = rel.RelatingBuildingElement.GlobalId
                    o_gid = rel.RelatedOpeningElement.GlobalId
                    opening_to_wall[o_gid] = w_gid
                except: pass

            # Use by_type() for each target type — avoids iterating all entities
            target_types = ['IfcColumn','IfcBeam','IfcSlab','IfcWall',
                           'IfcStair','IfcStairFlight','IfcRailing','IfcOpeningElement']
            for bt in target_types:
                for e in m.by_type(bt):
                    info = _process_element(e, settings, opening_to_wall)
                    if info:
                        elements.append(info)

            # Clean names: strip trailing :number
            for el in elements:
                el["name"] = re.sub(r':\d+\s*$', '', el["name"])
                if el["type"] == 'IfcBeam' and not el.get('steelProfile'):
                    w, d, _ = el["size"]
                    el["name"] = re.sub(r'\d+x\d+', f'{w:.0f}x{d:.0f}', el["name"])

            ctx = LAST_IFC_CONTEXT if LAST_IFC_CONTEXT else extract_ifc_context(LAST_IFC)
            s = ctx["storeys"][0] if ctx["storeys"] else {"name": "F1"}
            fh = ctx["floor_heights"].get(s["name"], 4500)
            summary = f"梁:{len(ctx['beams'])} 柱:{len(ctx['columns'])} 板:{len(ctx['slabs'])} 墙:{len(ctx['walls'])}"

            # ── Additive overlay: append stair 3D mesh from design ──
            if LAST_STAIR_DESIGN:
                sd = LAST_STAIR_DESIGN
                stair_els = build_stair_mesh_elements(
                    sd["flights"], sd["landings"], sd["stairwell"],
                    sd["sw_mm"], sd["well_w"])
                elements.extend(stair_els)
                stair_info = {"flights": [], "landings": []}
                for f in sd["flights"]:
                    stair_info["flights"].append({
                        "name": f.get("name",""), "steps": f.get("n",0),
                        "riser": f.get("riser",0), "tread": f.get("tread",0),
                        "length": f.get("len",0), "height": f.get("h",0)
                    })
                for l in sd["landings"]:
                    stair_info["landings"].append({
                        "name": l.get("name",""), "l": l.get("l",0),
                        "w": l.get("w",0), "t": l.get("t",0), "el": l.get("el",0)
                    })
                summary += f" | 楼梯: {len(sd['flights'])}跑 {sum(f.get('n',0) for f in sd['flights'])}级"
            else:
                stair_info = None

            result = {"elements": elements, "floorHeight": fh,
                "summary": summary, "stairDesign": stair_info,
                "_file": LAST_IFC, "_stair_key": stair_key}
            Handler._geom_cache = result
            return self._json(result)
        except Exception as e:
            import traceback; traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def _geometry_stream(self):
        """SSE endpoint: pre-scans storey membership, then tessellates and streams floor-by-floor."""
        global LAST_IFC, LAST_IFC_MODEL, LAST_IFC_CONTEXT, LAST_STAIR_DESIGN
        if not LAST_IFC or not Path(LAST_IFC).exists():
            return self._json({"error":"No IFC uploaded yet"}, 400)

        self.send_response(200); self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        stair_key = hash(json.dumps(LAST_STAIR_DESIGN, sort_keys=True)) if LAST_STAIR_DESIGN else None
        cached = getattr(Handler, '_geom_cache', None)

        def _send(data):
            self.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        TYPE_CN = {'IfcColumn':'柱','IfcBeam':'梁','IfcSlab':'板','IfcWall':'墙',
                   'IfcStair':'楼梯','IfcStairFlight':'梯段','IfcRailing':'栏杆','IfcOpeningElement':'洞口'}

        try:
            # ── Cache hit: group & stream ──
            if cached and cached.get('_file') == LAST_IFC and cached.get('_stair_key') == stair_key:
                all_elements = list(cached.get("elements", []))
                ctx = LAST_IFC_CONTEXT if LAST_IFC_CONTEXT else extract_ifc_context(LAST_IFC)
                storeys = sorted(ctx.get("storeys", []), key=lambda s: s.get("elevation", 0))
                if not storeys: storeys = [{"name": "F1", "elevation": 0}]
                groups = {s["name"]: [] for s in storeys}
                for el in all_elements:
                    z = el["pos"][2]; matched = storeys[0]["name"]
                    for s in storeys:
                        if z >= s["elevation"] - 500: matched = s["name"]
                    groups[matched].append(el)
                total = len(all_elements); sent = 0
                for s in storeys:
                    batch = groups.get(s["name"], [])
                    if not batch: continue
                    tc = {}
                    for el in batch: t = el["type"]; tc[t] = tc.get(t, 0) + 1
                    parts = [f"{TYPE_CN.get(t,t)}{c}" for t, c in sorted(tc.items())]
                    _send({"type": "phase", "label": f"{s['name']} ({', '.join(parts)})", "current": sent, "total": total})
                    _send({"type": "elements", "elements": batch})
                    sent += len(batch)
                    time.sleep(0.05)
                _send({"type": "phase", "label": "完成", "current": total, "total": total})
                _send({"type": "done", "summary": cached.get("summary",""),
                       "floorHeight": cached.get("floorHeight", 4500),
                       "stairDesign": cached.get("stairDesign")})
                return

            import ifcopenshell.geom as geom
            m = (LAST_IFC_MODEL if LAST_IFC_MODEL else ifcopenshell.open(LAST_IFC))
            settings = geom.settings()
            ctx = LAST_IFC_CONTEXT if LAST_IFC_CONTEXT else extract_ifc_context(LAST_IFC)
            storeys = sorted(ctx.get("storeys", []), key=lambda s: s.get("elevation", 0))
            if not storeys: storeys = [{"name": "F1", "elevation": 0}]

            # Map GlobalId -> storey name for quick lookup
            name_map = LAST_IFC_NAME_MAP

            # ── Phase 1: Quick pre-scan (Z-only, no tessellation) ──
            target_types = ['IfcColumn','IfcBeam','IfcSlab','IfcWall',
                           'IfcStair','IfcStairFlight','IfcRailing','IfcOpeningElement']
            _send({"type": "phase", "label": "正在分析楼层...", "current": 0, "total": 1})

            # Build entity lists per storey (entity reference only, not tessellated yet)
            storey_entities = {s["name"]: [] for s in storeys}
            for bt in target_types:
                for e in m.by_type(bt):
                    z = _get_entity_z(e)
                    matched = storeys[0]["name"]
                    for s in storeys:
                        if z >= s["elevation"] - 500:
                            matched = s["name"]
                    storey_entities[matched].append(e)

            # Count totals
            total = sum(len(v) for v in storey_entities.values())
            per_storey = {sn: len(elist) for sn, elist in storey_entities.items()}

            _send({"type": "phase", "label": f"共{len(storeys)}层 {total}个构件", "current": 0, "total": total})

            # ── Phase 2: Tessellate & stream storey-by-storey ──
            opening_to_wall = {}
            for rel in m.by_type('IfcRelVoidsElement'):
                try:
                    opening_to_wall[rel.RelatedOpeningElement.GlobalId] = rel.RelatingBuildingElement.GlobalId
                except: pass

            all_elements = []
            sent = 0
            s0 = storeys[0]["name"] if storeys else "F1"
            fh = ctx["floor_heights"].get(s0, 4500)
            summary = f"梁:{len(ctx['beams'])} 柱:{len(ctx['columns'])} 板:{len(ctx['slabs'])} 墙:{len(ctx['walls'])}"

            for s in storeys:
                sn = s["name"]
                entities = storey_entities.get(sn, [])
                if not entities: continue

                # Build type summary for label
                tc_est = {}
                for e in entities:
                    for bt in target_types:
                        if e.is_a(bt):
                            tc_est[bt] = tc_est.get(bt, 0) + 1
                            break
                parts = [f"{TYPE_CN.get(t,t)}{c}" for t, c in sorted(tc_est.items())]
                _send({"type": "phase", "label": f"{sn} ({', '.join(parts)}) 解析中...", "current": sent, "total": total})

                batch = []
                for e in entities:
                    info = _process_element(e, settings, opening_to_wall)
                    if info:
                        # Fix garbled name
                        if name_map and info["global_id"] in name_map:
                            info["name"] = name_map[info["global_id"]]
                        # Clean name
                        info["name"] = re.sub(r':\d+\s*$', '', info["name"])
                        if info["type"] == 'IfcBeam' and not info.get('steelProfile'):
                            w2, d2, _ = info["size"]
                            info["name"] = re.sub(r'\d+x\d+', f'{w2:.0f}x{d2:.0f}', info["name"])
                        batch.append(info)

                if batch:
                    all_elements.extend(batch)
                    _send({"type": "elements", "elements": batch})
                sent += len(entities)
                time.sleep(0.05)
                if self.wfile.closed: return

            # Stair overlay — send as elements batch for immediate rendering
            stair_info = None
            if LAST_STAIR_DESIGN:
                sd = LAST_STAIR_DESIGN
                stair_els = build_stair_mesh_elements(
                    sd["flights"], sd["landings"], sd["stairwell"],
                    sd["sw_mm"], sd["well_w"])
                all_elements.extend(stair_els)
                # Send stair overlay elements to frontend
                _send({"type": "phase", "label": f"楼梯 ({len(stair_els)} 构件)", "current": total, "total": total})
                _send({"type": "elements", "elements": stair_els})
                stair_info = {"flights": [], "landings": []}
                for f in sd["flights"]:
                    stair_info["flights"].append({
                        "name": f.get("name",""), "steps": f.get("n",0),
                        "riser": f.get("riser",0), "tread": f.get("tread",0),
                        "length": f.get("len",0), "height": f.get("h",0)
                    })
                for l in sd["landings"]:
                    stair_info["landings"].append({
                        "name": l.get("name",""), "l": l.get("l",0),
                        "w": l.get("w",0), "t": l.get("t",0), "el": l.get("el",0)
                    })
                summary += f" | 楼梯: {len(sd['flights'])}跑 {sum(f.get('n',0) for f in sd['flights'])}级"

            _send({"type": "phase", "label": "完成", "current": total, "total": total})
            _send({"type": "done", "summary": summary, "floorHeight": fh,
                   "stairDesign": stair_info})

            # Cache result
            result = {"elements": all_elements, "floorHeight": fh,
                "summary": summary, "stairDesign": stair_info,
                "_file": LAST_IFC, "_stair_key": stair_key}
            Handler._geom_cache = result

        except Exception as e:
            import traceback; traceback.print_exc()
            try:
                _send({"type": "error", "error": str(e)})
            except: pass

    def _read_json(self):
        """Read JSON body with proper encoding detection."""
        cl = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(cl)
        for enc in ['utf-8', 'gbk', 'latin-1']:
            try:
                body = raw.decode(enc)
                return json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return json.loads(raw.decode('utf-8', errors='replace'))

    def _parse_multipart(self):
        ct = self.headers.get("Content-Type","")
        cl = int(self.headers.get("Content-Length",0))
        body = self.rfile.read(cl)
        boundary = ct.split("boundary=")[1].strip()
        if boundary.startswith('"'): boundary = boundary[1:-1]
        filename = "uploaded.ifc"; data = b""
        for part in body.split(("--"+boundary).encode()):
            if b'Content-Disposition' not in part: continue
            hdr_end = part.find(b'\r\n\r\n')
            if hdr_end < 0: continue
            # Decode headers: try UTF-8 first (for CJK filenames), fallback to latin-1
            hdr_bytes = part[:hdr_end]
            try:
                hdrs = hdr_bytes.decode('utf-8')
            except UnicodeDecodeError:
                hdrs = hdr_bytes.decode('latin-1')
            # RFC 5987 filename* (UTF-8 percent-encoded) takes precedence
            m_star = re.search(r"filename\*=(?:UTF-8''|utf-8'')([^;\s]+)", hdrs)
            if m_star:
                from urllib.parse import unquote
                filename = unquote(m_star.group(1))
            else:
                m = re.search(r'filename="([^"]*)"', hdrs)
                if m: filename = m.group(1)
            if 'name="file"' in hdrs:
                data = part[hdr_end+4:]
                if data.endswith(b'\r\n'): data = data[:-2]
                break
        return filename, data

    def _analyze(self):
        global LAST_IFC, LAST_IFC_MODEL, LAST_IFC_CONTEXT, LAST_IFC_NAME_MAP, LAST_STAIR_DESIGN
        filename, data = self._parse_multipart()
        if not data: return self._json({"error":"empty file"},400)
        with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as tmp:
            tmp.write(data); tp = tmp.name
        if LAST_IFC and Path(LAST_IFC).exists(): Path(LAST_IFC).unlink(missing_ok=True)
        LAST_IFC = tp
        LAST_STAIR_DESIGN = None
        Handler._geom_cache = None
        try:
            LAST_IFC_MODEL = ifcopenshell.open(tp)
            LAST_IFC_NAME_MAP = _build_name_map(tp)  # decode CJK entity names from raw file
            ctx = extract_ifc_context(tp)
            _fix_storey_names(ctx, LAST_IFC_NAME_MAP)  # fix garbled storey names
            LAST_IFC_CONTEXT = ctx  # cache context for reuse
            s = ctx["storeys"][0] if ctx["storeys"] else {"name":"F1","elevation":0}
            fh = ctx["floor_heights"].get(s["name"],4500)
            bt = ctx["beam_elevations"][-1] if ctx["beam_elevations"] else fh
            return self._json({
                "file":filename,
                "summary":f"梁:{len(ctx['beams'])} 柱:{len(ctx['columns'])} 板:{len(ctx['slabs'])} 墙:{len(ctx['walls'])}",
                "floorHeight":fh,"beamTop":bt,"beamDepth":400,
                "columnGridX":" / ".join(str(x) for x in ctx["col_x"]) if ctx["col_x"] else "未检测",
                "columnGridY":" / ".join(str(y) for y in ctx["col_y"]) if ctx["col_y"] else "未检测",
                "candidates":[{"name":c["name"],"l":c["length_mm"],"w":c["width_mm"]} for c in ctx["candidates"]]
            })
        except Exception as e:
            Path(tp).unlink(missing_ok=True); LAST_IFC = None
            return self._json({"error":str(e)},500)

    def _design(self):
        body = self._read_json()
        props = body.get("stairwell",{})
        sw = {"length_mm":props.get("length_mm",6000),"width_mm":props.get("width_mm",2700),
              "floor_height_mm":props.get("floor_height_mm",4500),
              "beam_position_top_mm":props.get("beam_top",4400),"beam_depth_mm":props.get("beam_depth",400)}
        key = body.get("api_key",""); model = body.get("model","deepseek-chat")
        ep = body.get("endpoint","https://api.deepseek.com")
        d = algo_design(sw); src = "📐 本地算法"; debug = ""
        if key:
            r = call_ai(sw, key, model, ep)
            if r["ok"]: d = r["design"]; src = r["source"]
            else: src=f"⚠️ AI失败: {r['err']}"; debug=r.get("debug","") or r.get("err","")
        errs = check_rules(d, sw)
        flights=[{"name":f["name"],"n":f["tread_count"],"riser":f["riser_height_mm"],"tread":f["tread_depth_mm"],"len":f["length_mm"],"h":f["height_mm"]} for f in d["flights"]]
        landings=[{"name":l["name"],"l":l["length_mm"],"w":l["width_mm"],"t":l["thickness_mm"],"el":l["elevation_mm"]} for l in d["landings"]]
        railings=", ".join(f"{r['name']}({r['height_mm']}mm)" for r in d["railings"])
        result={"source":src,"design":{"flights":flights,"landings":landings,"railings":railings},"errors":errs}
        if debug: result["debug"]=debug[:800]
        return self._json(result)

    def _gen_ifc(self):
        global LAST_IFC, LAST_STAIR_DESIGN
        if not LAST_IFC or not Path(LAST_IFC).exists():
            return self._json({"error":"请先上传IFC文件"},400)
        body = self._read_json()
        flights = body.get("flights",[]); landings = body.get("landings",[])
        sw = body.get("stairwell",{})
        sw_mm = body.get("width_mm",1200)
        try:
            well_w = body.get("stairwell_width_mm", 2700)
            # Inject storey elevation from analysis context
            if LAST_IFC_CONTEXT:
                ss = LAST_IFC_CONTEXT.get("storeys", [])
                sw["storey_elevation"] = ss[0]["elevation"] if ss else 0
            else:
                sw.setdefault("storey_elevation", 0)
            # Generate IFC file (for download)
            tp = generate_stair_ifc(LAST_IFC, flights, landings, sw, sw_mm, well_w)
            self._file(tp)
            Path(tp).unlink(missing_ok=True)
            # Store design for additive 3D overlay (no IFC merge needed for visualization)
            LAST_STAIR_DESIGN = {
                "flights": flights, "landings": landings,
                "stairwell": sw, "sw_mm": sw_mm, "well_w": well_w
            }
            Handler._geom_cache = None  # invalidate geometry cache
        except Exception as e:
            import traceback; traceback.print_exc()
            self._json({"error":str(e)},500)

if __name__ == "__main__":
    print(f"StructureAI v0.7.0 → http://localhost:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
