"""
AI Stair Designer v0.2 — Blender/Bonsai Addon
Workflow: Import IFC → Specify stairwell → AI designs stair → Generate 3D + IFC
"""

bl_info = {
    "name": "AI Stair Designer",
    "author": "AI Structural Engineer",
    "version": (0, 2, 0),
    "blender": (5, 1, 0),
    "location": "3D Viewport > Sidebar > AI Stair",
    "description": "Import IFC, auto-detect stairwell, AI-powered stair design",
    "category": "BIM",
}

import bpy
import bmesh
import ifcopenshell
import ifcopenshell.api
from ifcopenshell.api import run
from pathlib import Path
import json
import urllib.request
import urllib.error

# ============================================================
#  Rule Checker
# ============================================================
class StairRuleChecker:
    RISER_MAX = 175; TREAD_MIN = 260; HEADROOM_MIN = 2200
    WIDTH_MIN = 1100; LANDING_MIN = 1200

    @classmethod
    def check(cls, stair_data, stairwell):
        errors = []
        w = stair_data.get("width_mm", 0)
        if w < cls.WIDTH_MIN: errors.append(("❌", f"楼梯宽度 {w}mm < {cls.WIDTH_MIN}mm"))
        for f in stair_data.get("flights", []):
            r, t, fid = f.get("riser_height_mm", 0), f.get("tread_depth_mm", 0), f.get("id", "?")
            if r > cls.RISER_MAX: errors.append(("❌", f"[{fid}] 踏步高 {r}mm > {cls.RISER_MAX}mm"))
            if t < cls.TREAD_MIN: errors.append(("❌", f"[{fid}] 踏步深 {t}mm < {cls.TREAD_MIN}mm"))
        for ld in stair_data.get("landings", []):
            d, lid = ld.get("length_mm", 0), ld.get("id", "?")
            if d < cls.LANDING_MIN: errors.append(("❌", f"[{lid}] 平台深 {d}mm < {cls.LANDING_MIN}mm"))
        bb = stairwell.get("beam_position_top_mm", stairwell["floor_height_mm"]) - stairwell.get("beam_depth_mm", 400)
        for ld in stair_data.get("landings", []):
            if ld.get("name") == "楼层平台": continue
            hr = bb - ld.get("elevation_mm", 0)
            if hr < cls.HEADROOM_MIN: errors.append(("❌", f"净高 {hr}mm < {cls.HEADROOM_MIN}mm (梁底={bb})"))
        return errors


# ============================================================
#  Real AI Design Engine (DeepSeek API)
# ============================================================

STAIR_PROMPT_TEMPLATE = """你是建筑结构工程师。根据楼梯间参数和GB 50010规范设计一部双跑楼梯。

## 规范要求
- 踏步高度 ≤ 175mm, 踏步深度 ≥ 260mm
- 平台深度 ≥ 1200mm, 楼梯净宽 ≥ 1100mm
- 梯段下净高 ≥ 2200mm
- 扶手高度 900mm

## 楼梯间参数
{stairwell_json}

## 输出要求
严格输出JSON（不要markdown标记，不要解释）：
{{
  "type": "double_run",
  "width_mm": 数字,
  "flights": [
    {{"id": "F1", "name": "第一跑", "tread_count": 数字, "riser_height_mm": 数字, "tread_depth_mm": 数字, "start_at_bottom": true, "length_mm": 数字, "height_mm": 数字}},
    {{"id": "F2", "name": "第二跑", "tread_count": 数字, "riser_height_mm": 数字, "tread_depth_mm": 数字, "start_at_bottom": false, "length_mm": 数字, "height_mm": 数字}}
  ],
  "landings": [
    {{"id": "L1", "name": "中间平台", "length_mm": 数字, "width_mm": 数字, "thickness_mm": 150, "elevation_mm": 数字}},
    {{"id": "L2", "name": "楼层平台", "length_mm": 数字, "width_mm": 数字, "thickness_mm": 150, "elevation_mm": 数字}}
  ],
  "railings": [
    {{"id": "R1", "name": "左侧栏杆", "attached_to": "F1", "height_mm": 900}},
    {{"id": "R2", "name": "右侧栏杆", "attached_to": "F2", "height_mm": 900}}
  ]
}}"""


