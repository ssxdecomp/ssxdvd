#!/usr/bin/env python3
import argparse
import os
import shutil
import sys
import json
from pathlib import Path
from typing import Dict, List, Set, Union

import ninja_syntax
import splat
import splat.scripts.split as split
from splat.segtypes.linker_entry import LinkerEntry

ROOT = Path(__file__).parent.resolve()
TOOLS_DIR = ROOT / "tools"
OUTDIR = "out"

YAML_FILE = ROOT / "config" / "us" / "ssxdvd.yaml"
BASENAME = "SLUS_203.26"
LD_PATH = f"{BASENAME}.ld"
ELF_PATH = f"{OUTDIR}/{BASENAME}"
MAP_PATH = f"{OUTDIR}/{BASENAME}.map"
PRE_ELF_PATH = f"{OUTDIR}/{BASENAME}.elf"

COMMON_INCLUDES = "-Iinclude -isystem include/sdk/ee -isystem include/gcc"


CC_DIR = f"{TOOLS_DIR}/cc/ee-991111-01"
DRIVER_PATH_FLAG = f"-B{CC_DIR}/lib/gcc-lib/ee/2.9-ee-991111-01/"

# See tools/cc/README.md for how these were gathered
COMMON_CFLAGS = "-O2 -fno-edge-lcm -fomit-frame-pointer"
COMMON_CXXFLAGS = "-fno-exceptions -fno-rtti"

COMPILE_C_RULE = f"{CC_DIR}/bin/ee-gcc -c {COMMON_INCLUDES} {DRIVER_PATH_FLAG} {COMMON_CFLAGS} $in"
COMPILE_CXX_RULE = f"{CC_DIR}/bin/ee-gcc -xc++ -c {COMMON_INCLUDES} {DRIVER_PATH_FLAG} {COMMON_CFLAGS} {COMMON_CXXFLAGS} $in"

# Path to decompals binutils.
DECOMPALS_BINUTILS = "tools/binutils/"

CATEGORY_MAP = {
    "sce": "Libs",
    "data": "Data",
}

def clean():
    files_to_clean = [
        ".splache",
        ".ninja_log",
        "build.ninja",
        "permuter_settings.toml",
        LD_PATH
    ]
    for filename in files_to_clean:
        if os.path.exists(filename):
            os.remove(filename)

    shutil.rmtree("asm", ignore_errors=True)
    shutil.rmtree("assets", ignore_errors=True)
    shutil.rmtree("obj", ignore_errors=True)
    shutil.rmtree("out", ignore_errors=True)


def write_permuter_settings():
    with open("permuter_settings.toml", "w", encoding="utf-8") as f:
        f.write(f"""compiler_command = "{COMPILE_CXX_RULE} -D__GNUC__"
assembler_command = "{DECOMPALS_BINUTILS}/mips-ps2-decompals-as -march=r5900 -mabi=eabi -Iinclude"
compiler_type = "gcc"

[preserve_macros]

[decompme.compilers]
"tools/cc/ee-991111-01/bin/ee-gcc" = "ee-gcc2.9-991111-01"
""")

