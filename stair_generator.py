"""
AI Stair Generator — runs inside Blender.
Reads stair design JSON → creates IFC stair model → runs rule checks.
"""

import ifcopenshell
import ifcopenshell.api
from ifcopenshell.api import run
import json
import sys
import os
import math
from pathlib import Path


# ============================================================
#  Rule Checker (deterministic — no AI involved)
# ============================================================

class StairRuleChecker:
    """Validates stair design against structural codes (GB 50010)."""

    def __init__(self, design: dict):
        rules = design.get("rules", {})
        self.RISER_MAX = rules.get("riser_max_mm", 175)
        self.TREAD_MIN = rules.get("tread_min_mm", 260)
        self.HEADROOM_MIN = rules.get("headroom_min_mm", 2200)
        self.WIDTH_MIN = rules.get("stair_width_min_mm", 1100)
        self.LANDING_MIN = rules.get("landing_depth_min_mm", 1200)

    def check(self, stair: dict, stairwell: dict) -> list[dict]:
        errors = []

        # Check stair width
        width = stair.get("width_mm", 0)
        if width < self.WIDTH_MIN:
            errors.append({
                "level": "error",
                "rule": "stair_width",
                "message": f"楼梯宽度 {width}mm < 最小要求 {self.WIDTH_MIN}mm",
                "fix": f"增加楼梯宽度至少到 {self.WIDTH_MIN}mm"
            })

        # Check each flight
        for flight in stair.get("flights", []):
            riser = flight.get("riser_height_mm", 0)
            tread = flight.get("tread_depth_mm", 0)
            n_treads = flight.get("tread_count", 0)
            flight_id = flight.get("id", "?")

            if riser > self.RISER_MAX:
                errors.append({
                    "level": "error",
                    "rule": "riser_max",
                    "location": flight_id,
                    "message": f"踏步高度 {riser}mm > 最大允许 {self.RISER_MAX}mm",
                    "fix": f"减小踏步高度至 {self.RISER_MAX}mm 以下，增加踏步数"
                })

            if tread < self.TREAD_MIN:
                errors.append({
                    "level": "error",
                    "rule": "tread_min",
                    "location": flight_id,
                    "message": f"踏步深度 {tread}mm < 最小要求 {self.TREAD_MIN}mm",
                    "fix": f"增加踏步深度至少到 {self.TREAD_MIN}mm"
                })

        # Check landings
        for landing in stair.get("landings", []):
            depth = landing.get("length_mm", 0)
            landing_id = landing.get("id", "?")
            if depth < self.LANDING_MIN:
                errors.append({
                    "level": "error",
                    "rule": "landing_depth",
                    "location": landing_id,
                    "message": f"平台深度 {depth}mm < 最小要求 {self.LANDING_MIN}mm",
                    "fix": f"增加平台深度至少到 {self.LANDING_MIN}mm"
                })

        # Headroom check (simplified: check if beam bottom clears top of stair)
        if stairwell and stair:
            floor_height = stairwell.get("floor_height_mm", 0)
            beam_top = stairwell.get("beam_position_top_mm", floor_height)
            beam_depth = stairwell.get("beam_depth_mm", 400)
            beam_bottom = beam_top - beam_depth

            # Find the highest point of the stair under the beam
            for landing in stair.get("landings", []):
                elev = landing.get("elevation_mm", 0)
                thickness = landing.get("thickness_mm", 150)
                top_of_landing = elev
                if landing.get("name") == "楼层平台":
                    continue  # floor landing is at the top
                headroom = beam_bottom - top_of_landing
                if headroom < self.HEADROOM_MIN:
                    errors.append({
                        "level": "error",
                        "rule": "headroom",
                        "location": landing.get("id", "?"),
                        "message": f"中间平台净高 {headroom}mm < 要求 {self.HEADROOM_MIN}mm (梁底标高 {beam_bottom}mm)",
                        "fix": f"降低平台标高或提高梁底标高。当前梁底={beam_bottom}mm, 平台顶={top_of_landing}mm"
                    })

        return errors


# ============================================================
#  IFC Stair Builder
# ============================================================