def call_deepseek_api(prompt: str, api_config: dict) -> tuple:
    """Call DeepSeek API and return (parsed_dict, raw_content, source_label)."""
    endpoint = api_config.get("endpoint", "https://api.deepseek.com")
    api_key = api_config.get("api_key", "")
    model = api_config.get("model", "deepseek-chat")

    if not api_key:
        print("  ⚠️  No API key configured")
        return (None, "", "未配置API Key")

    url = f"{endpoint}/v1/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a structural engineer. Output only valid JSON, no markdown, no explanation."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_content = data["choices"][0]["message"]["content"].strip()

            # Remove markdown code fences if present
            clean = raw_content
            if clean.startswith("```"):
                lines = clean.split("\n")
                clean = "\n".join(lines[1:]) if len(lines) > 1 else clean
            if clean.endswith("```"):
                clean = clean[:-3].strip()
            if "```" in clean:
                clean = clean.replace("```json", "").replace("```", "").strip()

            parsed = json.loads(clean)
            source = f"DeepSeek AI ({model})"
            print(f"  ✅ AI response parsed, {len(raw_content)} chars")
            return (parsed, raw_content, source)

    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else ""
        print(f"  ❌ API HTTP {e.code}: {err_body[:200]}")
        return (None, err_body[:500], f"API错误 HTTP {e.code}")
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  ❌ Parse error: {e}")
        return (None, raw_content[:500] if 'raw_content' in dir() else str(e), f"解析失败: {e}")
    except Exception as e:
        print(f"  ❌ API call failed: {e}")
        return (None, str(e), f"调用失败: {e}")


def ai_generate_stair_design(stairwell, api_config=None):
    """Generate stair design using DeepSeek AI, with algorithmic fallback.
    Returns (design_dict, source_label, raw_response_text)."""

    # Try real AI first
    if api_config and api_config.get("api_key"):
        print("  🤖 Calling DeepSeek AI...")
        prompt = STAIR_PROMPT_TEMPLATE.format(stairwell_json=json.dumps(stairwell, indent=2, ensure_ascii=False))
        result, raw, source = call_deepseek_api(prompt, api_config)

        if result and "flights" in result:
            print(f"  ✅ AI design: {len(result.get('flights',[]))} flights")
            for f in result.get("flights", []):
                f.setdefault("tread_depth_mm", 280)
                f.setdefault("riser_height_mm", 150)
                f.setdefault("length_mm", f.get("tread_count", 10) * f.get("tread_depth_mm", 280))
                f.setdefault("height_mm", f.get("tread_count", 10) * f.get("riser_height_mm", 150))
                f.setdefault("start_at_bottom", f.get("id") == "F1")
            for ld in result.get("landings", []):
                ld.setdefault("thickness_mm", 150)
                ld.setdefault("width_mm", stairwell.get("width_mm", 2700))
            result.setdefault("type", "double_run")
            result.setdefault("width_mm", stairwell.get("width_mm", 1200) - 300)
            return (result, source, raw[:2000] if raw else "")
        else:
            # AI failed — fall through to algorithmic fallback
            print(f"  ⚠️  AI failed ({source}), using algorithmic fallback")
            # Will continue to algorithmic section below

    # Algorithmic fallback
    L, W, H = stairwell["length_mm"], stairwell["width_mm"], stairwell["floor_height_mm"]
    total_risers = round(H / 150)
    riser_h = H / total_risers; rpf = total_risers // 2
    td, sw = 280, min(W - 300, 1200); sw = max(sw, 1100)
    fl = rpf * td; ll = max(round((L - fl) / 2), 1200)
    print("  📐 Algorithmic fallback")
    design = {
        "type": "double_run", "width_mm": sw,
        "flights": [
            {"id": "F1", "name": "第一跑", "tread_count": rpf, "riser_height_mm": round(riser_h),
             "tread_depth_mm": td, "start_at_bottom": True, "length_mm": fl, "height_mm": round(riser_h * rpf)},
            {"id": "F2", "name": "第二跑", "tread_count": total_risers - rpf, "riser_height_mm": round(riser_h),
             "tread_depth_mm": td, "start_at_bottom": False,
             "length_mm": (total_risers - rpf) * td, "height_mm": round(riser_h * (total_risers - rpf))},
        ],
        "landings": [
            {"id": "L1", "name": "中间平台", "length_mm": ll, "width_mm": W, "thickness_mm": 150, "elevation_mm": round(riser_h * rpf)},
            {"id": "L2", "name": "楼层平台", "length_mm": ll, "width_mm": W, "thickness_mm": 150, "elevation_mm": H},
        ],
        "railings": [
            {"id": "R1", "name": "左侧栏杆", "attached_to": "F1", "height_mm": 900},
            {"id": "R2", "name": "右侧栏杆", "attached_to": "F2", "height_mm": 900},
        ],
    }
    # If we got here from AI failure, include the failure reason
    source_label = "📐 本地算法"
    if api_config and api_config.get("api_key"):
        source_label = f"⚠️ AI调用失败,降级为本地算法"
    return (design, source_label, "")


