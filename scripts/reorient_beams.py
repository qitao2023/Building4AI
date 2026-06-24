"""
Reorient two specific beams in 空压站-S-20220530.ifc:

  圆管梁 O152x6 (3gZgM2_SP7$u36tWWmycVR):
    Current: beam long axis in viewer XY plane (horizontal)
    Target:  beam long axis in viewer YZ plane (vertical)
    IFC RefDirection: (0.653, 0, 0.757) → (0, 0.653, 0.757)
    IFC Axis:          (0.757, 0, -0.653) → (1, 0, 0)

  角钢梁 L90x6  (0DKbCl2H58YvK$smTGuHnP):
    Current: nearly vertical
    Target:  horizontal in XY plane, keep existing X/Y direction
    IFC RefDirection: project to XY plane, normalize
    IFC Axis:          (0, 0, 1)
"""

import math
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom


def normalize(v):
    """Return a normalized tuple of 3 floats."""
    x, y, z = v
    n = math.sqrt(x * x + y * y + z * z)
    if n < 1e-12:
        return (0.0, 0.0, 0.0)
    return (x / n, y / n, z / n)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def compute_new_placement(beam, target):
    """
    Compute new (Axis, RefDirection) for the beam's ObjectPlacement.

    target: 'vertical' (O152x6 → YZ plane) or 'horizontal' (L90x6 → XY plane)
    Returns (new_axis, new_refdir) as tuples of 3 floats.
    """
    rp = beam.ObjectPlacement.RelativePlacement
    old_refdir = tuple(float(v) for v in rp.RefDirection.DirectionRatios)
    # old_axis = tuple(float(v) for v in rp.Axis.DirectionRatios)

    if target == "vertical":
        # O152x6: move from XZ to YZ plane
        # Swap X component to Y, keep Z component
        new_refdir = normalize((0.0, old_refdir[0], old_refdir[2]))
        new_axis = (1.0, 0.0, 0.0)
    elif target == "horizontal":
        # L90x6: project RefDirection to XY plane, normalize
        proj = (old_refdir[0], old_refdir[1], 0.0)
        new_refdir = normalize(proj)
        new_axis = (0.0, 0.0, 1.0)
    else:
        raise ValueError(f"Unknown target: {target}")

    # Ensure orthogonality
    d = dot(new_refdir, new_axis)
    if abs(d) > 0.001:
        # Gram-Schmidt: adjust new_axis to be orthogonal to new_refdir
        ax, ay, az = new_axis
        rx, ry, rz = new_refdir
        new_axis = (ax - d * rx, ay - d * ry, az - d * rz)
        new_axis = normalize(new_axis)

    return new_axis, new_refdir


def apply_placement(beam, new_axis, new_refdir):
    """Write new Axis and RefDirection to the beam's ObjectPlacement."""
    rp = beam.ObjectPlacement.RelativePlacement
    rp.Axis.DirectionRatios = new_axis
    rp.RefDirection.DirectionRatios = new_refdir


def get_beam_long_axis_world(beam):
    """
    Return the world-space long-axis direction of the beam
    using ifcopenshell geometry tessellation.
    """
    settings = ifcopenshell.geom.settings()
    shape = ifcopenshell.geom.create_shape(settings, beam)
    m = shape.transformation.matrix
    # Column 0 (local X) of the 4x4 column-major matrix = beam long axis in world
    return (m[0], m[1], m[2])


def get_beam_origin_world(beam):
    """Return the world-space origin position using ifcopenshell placement utility."""
    import ifcopenshell.util.placement as placement
    M = placement.get_local_placement(beam.ObjectPlacement)
    return (float(M[0, 3]), float(M[1, 3]), float(M[2, 3]))


def verify_reorientation(beam, target):
    """Verify the beam long axis is correct after reorientation."""
    long_axis = get_beam_long_axis_world(beam)
    la = normalize(long_axis)

    if target == "vertical":
        # Long axis should be in YZ plane (X ≈ 0)
        return abs(la[0]) < 0.05, la
    elif target == "horizontal":
        # Long axis should be in XY plane (Z ≈ 0)
        return abs(la[2]) < 0.05, la
    return False, la


