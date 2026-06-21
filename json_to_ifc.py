"""
JSON to IFC — AI design instructions → Blender/Bonsai execution.

Runs INSIDE Blender Python environment:
  blender --background --python json_to_ifc.py -- --input design.json --output result.ifc

The input JSON follows this schema:
{
  "project": "Project Name",
  "elements": [
    {
      "action": "create",
      "ifc_type": "IfcBeam",
      "name": "KL1",
      "storey": "Level 1",
      "properties": { "Width": 300, "Height": 700, "Length": 6000 },
      "material": "C30"
    }
  ]
}
"""

import ifcopenshell
import ifcopenshell.api
from ifcopenshell.api import run
import json
import sys
import os
import argparse
from pathlib import Path


def create_ifc_from_json(json_path: str, output_path: str):
    """Execute AI design instructions to create an IFC model."""

    with open(json_path, "r", encoding="utf-8") as f:
        design = json.load(f)

    # 1. Create fresh IFC model
    model = ifcopenshell.file()
    project_name = design.get("project", "AI Generated Model")

    project = run("root.create_entity", model, ifc_class="IfcProject", name=project_name)
    run("unit.assign_unit", model)

    # 2. Spatial structure
    site = run("root.create_entity", model, ifc_class="IfcSite", name="Default Site")
    building = run("root.create_entity", model, ifc_class="IfcBuilding", name="Default Building")
    run("aggregate.assign_object", model, products=[site], relating_object=project)
    run("aggregate.assign_object", model, products=[building], relating_object=site)

    # 3. Storeys map (name → entity)
    storeys = {}
    storey_names = set()
    for elem in design.get("elements", []):
        storey_names.add(elem.get("storey", "Level 1"))

    for sname in sorted(storey_names):
        s = run("root.create_entity", model, ifc_class="IfcBuildingStorey", name=sname)
        run("aggregate.assign_object", model, products=[s], relating_object=building)
        storeys[sname] = s

    # 4. Create elements
    created = []
    for i, elem in enumerate(design.get("elements", [])):
        action = elem.get("action", "create")
        ifc_type = elem.get("ifc_type", "IfcBuildingElementProxy")
        name = elem.get("name", f"Element_{i+1}")
        storey_name = elem.get("storey", "Level 1")

        if action != "create":
            print(f"  Skipping unknown action: {action}")
            continue

        entity = run("root.create_entity", model, ifc_class=ifc_type, name=name)
        run("spatial.assign_container", model, products=[entity], relating_structure=storeys[storey_name])

        # Add property set if properties provided
        props = elem.get("properties", {})
        if props:
            pset = run("pset.add_pset", model, product=entity, name="AI_Design_Parameters")
            run("pset.edit_pset", model, pset=pset, properties=props)

        # Add material if specified
        material_name = elem.get("material")
        if material_name:
            mat = run("material.add_material", model, name=material_name)
            run("material.assign_material", model, products=[entity], material=mat)

        created.append({
            "global_id": entity.GlobalId,
            "name": name,
            "ifc_type": ifc_type,
            "storey": storey_name,
        })

        print(f"  ✓ Created {ifc_type} '{name}' (GlobalId: {entity.GlobalId})")

    # 5. Save
    model.write(output_path)
    print(f"\nSaved: {output_path}")
    print(f"Total elements: {len(created)}")

    return created


if __name__ == "__main__":
    # Support both CLI args and hardcoded defaults (for Blender headless)
    input_path = "ai_design.json"
    output_path = "ai_generated.ifc"

    # Try to parse Blender's sys.argv (skip blender args before '--')
    try:
        if "--" in sys.argv:
            idx = sys.argv.index("--")
            after = sys.argv[idx + 1:]
            parser = argparse.ArgumentParser()
            parser.add_argument("--input", "-i", type=str)
            parser.add_argument("--output", "-o", type=str)
            args = parser.parse_args(after)
            if args.input:
                input_path = args.input
            if args.output:
                output_path = args.output
    except Exception:
        pass  # Use defaults

    print(f"Input: {input_path}")
    print(f"Output: {output_path}")
    create_ifc_from_json(input_path, output_path)