# ============================================================
#  IFC Builder (creates IFC + visible 3D geometry)
# ============================================================
def build_stair_in_blender(stair_data, stairwell, project_name="AI Stair"):
    model = ifcopenshell.file()
    project = run("root.create_entity", model, ifc_class="IfcProject", name=project_name)
    run("unit.assign_unit", model)
    site = run("root.create_entity", model, ifc_class="IfcSite", name="Site")
    building = run("root.create_entity", model, ifc_class="IfcBuilding", name="Building")
    storey = run("root.create_entity", model, ifc_class="IfcBuildingStorey", name="Level 1")
    run("aggregate.assign_object", model, products=[site], relating_object=project)
    run("aggregate.assign_object", model, products=[building], relating_object=site)
    run("aggregate.assign_object", model, products=[storey], relating_object=building)

    stair = run("root.create_entity", model, ifc_class="IfcStair", name="楼梯")
    run("spatial.assign_container", model, products=[stair], relating_structure=storey)
    pset = run("pset.add_pset", model, product=stair, name="AI_Stair_Design")
    run("pset.edit_pset", model, pset=pset, properties={
        "Type": stair_data["type"], "Width_mm": stair_data["width_mm"],
        "StairwellLength_mm": stairwell["length_mm"], "StairwellWidth_mm": stairwell["width_mm"],
        "FloorHeight_mm": stairwell["floor_height_mm"],
    })

    created = []
    for f in stair_data.get("flights", []):
        fl = run("root.create_entity", model, ifc_class="IfcStairFlight", name=f["name"])
        run("aggregate.assign_object", model, products=[fl], relating_object=stair)
        fps = run("pset.add_pset", model, product=fl, name="Parameters")
        run("pset.edit_pset", model, pset=fps, properties={
            "Treads": f["tread_count"], "Riser_mm": f["riser_height_mm"], "Tread_mm": f["tread_depth_mm"]})
        created.append(f"梯段: {f['name']} ({fl.GlobalId[:12]}...)")

    for ld in stair_data.get("landings", []):
        sl = run("root.create_entity", model, ifc_class="IfcSlab", name=ld["name"])
        run("aggregate.assign_object", model, products=[sl], relating_object=stair)
        run("spatial.assign_container", model, products=[sl], relating_structure=storey)
        lps = run("pset.add_pset", model, product=sl, name="Parameters")
        run("pset.edit_pset", model, pset=lps, properties={
            "Length_mm": ld["length_mm"], "Width_mm": ld["width_mm"],
            "Thickness_mm": ld["thickness_mm"], "Elevation_mm": ld["elevation_mm"]})
        created.append(f"平台: {ld['name']} ({sl.GlobalId[:12]}...)")

    for rl in stair_data.get("railings", []):
        rail = run("root.create_entity", model, ifc_class="IfcRailing", name=rl["name"])
        run("aggregate.assign_object", model, products=[rail], relating_object=stair)
        run("pset.edit_pset", model,
            pset=run("pset.add_pset", model, product=rail, name="Parameters"),
            properties={"Height_mm": rl["height_mm"]})
        created.append(f"栏杆: {rl['name']} ({rail.GlobalId[:12]}...)")

    output = Path.home() / "Desktop" / f"{project_name.replace(' ', '_')}.ifc"
    model.write(str(output))

    create_visible_stair_geometry(stair_data, stairwell)
    return {"stair_id": stair.GlobalId, "elements": created, "output_path": str(output)}


# ============================================================
#  Visible 3D Geometry
# ============================================================
def _make_box(name, location, scale, material, coll):
    bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1.0)
    mesh = bpy.data.meshes.new(name + "_mesh"); bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location; obj.scale = scale
    obj.data.materials.append(material); coll.objects.link(obj)
    return obj

def _make_cone(name, location, height, radius, material, coll):
    bm = bmesh.new(); bmesh.ops.create_cone(bm, segments=12, radius1=radius, radius2=radius, depth=1.0)
    mesh = bpy.data.meshes.new(name + "_mesh"); bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location; obj.scale = (1, 1, height)
    obj.data.materials.append(material); coll.objects.link(obj)
    return obj