def main():
    project_dir = Path(__file__).resolve().parent.parent
    input_path = project_dir / "test" / "空压站-S-20220530.ifc"
    output_path = project_dir / "test" / "空压站-S-20220530_reoriented.ifc"

    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        sys.exit(1)

    print(f"Opening: {input_path}")
    model = ifcopenshell.open(str(input_path))

    modifications = [
        {
            "guid": "3gZgM2_SP7$u36tWWmycVR",
            "name": "圆管梁 O152x6",
            "target": "vertical",
        },
        {
            "guid": "0DKbCl2H58YvK$smTGuHnP",
            "name": "角钢梁 L90x6",
            "target": "horizontal",
        },
    ]

    for mod in modifications:
        beam = model.by_guid(mod["guid"])
        name = mod["name"]
        target = mod["target"]

        print(f"\n{'='*60}")
        print(f"Processing: {name} ({mod['guid']})")
        print(f"Target: {target}")

        # Read current state
        rp = beam.ObjectPlacement.RelativePlacement
        old_axis = tuple(float(v) for v in rp.Axis.DirectionRatios)
        old_refdir = tuple(float(v) for v in rp.RefDirection.DirectionRatios)
        old_origin = tuple(float(v) for v in rp.Location.Coordinates)

        print(f"  Old Origin:      ({old_origin[0]:.2f}, {old_origin[1]:.2f}, {old_origin[2]:.2f})")
        print(f"  Old Axis (Z):    ({old_axis[0]:.6f}, {old_axis[1]:.6f}, {old_axis[2]:.6f})")
        print(f"  Old RefDir (X):  ({old_refdir[0]:.6f}, {old_refdir[1]:.6f}, {old_refdir[2]:.6f})")

        # Compute and apply new placement
        new_axis, new_refdir = compute_new_placement(beam, target)
        apply_placement(beam, new_axis, new_refdir)

        print(f"  New Axis (Z):    ({new_axis[0]:.6f}, {new_axis[1]:.6f}, {new_axis[2]:.6f})")
        print(f"  New RefDir (X):  ({new_refdir[0]:.6f}, {new_refdir[1]:.6f}, {new_refdir[2]:.6f})")

        # Verify dot product
        d = dot(new_axis, new_refdir)
        print(f"  Orthogonality:   dot(Axis,RefDir) = {d:.2e}")

    # Write output
    print(f"\nWriting: {output_path}")
    model.write(str(output_path))
    print("Done.")

    # ---- Verification pass ----
    print(f"\n{'='*60}")
    print("VERIFICATION: re-reading output file and tessellating...")
    model2 = ifcopenshell.open(str(output_path))

    all_ok = True
    for mod in modifications:
        beam = model2.by_guid(mod["guid"])
        name = mod["name"]
        target = mod["target"]

        ok, la = verify_reorientation(beam, target)
        status = "OK" if ok else "FAIL"
        print(f"  {name}: long_axis=({la[0]:.6f}, {la[1]:.6f}, {la[2]:.6f}) {status}")
        if not ok:
            all_ok = False

        # Also check origin unchanged
        rp = beam.ObjectPlacement.RelativePlacement
        origin = tuple(float(v) for v in rp.Location.Coordinates)
        orig = modifications[0]["guid"]  # not useful
        if target == "vertical":
            expected_origin = (36138.16, 31650.00, -116.04)
        else:
            expected_origin = (38978.24, 28937.19, 1336.00)
        dx = origin[0] - expected_origin[0]
        dy = origin[1] - expected_origin[1]
        dz = origin[2] - expected_origin[2]
        print(f"    Origin drift: ({dx:.3f}, {dy:.3f}, {dz:.3f}) mm")

    if all_ok:
        print("\n ALL VERIFICATIONS PASSED")
    else:
        print("\n SOME VERIFICATIONS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