def build_stuff(linker_entries: List[LinkerEntry], skip_checksum=False, objects_only=False, dual_objects=False):
    """
    Build the objects and the final ELF file.
    If objects_only is True, only build objects and skip linking/checksum.
    If dual_objects is True, build objects twice: once normally, once with -DSKIP_ASM.
    """
    built_objects: Set[Path] = set()
    objdiff_units = []  # For objdiff.json

    def build(
        object_paths: Union[Path, List[Path]],
        src_paths: List[Path],
        task: str,
        variables: Dict[str, str] = None,
        implicit_outputs: List[str] = None,
        out_dir: str = None,
        extra_flags: str = "",
        collect_objdiff: bool = False,
        orig_entry=None,
    ):
        """
        Helper function to build objects.
        """
        # Handle none parameters
        if variables is None:
            variables = {}

        if implicit_outputs is None:
            implicit_outputs = []

        # Convert object_paths to list if it is not already
        if not isinstance(object_paths, list):
            object_paths = [object_paths]

        # Only rewrite output path to .o if out_dir is set (i.e. --objects mode)
        if out_dir:
            new_object_paths = []
            for obj in object_paths:
                obj = Path(obj)
                stem = obj.stem
                if obj.suffix in [".s", ".c" , ".cpp"]:
                    stem = obj.stem
                else:
                    if obj.suffix == ".o" and obj.with_suffix("").suffix in [".cpp",".s", ".c"]:
                        stem = obj.with_suffix("").stem
                target_dir = out_dir if out_dir else obj.parent
                new_obj = Path(target_dir) / (stem + ".o")
                new_object_paths.append(new_obj)
            object_paths = new_object_paths

        # Otherwise, use the original object_paths (with .s.o, .c.o, etc.)

        # Convert paths to strings
        object_strs = [str(obj) for obj in object_paths]

        # Add object paths to built_objects
        for idx, object_path in enumerate(object_paths):
            if object_path.suffix == ".o":
                built_objects.add(object_path)
            # Add extra_flags to variables if present
            build_vars = variables.copy()
            if extra_flags:
                build_vars["cflags"] = extra_flags
            ninja.build(
                outputs=[str(object_path)],
                rule=task,
                inputs=[str(s) for s in src_paths],
                variables=build_vars,
                implicit_outputs=implicit_outputs,
            )
            # Collect for objdiff.json if requested
            if collect_objdiff and orig_entry is not None:
                src = src_paths[0] if src_paths else None
                if src:
                    src = Path(src)
                    # Always use the final "matched" name, i.e. as if it will be in src/ with no asm/ prefix
                    try:
                        # If the file is in asm/, replace asm/ with nothing (just drop asm/)
                        if src.parts[0] == "asm":
                            rel = Path(*src.parts[1:])
                        elif src.parts[0] == "src":
                            rel = Path(*src.parts[1:])
                        else:
                            rel = src
                        # Remove extension for the name
                        name = str(rel.with_suffix(""))
                    except Exception:
                        name = str(src.with_suffix(""))
                else:
                    name = object_path.stem
                if "target" in str(object_path):
                    target_path = str(object_path)
                    # Determine if a .c or .cpp file exists in src/ for this unit (recursively)
                    src_base = rel.with_suffix("")
                    src_c_files = list(Path("src").rglob(src_base.name + ".c"))
                    src_cpp_files = list(Path("src").rglob(src_base.name + ".cpp"))
                    has_src = bool(src_c_files or src_cpp_files)
                    # Determine the category based on the name
                    categories = [name.split("/")[0]]
                    unit = {
                        "name": name,
                        "target_path": target_path,
                        "metadata": {
                            "progress_categories": categories,
                        }
                    }
                    if has_src:
                        base_path = str(object_path).replace("target", "current")
                        unit["base_path"] = base_path
                    objdiff_units.append(unit)

    ninja = ninja_syntax.Writer(open(str(ROOT / "build.ninja"), "w", encoding="utf-8"), width=9999)

    ld_args = "-EL -T config/undefined_syms_auto.txt -T config/undefined_funcs_auto.txt -Map $mapfile -T $in -o $out"

    ninja.rule(
        "as",
        description="as $in",
        command=f"cpp {COMMON_INCLUDES} $in -o  - | {DECOMPALS_BINUTILS}/mips-ps2-decompals-as -no-pad-sections -EL -march=5900 -mabi=eabi -Iinclude -o $out",
    )

    ninja.rule(
        "cc",
        description="cc $in",
        command=f"{COMPILE_C_RULE} $cflags -o $out && {DECOMPALS_BINUTILS}/mips-ps2-decompals-strip $out -N dummy-symbol-name",
    )

    ninja.rule(
        "cpp",
        description="cpp $in",
        command=f"{COMPILE_CXX_RULE} $cflags -o $out && {DECOMPALS_BINUTILS}/mips-ps2-decompals-strip $out -N dummy-symbol-name",
    )

    ninja.rule(
        "ld",
        description="link $out",
        command=f"{DECOMPALS_BINUTILS}/mips-ps2-decompals-ld {ld_args}",
    )

    if not skip_checksum:
        ninja.rule(
            "sha1sum",
            description="sha1sum $in",
            command="sha1sum -c $in && touch $out",
        )

    ninja.rule(
        "elf",
        description="elf $out",
        command=f"{DECOMPALS_BINUTILS}/mips-ps2-decompals-objcopy $in $out -O binary",
    )

    # Add recipes for everything
    for entry in linker_entries:
        seg = entry.segment

        if seg.type[0] == ".":
            continue

        if entry.object_path is None:
            continue

        if isinstance(seg, splat.segtypes.common.asm.CommonSegAsm) or isinstance(
            seg, splat.segtypes.common.data.CommonSegData
        ):
            if dual_objects:
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/target", collect_objdiff=True, orig_entry=entry)
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/current", extra_flags="-DSKIP_ASM")
            else:
                build(entry.object_path, entry.src_paths, "as")
        elif isinstance(seg, splat.segtypes.common.c.CommonSegC):
            if dual_objects:
                build(entry.object_path, entry.src_paths, "cc", out_dir="obj/target", collect_objdiff=True, orig_entry=entry)
                build(entry.object_path, entry.src_paths, "cc", out_dir="obj/current", extra_flags="-DSKIP_ASM")
            else:
                build(entry.object_path, entry.src_paths, "cc")
        elif isinstance(seg, splat.segtypes.common.cpp.CommonSegCpp):
            if dual_objects:
                build(entry.object_path, entry.src_paths, "cpp", out_dir="obj/target", collect_objdiff=True, orig_entry=entry)
                build(entry.object_path, entry.src_paths, "cpp", out_dir="obj/current", extra_flags="-DSKIP_ASM")
            else:
                build(entry.object_path, entry.src_paths, "cpp")
        elif isinstance(seg, splat.segtypes.common.databin.CommonSegDatabin):
            if dual_objects:
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/target", collect_objdiff=True, orig_entry=entry)
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/current", extra_flags="-DSKIP_ASM")
            else:
                build(entry.object_path, entry.src_paths, "as")
        elif isinstance(seg, splat.segtypes.common.rodatabin.CommonSegRodatabin):
            if dual_objects:
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/target", collect_objdiff=True, orig_entry=entry)
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/current", extra_flags="-DSKIP_ASM")
            else:
                build(entry.object_path, entry.src_paths, "as")
        elif isinstance(seg, splat.segtypes.common.textbin.CommonSegTextbin):
            if dual_objects:
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/target", collect_objdiff=True, orig_entry=entry)
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/current", extra_flags="-DSKIP_ASM")
            else:
                build(entry.object_path, entry.src_paths, "as")
        elif isinstance(seg, splat.segtypes.common.bin.CommonSegBin):
            if dual_objects:
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/target", collect_objdiff=True, orig_entry=entry)
                build(entry.object_path, entry.src_paths, "as", out_dir="obj/current", extra_flags="-DSKIP_ASM")
            else:
                build(entry.object_path, entry.src_paths, "as")
        else:
            print(f"ERROR: Unsupported build segment type {seg.type}")
            sys.exit(1)

    if objects_only:
        # Write objdiff.json if dual_objects (i.e. --objects)
        if dual_objects:
            objdiff = {
                "$schema": "https://raw.githubusercontent.com/encounter/objdiff/main/config.schema.json",
                "custom_make": "ninja",
                "custom_args": [],
                "build_target": False,
                "build_base": True,
                "watch_patterns": [
                    "src/**/*.c",
                    "src/**/*.cp",
                    "src/**/*.cpp",
                    "src/**/*.cxx",
                    "src/**/*.h",
                    "src/**/*.hp",
                    "src/**/*.hpp",
                    "src/**/*.hxx",
                    "src/**/*.s",
                    "src/**/*.S",
                    "src/**/*.asm",
                    "src/**/*.inc",
                    "src/**/*.py",
                    "src/**/*.yml",
                    "src/**/*.txt",
                    "src/**/*.json"
                ],
                "units": objdiff_units,
                "progress_categories": [ {"id": id, "name": name} for id, name in CATEGORY_MAP.items() ],
            }
            with open("objdiff.json", "w", encoding="utf-8") as f:
                json.dump(objdiff, f, indent=2)
        return

    ninja.build(
        PRE_ELF_PATH,
        "ld",
        LD_PATH,
        implicit=[str(obj) for obj in built_objects],
        variables={"mapfile": MAP_PATH},
    )

    ninja.build(
        ELF_PATH,
        "elf",
        PRE_ELF_PATH,
    )

    # FIXME: Do this in a more elegant way;
    # probably a python script which reads the splat yaml to get the SHA1,
    # rather than needing a seperate sha1 file.
    # My obession with DRY will never stop >:3
    if not skip_checksum:
        ninja.build(
            ELF_PATH + ".ok",
            "sha1sum",
            "config/checksum.sha1",
            implicit=[ELF_PATH],
        )
    else:
        print("Not adding sha1 rule")