def create_visible_stair_geometry(stair_data, stairwell):
    for obj in list(bpy.data.objects):
        if obj.name.startswith("AI_Stair_"): bpy.data.objects.remove(obj, do_unlink=True)

    coll = bpy.data.collections.get("AI_Stair") or bpy.data.collections.new("AI_Stair")
    if "AI_Stair" not in [c.name for c in bpy.context.scene.collection.children]:
        bpy.context.scene.collection.children.link(coll)

    sw, L = stair_data.get("width_mm", 1200) / 1000, stairwell["length_mm"] / 1000
    W, H = stairwell["width_mm"] / 1000, stairwell["floor_height_mm"] / 1000

    mat = lambda name, color: (bpy.data.materials.get(name) or bpy.data.materials.new(name))
    mat_c = mat("AI_Concrete", None); mat_c.diffuse_color = (0.7, 0.68, 0.62, 1.0)
    mat_s = mat("AI_Steel", None); mat_s.diffuse_color = (0.3, 0.35, 0.4, 1.0)
    mat_w = mat("AI_Stairwell", None); mat_w.diffuse_color = (0.2, 0.2, 0.25, 0.15)

    # Stairwell outline
    _make_box("AI_Stair_well", (L/2, W/2, H/2), (L/2, W/2, H/2), mat_w, coll).display_type = 'WIRE'

    # Landings
    for ld in stair_data.get("landings", []):
        ll = ld.get("length_mm", 1200) / 1000; lw = ld.get("width_mm", 2400) / 1000
        th = ld.get("thickness_mm", 150) / 1000; el = ld.get("elevation_mm", 0) / 1000
        _make_box(f"AI_Stair_{ld['name']}", (L - ll/2, W/2, el + th/2), (ll/2, lw/2, th/2), mat_c, coll)

    # Steps
    for fi, flight in enumerate(stair_data.get("flights", [])):
        nt = flight.get("tread_count", 10); td = flight.get("tread_depth_mm", 280) / 1000
        rh = flight.get("riser_height_mm", 150) / 1000
        if fi == 0: se, xs, xd = 0, 0, 1
        else: se, xs, xd = H - nt * rh, L, -1
        for s in range(nt):
            _make_box(f"AI_Stair_F{fi+1}_step{s+1}",
                      (xs + xd * s * td + xd * td / 2, W/2, se + s * rh + rh/2),
                      (td/2, sw/2, rh/2), mat_c, coll)

    # Railings
    rh_r = stair_data.get("railings", [{}])[0].get("height_mm", 900) / 1000
    for fi, flight in enumerate(stair_data.get("flights", [])):
        nt = flight.get("tread_count", 10); td = flight.get("tread_depth_mm", 280) / 1000
        rh = flight.get("riser_height_mm", 150) / 1000
        if fi == 0: se, xs, xd = 0, 0, 1
        else: se, xs, xd = H - nt * rh, L, -1
        fex = xs + xd * nt * td
        for side, yo in [("L", -sw/2 + 0.06), ("R", sw/2 - 0.06)]:
            yp = W/2 + yo
            for si in sorted(set([0] + list(range(3, nt, 3)) + [nt - 1])):
                _make_cone(f"AI_Stair_F{fi+1}_{side}_post{si}",
                           (xs + xd * si * td + xd * td/2, yp, se + si * rh + rh + rh_r/2),
                           rh_r, 0.04, mat_s, coll)
            rsx, rex = xs + xd * td/2, fex - xd * td/2
            _make_box(f"AI_Stair_F{fi+1}_{side}_rail",
                      ((rsx + rex)/2, yp, (se + (nt-1)*rh)/2 + rh + rh_r),
                      (abs(rex - rsx)/2, 0.015, 0.015), mat_s, coll)
    print("  ✓ Created visible 3D geometry")


