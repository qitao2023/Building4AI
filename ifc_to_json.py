"""
IFC to JSON converter — optimized for AI consumption.
Extracts entities with GlobalId, type, name, properties, and spatial relationships.
"""

import ifcopenshell
import json
import sys
from pathlib import Path


def ifc_to_simple_json(ifc_path: str) -> dict:
    """Convert an IFC file to a simplified JSON structure AI can easily understand."""

    model = ifcopenshell.open(ifc_path)

    result = {
        "file": Path(ifc_path).name,
        "schema": model.schema,
        "description": None,
        "spatial_structure": [],
        "elements": [],
    }

    # Extract project description
    for project in model.by_type("IfcProject"):
        result["description"] = project.Name or project.Description or "Untitled"

    # Spatial structure (site > building > storey hierarchy)
    for site in model.by_type("IfcSite"):
        site_info = _entity_brief(site)
        site_info["buildings"] = []
        for building in _get_related(model, site, "IfcBuilding"):
            bld_info = _entity_brief(building)
            bld_info["storeys"] = []
            for storey in _get_related(model, building, "IfcBuildingStorey"):
                sty_info = _entity_brief(storey)
                sty_info["elements"] = []
                bld_info["storeys"].append(sty_info)
            site_info["buildings"].append(bld_info)
        result["spatial_structure"].append(site_info)

    # All building elements with their properties
    element_types = [
        "IfcBeam", "IfcColumn", "IfcSlab", "IfcWall", "IfcStair",
        "IfcStairFlight", "IfcRailing", "IfcPlate", "IfcFooting",
        "IfcPile", "IfcMember", "IfcCurtainWall", "IfcRamp",
        "IfcRoof", "IfcBuildingElementProxy",
    ]

    for ifc_type in element_types:
        for entity in model.by_type(ifc_type):
            elem_info = _entity_detail(entity, model)
            if elem_info:
                result["elements"].append(elem_info)

    # Also check for IfcElementAssembly (e.g., precast units)
    for assembly in model.by_type("IfcElementAssembly"):
        elem_info = _entity_detail(assembly, model)
        if elem_info:
            result["elements"].append(elem_info)

    # Count summary
    type_counts = {}
    for elem in result["elements"]:
        t = elem["ifc_type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    result["summary"] = {
        "total_elements": len(result["elements"]),
        "by_type": type_counts,
    }

    return result


def _entity_brief(entity) -> dict:
    """Brief info for spatial structure nodes."""
    return {
        "global_id": getattr(entity, "GlobalId", None),
        "name": getattr(entity, "Name", None),
        "ifc_type": entity.is_a(),
    }


def _entity_detail(entity, model) -> dict | None:
    """Detailed info for building elements."""
    info = {
        "global_id": getattr(entity, "GlobalId", None),
        "name": getattr(entity, "Name", None),
        "ifc_type": entity.is_a(),
        "description": getattr(entity, "Description", None),
        "properties": {},
        "materials": [],
    }

    # Get property sets
    for rel in getattr(entity, "IsDefinedBy", []) or []:
        if rel.is_a("IfcRelDefinesByProperties"):
            pset = rel.RelatingPropertyDefinition
            if pset and pset.is_a("IfcPropertySet"):
                pset_name = pset.Name or "UnnamedPset"
                props = {}
                for prop in getattr(pset, "HasProperties", []) or []:
                    if prop.is_a("IfcPropertySingleValue"):
                        val = prop.NominalValue
                        if val is not None:
                            props[prop.Name] = val.wrappedValue if hasattr(val, 'wrappedValue') else str(val)
                    elif prop.is_a("IfcPropertyEnumeratedValue"):
                        vals = prop.EnumerationValues
                        if vals:
                            props[prop.Name] = [v.wrappedValue if hasattr(v, 'wrappedValue') else str(v) for v in vals]
                if props:
                    info["properties"][pset_name] = props

    # Get material
    for rel in getattr(entity, "HasAssociations", []) or []:
        if rel.is_a("IfcRelAssociatesMaterial"):
            mat = rel.RelatingMaterial
            if mat:
                mat_name = getattr(mat, "Name", None)
                if mat_name:
                    info["materials"].append(mat_name)

    # Get quantity sets (for dimensions)
    for rel in getattr(entity, "IsDefinedBy", []) or []:
        if rel.is_a("IfcRelDefinesByProperties"):
            qto = rel.RelatingPropertyDefinition
            if qto and qto.is_a("IfcElementQuantity"):
                qto_name = qto.Name or "Quantities"
                quantities = {}
                for q in getattr(qto, "Quantities", []) or []:
                    if hasattr(q, "LengthValue") and hasattr(q, "Name"):
                        quantities[q.Name] = q.LengthValue
                    elif hasattr(q, "AreaValue") and hasattr(q, "Name"):
                        quantities[q.Name] = q.AreaValue
                    elif hasattr(q, "VolumeValue") and hasattr(q, "Name"):
                        quantities[q.Name] = q.VolumeValue
                if quantities:
                    info["properties"][qto_name] = quantities

    # Container (which storey)
    container = getattr(entity, "ContainedInStructure", None)
    if container:
        try:
            storey = container[0].RelatingStructure if container else None
            if storey:
                info["storey"] = {
                    "global_id": storey.GlobalId,
                    "name": storey.Name,
                }
        except (IndexError, AttributeError):
            pass

    return info


def _get_related(model, entity, target_type: str) -> list:
    """Get related entities of a specific type through decomposition relationships."""
    related = []
    for rel in getattr(entity, "IsDecomposedBy", []) or []:
        for obj in getattr(rel, "RelatedObjects", []) or []:
            if obj.is_a(target_type):
                related.append(obj)
    return related


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ifc_to_json.py <input.ifc> [output.json]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else Path(input_path).stem + ".json"

    data = ifc_to_simple_json(input_path)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Converted {input_path} → {output_path}")
    print(f"Found {data['summary']['total_elements']} elements: {data['summary']['by_type']}")