def build_stair_ifc(design: dict, output_path: str) -> dict:
    """Create IFC stair model from AI design JSON."""

    model = ifcopenshell.file()
    stair_data = design.get("stair", {})
    stairwell = design.get("stairwell", {})
    project_name = design.get("project", "AI Stair")

    # Project setup
    project = run("root.create_entity", model, ifc_class="IfcProject", name=project_name)
    run("unit.assign_unit", model)

    site = run("root.create_entity", model, ifc_class="IfcSite", name="Site")
    building = run("root.create_entity", model, ifc_class="IfcBuilding", name="Building")
    storey = run("root.create_entity", model, ifc_class="IfcBuildingStorey", name="Level 1")
    run("aggregate.assign_object", model, products=[site], relating_object=project)
    run("aggregate.assign_object", model, products=[building], relating_object=site)
    run("aggregate.assign_object", model, products=[storey], relating_object=building)

    # Create IfcStair (container)
    stair = run("root.create_entity", model, ifc_class="IfcStair", name=stair_data.get("type", "stair"))
    run("spatial.assign_container", model, products=[stair], relating_structure=storey)
    stair_id = stair.GlobalId

    # Store design params on the stair
    pset = run("pset.add_pset", model, product=stair, name="AI_Stair_Design")
    run("pset.edit_pset", model, pset=pset, properties={
        "Type": stair_data.get("type", "double_run"),
        "Width_mm": stair_data.get("width_mm", 0),
        "DesignCode": design.get("design_code", "GB 50010"),
        "TotalRiserHeight_mm": stair_data.get("flights", [{}])[0].get("riser_height_mm", 0) if stair_data.get("flights") else 0,
    })

    created_elements = []

    # Create flights
    for flight_data in stair_data.get("flights", []):
        flight = run("root.create_entity", model, ifc_class="IfcStairFlight", name=flight_data.get("name", "Flight"))
        run("aggregate.assign_object", model, products=[flight], relating_object=stair)

        f_pset = run("pset.add_pset", model, product=flight, name="AI_Flight_Params")
        run("pset.edit_pset", model, pset=f_pset, properties={
            "TreadCount": flight_data.get("tread_count", 0),
            "RiserHeight_mm": flight_data.get("riser_height_mm", 0),
            "TreadDepth_mm": flight_data.get("tread_depth_mm", 0),
            "FlightLength_mm": flight_data.get("length_mm", 0),
            "FlightHeight_mm": flight_data.get("height_mm", 0),
            "StartsAtBottom": flight_data.get("start_at_bottom", True),
        })

        created_elements.append({
            "element": "flight",
            "id": flight_data.get("id"),
            "global_id": flight.GlobalId,
            "name": flight_data.get("name"),
        })
        print(f"  ✓ Created IfcStairFlight '{flight_data.get('name')}' ({flight.GlobalId})")

    # Create landings (as IfcSlab)
    for landing_data in stair_data.get("landings", []):
        slab = run("root.create_entity", model, ifc_class="IfcSlab", name=landing_data.get("name", "Landing"))
        run("aggregate.assign_object", model, products=[slab], relating_object=stair)
        run("spatial.assign_container", model, products=[slab], relating_structure=storey)

        l_pset = run("pset.add_pset", model, product=slab, name="AI_Landing_Params")
        run("pset.edit_pset", model, pset=l_pset, properties={
            "LandingLength_mm": landing_data.get("length_mm", 0),
            "LandingWidth_mm": landing_data.get("width_mm", 0),
            "Thickness_mm": landing_data.get("thickness_mm", 0),
            "Elevation_mm": landing_data.get("elevation_mm", 0),
        })

        created_elements.append({
            "element": "landing",
            "id": landing_data.get("id"),
            "global_id": slab.GlobalId,
            "name": landing_data.get("name"),
        })
        print(f"  ✓ Created IfcSlab (landing) '{landing_data.get('name')}' ({slab.GlobalId})")

    # Create railings
    for railing_data in stair_data.get("railings", []):
        rail = run("root.create_entity", model, ifc_class="IfcRailing", name=railing_data.get("name", "Railing"))
        run("aggregate.assign_object", model, products=[rail], relating_object=stair)

        r_pset = run("pset.add_pset", model, product=rail, name="AI_Railing_Params")
        run("pset.edit_pset", model, pset=r_pset, properties={
            "Height_mm": railing_data.get("height_mm", 900),
            "AttachedTo": railing_data.get("attached_to", ""),
        })

        created_elements.append({
            "element": "railing",
            "id": railing_data.get("id"),
            "global_id": rail.GlobalId,
            "name": railing_data.get("name"),
        })
        print(f"  ✓ Created IfcRailing '{railing_data.get('name')}' ({rail.GlobalId})")

    # Save
    model.write(output_path)

    return {
        "stair_global_id": stair_id,
        "elements": created_elements,
    }


# ============================================================
#  Main: Read JSON → Check Rules → Build → Report
# ============================================================

def main(input_path: str, output_path: str):
    print(f"=== AI Stair Generator ===")
    print(f"Input: {input_path}")

    with open(input_path, "r", encoding="utf-8") as f:
        design = json.load(f)

    # Step 1: Rule check BEFORE building
    print("\n--- Rule Check ---")
    checker = StairRuleChecker(design)
    errors = checker.check(
        design.get("stair", {}),
        design.get("stairwell", {})
    )

    if errors:
        print(f"  ❌ Found {len(errors)} issue(s):")
        for e in errors:
            print(f"     [{e['rule']}] {e['message']}")
            print(f"     → Fix: {e['fix']}")
    else:
        print("  ✅ All checks passed!")

    # Step 2: Build IFC model
    print("\n--- Building IFC Model ---")
    result = build_stair_ifc(design, output_path)

    # Step 3: Output report
    report = {
        "status": "ok" if not errors else "needs_fix",
        "project": design.get("project", ""),
        "output_file": output_path,
        "stair_global_id": result["stair_global_id"],
        "elements_created": len(result["elements"]),
        "rule_check": {
            "passed": len(errors) == 0,
            "errors": errors,
        }
    }

    report_path = Path(output_path).stem + "_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n--- Report saved to {report_path} ---")
    print(f"Status: {report['status']}")
    print(f"Stair GlobalId: {result['stair_global_id']}")
    return report


if __name__ == "__main__":
    # Default input/output (override via Blender sys.argv)
    input_file = "stair_design.json"
    output_file = "stair_output.ifc"

    try:
        if "--" in sys.argv:
            after = sys.argv[sys.argv.index("--") + 1:]
            import argparse
            p = argparse.ArgumentParser()
            p.add_argument("--input", "-i", type=str)
            p.add_argument("--output", "-o", type=str)
            args = p.parse_args(after)
            if args.input:
                input_file = args.input
            if args.output:
                output_file = args.output
    except Exception:
        pass

    main(input_file, output_file)