# ============================================================
#  IFC Import + Context Extraction
# ============================================================
def import_ifc_and_visualize(filepath):
    """Load IFC, create visible geometry for structural elements, extract context."""
    model = ifcopenshell.open(filepath)

    # Clean previous imports
    for obj in list(bpy.data.objects):
        if obj.name.startswith("IFC_"): bpy.data.objects.remove(obj, do_unlink=True)
    coll = bpy.data.collections.get("IFC_Import") or bpy.data.collections.new("IFC_Import")
    if "IFC_Import" not in [c.name for c in bpy.context.scene.collection.children]:
        bpy.context.scene.collection.children.link(coll)

    mat_beam = bpy.data.materials.get("IFC_Beam") or bpy.data.materials.new("IFC_Beam")
    mat_beam.diffuse_color = (0.2, 0.5, 0.8, 0.6)
    mat_col = bpy.data.materials.get("IFC_Column") or bpy.data.materials.new("IFC_Column")
    mat_col.diffuse_color = (0.8, 0.3, 0.2, 0.6)
    mat_slab = bpy.data.materials.get("IFC_Slab") or bpy.data.materials.new("IFC_Slab")
    mat_slab.diffuse_color = (0.6, 0.6, 0.6, 0.5)
    mat_wall = bpy.data.materials.get("IFC_Wall") or bpy.data.materials.new("IFC_Wall")
    mat_wall.diffuse_color = (0.85, 0.85, 0.7, 0.5)

    context = {"storeys": [], "beams": [], "columns": [], "slabs": [], "walls": [],
               "floor_heights": {}, "column_positions": []}

    # Storeys
    for s in model.by_type("IfcBuildingStorey"):
        elev = getattr(s, "Elevation", None)
        eh = elev.wrappedValue if hasattr(elev, 'wrappedValue') else (float(elev) if elev else 0)
        context["storeys"].append({"global_id": s.GlobalId, "name": s.Name or "?", "elevation": eh})

    # Beams
    for b in model.by_type("IfcBeam"):
        info = _extract_element_info(b, model)
        context["beams"].append(info)
        s = info.get("size", (300, 300, 6000))
        p = info.get("position", (0, 0, 0))
        _make_box(f"IFC_Beam_{b.Name or b.GlobalId[:8]}",
                  (p[0]/1000, p[1]/1000, p[2]/1000),
                  (s[2]/2000, s[0]/2000, s[1]/2000), mat_beam, coll).display_type = 'WIRE'

    # Columns
    for c in model.by_type("IfcColumn"):
        info = _extract_element_info(c, model)
        context["columns"].append(info)
        s = info.get("size", (400, 400, 4000))
        p = info.get("position", (0, 0, 0))
        _make_box(f"IFC_Col_{c.Name or c.GlobalId[:8]}",
                  (p[0]/1000, p[1]/1000, p[2]/1000 + s[2]/2000),
                  (s[0]/2000, s[1]/2000, s[2]/2000), mat_col, coll)

    # Slabs
    for s in model.by_type("IfcSlab"):
        info = _extract_element_info(s, model)
        context["slabs"].append(info)
        sz = info.get("size", (6000, 6000, 150))
        p = info.get("position", (0, 0, 0))
        _make_box(f"IFC_Slab_{s.Name or s.GlobalId[:8]}",
                  (p[0]/1000, p[1]/1000, p[2]/1000),
                  (sz[0]/2000, sz[1]/2000, sz[2]/2000), mat_slab, coll)

    # Walls
    for w in model.by_type("IfcWall"):
        info = _extract_element_info(w, model)
        context["walls"].append(info)
        sz = info.get("size", (200, 6000, 4000))
        p = info.get("position", (0, 0, 0))
        _make_box(f"IFC_Wall_{w.Name or w.GlobalId[:8]}",
                  (p[0]/1000, p[1]/1000, p[2]/1000 + sz[2]/2000),
                  (sz[0]/2000, sz[1]/2000, sz[2]/2000), mat_wall, coll)

    # Auto-detect floor heights from storey elevations
    storeys_sorted = sorted(context["storeys"], key=lambda s: s["elevation"])
    for i, s in enumerate(storeys_sorted):
        if i + 1 < len(storeys_sorted):
            h = storeys_sorted[i+1]["elevation"] - s["elevation"]
        else:
            h = 4500  # default
        context["floor_heights"][s["name"]] = round(h)

    # Detect column grid positions
    col_x = sorted(set(round(c.get("position", (0,0,0))[0]/100)*100 for c in context["columns"]))
    col_y = sorted(set(round(c.get("position", (0,0,0))[1]/100)*100 for c in context["columns"]))
    context["column_grid"] = {"x": col_x, "y": col_y}

    # Detect beam elevations around potential stairwells
    beam_elevs = set()
    for b in context["beams"]:
        p = b.get("position", (0,0,0))
        beam_elevs.add(round(p[2]/100)*100)
    context["beam_elevations"] = sorted(beam_elevs)

    print(f"  Imported: {len(context['beams'])} beams, {len(context['columns'])} columns, "
          f"{len(context['slabs'])} slabs, {len(context['walls'])} walls")
    storey_info = [(s['name'], context['floor_heights'].get(s['name'], 0)) for s in storeys_sorted]
    print(f"  Storeys: {storey_info}")
    print(f"  Column grid X: {col_x}, Y: {col_y}")

    return context


def _extract_element_info(entity, model):
    """Extract position and size from IFC element properties."""
    info = {"global_id": entity.GlobalId, "name": entity.Name, "type": entity.is_a(),
            "position": (0, 0, 0), "size": (200, 200, 3000)}

    # Try to get dimensions from property sets
    for rel in getattr(entity, "IsDefinedBy", []) or []:
        if not rel.is_a("IfcRelDefinesByProperties"): continue
        pset = rel.RelatingPropertyDefinition
        if not pset: continue
        props = {}
        for prop in getattr(pset, "HasProperties", []) or []:
            if hasattr(prop, "NominalValue") and prop.NominalValue:
                props[prop.Name] = prop.NominalValue.wrappedValue if hasattr(prop.NominalValue, 'wrappedValue') else prop.NominalValue

        # Common naming patterns for dimensions
        w = props.get("Width") or props.get("width") or props.get("b") or 200
        d = props.get("Depth") or props.get("depth") or props.get("h") or 200
        l = props.get("Length") or props.get("length") or props.get("L") or 3000
        info["size"] = (float(w), float(d), float(l))

    # Try to get position from ObjectPlacement
    op = getattr(entity, "ObjectPlacement", None)
    if op:
        rp = getattr(op, "RelativePlacement", None)
        if rp and hasattr(rp, "Location"):
            lc = rp.Location
            coords = lc.Coordinates if hasattr(lc, 'Coordinates') else lc
            try:
                info["position"] = (float(coords[0]), float(coords[1]), float(coords[2]))
            except (TypeError, IndexError):
                pass

    # Container storey for elevation
    container = getattr(entity, "ContainedInStructure", None)
    if container and len(container) > 0:
        storey = container[0].RelatingStructure
        elev = getattr(storey, "Elevation", None)
        if elev is not None:
            eh = elev.wrappedValue if hasattr(elev, 'wrappedValue') else float(elev)
            p = info["position"]
            info["position"] = (p[0], p[1], float(eh))

    return info