def main():
    parser = argparse.ArgumentParser(description="Configure the project")
    parser.add_argument(
        "-c",
        "--clean",
        help="Clean artifacts and build",
        action="store_true",
    )
    parser.add_argument(
        "-C",
        "--only-clean",
        help="Only clean artifacts",
        action="store_true",
    )
    parser.add_argument(
        "-s",
        "--skip-checksum",
        help="Skip the checksum step",
        action="store_true",
    )
    parser.add_argument(
        "-o",
        "--objects",
        help="Build objects to obj/target and obj/current (with -DSKIP_ASM), skip linking and checksum",
        action="store_true",
    )
    args = parser.parse_args()

    do_clean = (args.clean or args.only_clean) or False

    # FIXME: Set skip_checksum to False once builds are able
    # to match; it has been temporairly enabled here.
    do_skip_checksum = args.skip_checksum or True
    do_objects = args.objects or False

    if do_clean:
        clean()
        if args.only_clean:
            return

    split.main([YAML_FILE], modes="all", verbose=False)

    linker_entries = split.linker_writer.entries

    if do_objects:
        build_stuff(linker_entries, skip_checksum=True, objects_only=True, dual_objects=True)
    else:
        build_stuff(linker_entries, do_skip_checksum)

    write_permuter_settings()

if __name__ == "__main__":
    main()