# ============================================================
#  Auto-detect stairwell voids from column grid
# ============================================================
def detect_stairwell_candidates(context):
    """Find rectangular gaps in column grid that could be stairwells."""
    candidates = []
    grid_x = context.get("column_grid", {}).get("x", [])
    grid_y = context.get("column_grid", {}).get("y", [])

    if len(grid_x) < 2 or len(grid_y) < 2:
        # Can't detect from grid, return whole bounding box
        all_x = [c.get("position", (0,0,0))[0] for c in context["columns"]]
        all_y = [c.get("position", (0,0,0))[1] for c in context["columns"]]
        if all_x and all_y:
            candidates.append({
                "name": "全区域",
                "x1": min(all_x), "x2": max(all_x),
                "y1": min(all_y), "y2": max(all_y),
                "length_mm": max(all_x) - min(all_x),
                "width_mm": max(all_y) - min(all_y),
            })
        return candidates

    # Find gaps between adjacent columns > 2m (potential stairwell)
    for i in range(len(grid_x) - 1):
        gap_x = grid_x[i+1] - grid_x[i]
        if gap_x > 2000:  # potential stairwell span
            for j in range(len(grid_y) - 1):
                gap_y = grid_y[j+1] - grid_y[j]
                if gap_y > 2000:
                    candidates.append({
                        "name": f"区间 X{grid_x[i]}-{grid_x[i+1]} Y{grid_y[j]}-{grid_y[j+1]}",
                        "x1": grid_x[i], "x2": grid_x[i+1],
                        "y1": grid_y[j], "y2": grid_y[j+1],
                        "length_mm": gap_x, "width_mm": gap_y,
                    })

    if not candidates:
        candidates.append({
            "name": "全区域",
            "x1": min(grid_x), "x2": max(grid_x),
            "y1": min(grid_y), "y2": max(grid_y),
            "length_mm": max(grid_x) - min(grid_x),
            "width_mm": max(grid_y) - min(grid_y),
        })
    return candidates


# ============================================================
#  Operators
# ============================================================
class AISTAIR_OT_import_ifc(bpy.types.Operator):
    bl_idname = "aistair.import_ifc"
    bl_label = "导入 IFC 模型"
    bl_description = "导入IFC文件并自动分析结构模型"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.ifc", options={'HIDDEN'})

    def execute(self, context):
        props = context.scene.ai_stair_props
        props.ifc_path = self.filepath
        try:
            ctx = import_ifc_and_visualize(self.filepath)
            props.ifc_loaded = True
            props.ifc_summary = (f"梁:{len(ctx['beams'])} 柱:{len(ctx['columns'])} "
                                 f"板:{len(ctx['slabs'])} 墙:{len(ctx['walls'])}")

            # Store context for later use
            for s in ctx["storeys"]:
                item = props.storey_list.add()
                item.name = s["name"]
                item.elevation = s["elevation"]
            if ctx["storeys"]: props.storey_index = 0

            # Auto-detect stairwell candidates
            candidates = detect_stairwell_candidates(ctx)
            props.candidate_count = len(candidates)
            if candidates:
                c = candidates[0]
                props.stairwell_length = c["length_mm"]
                props.stairwell_width = c["width_mm"]
                props.candidate_info = "\n".join(f"  □ {c['name']}: {c['length_mm']}×{c['width_mm']}mm" for c in candidates[:8])

            # Auto-set floor height and beam info
            if ctx["storeys"]:
                storey_names = list(ctx["floor_heights"].keys())
                if storey_names:
                    h = ctx["floor_heights"].get(storey_names[0], 4500)
                    props.floor_height = h
                    props.beam_top = h - 50
            if ctx["beam_elevations"]:
                props.beam_top = ctx["beam_elevations"][-1]

            self.report({'INFO'}, f"IFC loaded: {props.ifc_summary}")
        except Exception as e:
            props.ifc_loaded = False
            self.report({'ERROR'}, f"导入失败: {e}")

        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class AISTAIR_OT_generate(bpy.types.Operator):
    bl_idname = "aistair.generate"
    bl_label = "AI 设计楼梯"
    bl_description = "根据楼梯间参数自动设计楼梯并生成IFC模型"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.ai_stair_props
        stairwell = {
            "length_mm": props.stairwell_length, "width_mm": props.stairwell_width,
            "floor_height_mm": props.floor_height,
            "beam_position_top_mm": props.beam_top, "beam_depth_mm": props.beam_depth,
        }
        api_config = {
            "endpoint": props.ai_endpoint,
            "api_key": props.ai_api_key,
            "model": props.ai_model,
        }
        props.ai_status = "🤖 正在调用 DeepSeek AI..."
        # Force UI refresh
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

        stair_design, ai_source, ai_raw = ai_generate_stair_design(stairwell, api_config)
        props.ai_source = ai_source
        props.ai_raw_response = ai_raw[:1500] if ai_raw else ""
        props.ai_status = f"✅ 完成 — {ai_source}"
        errors = StairRuleChecker.check(stair_design, stairwell)
        props.error_count = len(errors)
        props.error_text = "\n".join(f"{i} {m}" for i, m in errors) if errors else "✅ 全部检查通过"
        try:
            result = build_stair_in_blender(stair_design, stairwell, props.project_name)
            props.stair_id = result["stair_id"]; props.output_path = result["output_path"]
            props.elements_text = "\n".join(f"  ✓ {e}" for e in result["elements"])
            props.status = "ok" if not errors else "needs_review"
            props.status_text = {"ok": "✅ 生成成功！", "needs_review": "⚠️ 已生成，但有规范问题需检查"}.get(props.status, "")
            self.report({'INFO'}, f"Stair created: {result['output_path']}")
        except Exception as e:
            props.ai_status = f"❌ 失败: {e}"
            props.status = "error"; props.status_text = f"❌ 生成失败: {e}"
            self.report({'ERROR'}, str(e))
        return {'FINISHED'}


class AISTAIR_OT_export(bpy.types.Operator):
    bl_idname = "aistair.export"; bl_label = "导出 IFC"
    bl_description = "导出楼梯模型为 IFC 文件"
    def execute(self, context):
        if context.scene.ai_stair_props.output_path:
            self.report({'INFO'}, f"IFC: {context.scene.ai_stair_props.output_path}")
        return {'FINISHED'}


# ============================================================
#  UI: Storey List Item
# ============================================================
class AISTAIR_StoreyItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="楼层")
    elevation: bpy.props.FloatProperty(name="标高")


# ============================================================
#  UI: Properties
# ============================================================
class AISTAIR_Properties(bpy.types.PropertyGroup):
    # IFC import
    ifc_path: bpy.props.StringProperty(name="IFC路径", default="")
    ifc_loaded: bpy.props.BoolProperty(name="已加载", default=False)
    ifc_summary: bpy.props.StringProperty(name="模型摘要", default="")
    candidate_count: bpy.props.IntProperty(name="候选区间", default=0)
    candidate_info: bpy.props.StringProperty(name="候选详情", default="")

    # Stairwell
    project_name: bpy.props.StringProperty(name="项目", default="AI楼梯")
    stairwell_length: bpy.props.IntProperty(name="长度", default=6600, min=1000, max=30000)
    stairwell_width: bpy.props.IntProperty(name="宽度", default=2700, min=1000, max=20000)
    floor_height: bpy.props.IntProperty(name="层高", default=4500, min=2000, max=12000)
    beam_top: bpy.props.IntProperty(name="梁顶高", default=4450, min=0, max=20000)
    beam_depth: bpy.props.IntProperty(name="梁高", default=400, min=200, max=2000)

    # Storey list (from IFC)
    storey_list: bpy.props.CollectionProperty(type=AISTAIR_StoreyItem)
    storey_index: bpy.props.IntProperty(name="楼层", default=0)

    # AI API config
    ai_endpoint: bpy.props.StringProperty(name="API地址", default="https://api.deepseek.com")
    ai_api_key: bpy.props.StringProperty(name="API Key", default="", subtype='PASSWORD')
    ai_model: bpy.props.StringProperty(name="模型", default="deepseek-v4-pro")
    ai_status: bpy.props.StringProperty(name="AI状态", default="")
    ai_source: bpy.props.StringProperty(name="设计来源", default="")
    ai_raw_response: bpy.props.StringProperty(name="AI原始回复", default="")

    # Output
    status: bpy.props.StringProperty(default="")
    status_text: bpy.props.StringProperty(default="")
    error_count: bpy.props.IntProperty(default=0)
    error_text: bpy.props.StringProperty(default="")
    elements_text: bpy.props.StringProperty(default="")
    stair_id: bpy.props.StringProperty(default="")
    output_path: bpy.props.StringProperty(default="")


# ============================================================
#  UI: Panel
# ============================================================
class AISTAIR_PT_main(bpy.types.Panel):
    bl_label = "AI 楼梯设计"
    bl_idname = "AISTAIR_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AI Stair"

    def draw(self, context):
        layout = self.layout
        props = context.scene.ai_stair_props

        # === Step 1: Import IFC ===
        box = layout.box()
        box.label(text="📂 第一步：导入模型", icon='IMPORT')
        row = box.row()
        row.scale_y = 1.5
        row.operator("aistair.import_ifc", text="选择 IFC 文件...", icon='FILE_FOLDER')
        if props.ifc_loaded:
            box.label(text=f"  ✅ {Path(props.ifc_path).name}")
            box.label(text=f"  📊 {props.ifc_summary}")

        # === Step 2: Stairwell ===
        if props.ifc_loaded:
            layout.separator()
            box = layout.box()
            box.label(text="📐 第二步：楼梯间设置", icon='CUBE')

            if props.candidate_info:
                sub = box.box()
                sub.label(text=f"🔍 自动识别 {props.candidate_count} 个潜在楼梯间:")
                for line in props.candidate_info.split("\n"):
                    sub.label(text=line)

            col = box.column(align=True)
            col.prop(props, "project_name", text="项目名称")
            col.separator()
            col.prop(props, "stairwell_length", text="楼梯间长度 (mm)")
            col.prop(props, "stairwell_width", text="楼梯间宽度 (mm)")
            col.prop(props, "floor_height", text="层高 (mm)")
            col.prop(props, "beam_top", text="框架梁顶标高 (mm)")
            col.prop(props, "beam_depth", text="框架梁高 (mm)")

        # === Also allow manual input without IFC ===
        if not props.ifc_loaded:
            layout.separator()
            box = layout.box()
            box.label(text="📐 手动设置（或先导入IFC）", icon='CUBE')
            col = box.column(align=True)
            col.prop(props, "project_name", text="项目名称")
            col.separator()
            col.prop(props, "stairwell_length", text="楼梯间长度 (mm)")
            col.prop(props, "stairwell_width", text="楼梯间宽度 (mm)")
            col.prop(props, "floor_height", text="层高 (mm)")
            col.prop(props, "beam_top", text="框架梁顶标高 (mm)")
            col.prop(props, "beam_depth", text="框架梁高 (mm)")

        # === AI Settings ===
        box = layout.box()
        box.label(text="⚙️ AI 设置 (DeepSeek)", icon='SETTINGS')
        col = box.column(align=True)
        col.prop(props, "ai_endpoint", text="API 地址")
        col.prop(props, "ai_api_key", text="API Key")
        col.prop(props, "ai_model", text="模型")
        if not props.ai_api_key:
            box.label(text="⚠️ 未填 Key 将用本地算法", icon='INFO')

        # === Step 3: Generate ===
        layout.separator()
        if props.ai_status:
            layout.label(text=props.ai_status, icon='INFO')
        row = layout.row()
        row.scale_y = 2.0
        row.operator("aistair.generate", text="🤖 AI 设计楼梯", icon='OUTLINER_OB_LIGHT')

        # === Results ===
        if props.status:
            layout.separator()
            box = layout.box()
            box.label(text="📋 生成结果", icon='TEXT')
            box.label(text=props.status_text, icon='CHECKMARK' if props.status == 'ok' else 'ERROR')

            # Design source (proof of AI)
            if props.ai_source:
                row = box.row()
                row.label(text=f"设计来源: {props.ai_source}", icon='OUTLINER_OB_LIGHT')
            if props.ai_raw_response:
                sub = box.box()
                sub.label(text="AI 原始回复:")
                for line in props.ai_raw_response[:500].split("\n"):
                    sub.label(text=line[:80])

            if props.error_text:
                sub = box.box()
                sub.label(text=f"规范检查 ({props.error_count} 项):")
                for line in props.error_text.split("\n"):
                    sub.label(text=line)
            if props.elements_text:
                sub = box.box()
                sub.label(text="创建的构件:")
                for line in props.elements_text.split("\n"):
                    sub.label(text=line)
            if props.output_path:
                box.label(text=f"保存到: {props.output_path}", icon='FILE_TICK')
                box.operator("aistair.export", text="📁 导出 IFC", icon='EXPORT')


# ============================================================
#  Registration
# ============================================================
classes = [AISTAIR_StoreyItem, AISTAIR_Properties,
           AISTAIR_OT_import_ifc, AISTAIR_OT_generate, AISTAIR_OT_export, AISTAIR_PT_main]

def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.ai_stair_props = bpy.props.PointerProperty(type=AISTAIR_Properties)

def unregister():
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
    del bpy.types.Scene.ai_stair_props

if __name__ == "__main__":
    register()
