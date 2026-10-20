"""Reading Keil uVision project files (.uvprojx, and the older .uvproj).

The result is a KeilProject: the facts from the project file, with every
path resolved on disk and expressed relative to the project root. Mapping
these facts to GCC options is done in toolchain.py.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .diagnostics import Diagnostics, KeilBridgeError
from .paths import common_ancestor, is_absolute_windows, rel, resolve_nocase, split_windows

FILE_TYPES = {
    "1": "c",
    "2": "asm",
    "3": "object",
    "4": "library",
    "5": "text",
    "8": "cpp",
}


@dataclass
class Memory:
    origin: int
    size: int


@dataclass
class SourceFile:
    path: str  # relative to the project root, with the on-disk spelling
    kind: str  # a value of FILE_TYPES
    group: str
    build: bool


@dataclass
class KeilProject:
    uvprojx: Path  # absolute
    root: Path  # absolute; common ancestor of everything the project uses
    target: str
    other_targets: list[str]
    device: str
    toolset: str  # "ac5" or "ac6"
    cpu_type: str  # e.g. "Cortex-M3"
    fpu: str | None  # the FPU field of <Cpu>, e.g. "FPU2", or None
    fpu_used: bool  # "Floating Point Hardware" is enabled
    output_name: str
    rom: Memory | None
    ram: Memory | None
    defines: list[str]
    undefines: list[str]
    includes: list[str]  # relative to root, existing directories only
    sources: list[SourceFile]
    # Raw option elements, keyed by tag name, for toolchain.py to interpret.
    cads: dict[str, str] = field(default_factory=dict)
    aads: dict[str, str] = field(default_factory=dict)
    ldads: dict[str, str] = field(default_factory=dict)

    @property
    def uvprojx_rel(self) -> str:
        return rel(self.uvprojx, self.root)


def load(uvprojx: Path, target_name: str | None, diag: Diagnostics) -> KeilProject:
    uvprojx = uvprojx.resolve()
    try:
        tree = ET.parse(uvprojx)
    except (OSError, ET.ParseError) as e:
        diag.error(f"cannot read project file: {e}", str(uvprojx))
        raise KeilBridgeError(str(e)) from None
    if uvprojx.suffix.lower() == ".uvproj":
        diag.warn("uVision 4 project file; read with the uVision 5 rules", uvprojx.name)

    project = tree.getroot()
    targets = project.findall("Targets/Target")
    if not targets:
        diag.fatal("project has no <Target>", uvprojx.name)
    names = [_text(t, "TargetName") for t in targets]
    if target_name is None:
        target = targets[0]
        if len(targets) > 1:
            diag.note(f"project has {len(targets)} targets, using the first one: {names[0]!r} (choose with --target)")
    elif target_name in names:
        target = targets[names.index(target_name)]
    else:
        diag.fatal(f"no target named {target_name!r}; available: {', '.join(names)}")
    _check_rte(project, diag, uvprojx.name)

    reader = _TargetReader(uvprojx, target, diag)
    result = reader.read()
    result.other_targets = [n for n in names if n != result.target]
    return result


def _check_rte(project: ET.Element, diag: Diagnostics, where: str):
    components = project.findall("RTE/components/component")
    if components:
        listed = ", ".join(f"{c.get('Cclass')}:{c.get('Cgroup')}" for c in components)
        diag.warn(f"RTE (run-time environment) components are not converted: {listed}", where)


def _text(element: ET.Element | None, path: str, default: str = "") -> str:
    if element is None:
        return default
    found = element.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _children(element: ET.Element | None) -> dict[str, str]:
    if element is None:
        return {}
    return {child.tag: (child.text or "").strip() for child in element if len(child) == 0}


def _int(text: str) -> int:
    return int(text, 0)


class _TargetReader:
    def __init__(self, uvprojx: Path, target: ET.Element, diag: Diagnostics):
        self.uvprojx = uvprojx
        self.base = uvprojx.parent
        self.target = target
        self.diag = diag
        self.where = uvprojx.name

    def read(self) -> KeilProject:
        t = self.target
        option = t.find("TargetOption")
        common = option.find("TargetCommonOption") if option is not None else None
        arm_ads = option.find("TargetArmAds") if option is not None else None
        if common is None or arm_ads is None:
            toolset = _text(t, "ToolsetName") or "unknown"
            self.diag.fatal(
                f"toolset {toolset!r} is not an ARM toolset (only ARM-ADS projects are supported)", self.where
            )

        toolset_name = _text(t, "ToolsetName")
        if toolset_name != "ARM-ADS":
            self.diag.error(f"toolset {toolset_name!r} is not supported (only ARM-ADS)", self.where)
        toolset = "ac6" if _text(t, "uAC6") == "1" else "ac5"

        cpu_type, fpu = self._parse_cpu(_text(common, "Cpu"))
        misc = arm_ads.find("ArmAdsMisc")
        self._check_unused_memories(misc)
        cads = arm_ads.find("Cads")
        controls = cads.find("VariousControls") if cads is not None else None

        sources = self._read_groups()
        include_texts = _split_list(_text(controls, "IncludePath"), ";")
        include_paths = [p for text in include_texts if (p := self._resolve_dir(text)) is not None]

        # The tree we convert must contain the project file, every source and
        # every include directory.
        anchors = [self.base] + [p for p, _ in sources] + include_paths
        root = common_ancestor(anchors)
        if root == Path(root.anchor):
            self.diag.fatal("project files are spread over the whole file system; cannot choose a project root")

        return KeilProject(
            uvprojx=self.uvprojx,
            root=root,
            target=_text(t, "TargetName"),
            other_targets=[],
            device=_text(common, "Device"),
            toolset=toolset,
            cpu_type=cpu_type,
            fpu=fpu,
            fpu_used=_text(misc, "RvdsVP", "0") != "0",
            output_name=_text(common, "OutputName") or "firmware",
            rom=self._memory(misc, "OCR_RVCT4", "Ir1Chk", "flash_size"),
            ram=self._memory(misc, "OCR_RVCT9", "Im1Chk", "ram_size"),
            defines=_split_list(_text(controls, "Define"), ","),
            undefines=_split_list(_text(controls, "Undefine"), ","),
            includes=_unique(rel(p, root) for p in include_paths),
            sources=[SourceFile(rel(p, root), s.kind, s.group, s.build) for p, s in sources],
            cads=_children(cads) | _children(controls),
            aads=_children(arm_ads.find("Aads")) | _children(arm_ads.find("Aads/VariousControls")),
            ldads=_children(arm_ads.find("LDads")),
        )

    def _parse_cpu(self, cpu: str) -> tuple[str, str | None]:
        """Pick CPUTYPE("...") and FPU* out of the space-separated <Cpu> string,
        e.g. 'IROM(0x08000000,0x10000) IRAM(...) CPUTYPE("Cortex-M4") FPU2 CLOCK(12000000) ELITTLE'."""
        cpu_type = ""
        fpu = None
        for item in cpu.split():
            if item.startswith('CPUTYPE("') and item.endswith('")'):
                cpu_type = item[len('CPUTYPE("') : -2]
            elif item.startswith("FPU"):
                fpu = item
        if not cpu_type:
            self.diag.error(f"no CPUTYPE in <Cpu> {cpu!r}", self.where)
        return cpu_type, fpu

    def _memory(self, misc, name, check, option) -> Memory | None:
        """A memory region as set in the 'Target' dialog (OCR_RVCT*)."""
        region = misc.find(f"OnChipMemories/{name}") if misc is not None else None
        if region is None or _text(misc, check) != "1":
            self.diag.error(f"memory region {name} is not enabled in the target settings", self.where, option=option)
            return None
        return Memory(_int(_text(region, "StartAddress", "0")), _int(_text(region, "Size", "0")))

    def _check_unused_memories(self, misc):
        unused = {
            "Ir2Chk": "IROM2",
            "Im2Chk": "IRAM2",
            "Ro1Chk": "off-chip ROM1",
            "Ro2Chk": "off-chip ROM2",
            "Ro3Chk": "off-chip ROM3",
            "Ra1Chk": "off-chip RAM1",
            "Ra2Chk": "off-chip RAM2",
            "Ra3Chk": "off-chip RAM3",
        }
        for check, region in unused.items():
            if _text(misc, check) == "1":
                self.diag.warn(f"{region} is enabled but only IROM1/IRAM1 are converted", self.where)

    def _read_groups(self):
        result = []
        for group in self.target.findall("Groups/Group"):
            group_name = _text(group, "GroupName")
            group_build = self._include_in_build(group.find("GroupOption"), f"group {group_name!r}")
            for f in group.findall("Files/File"):
                file_type = _text(f, "FileType")
                file_path = _text(f, "FilePath")
                kind = FILE_TYPES.get(file_type)
                if kind is None:
                    self.diag.warn(f"file '{file_path}' has unknown FileType {file_type}; ignored", self.where)
                    continue
                path = self._resolve_file(file_path)
                if path is None:
                    continue
                build = group_build and self._include_in_build(f.find("FileOption"), f"file '{file_path}'")
                result.append((path, SourceFile("", kind, group_name, build)))
        return result

    def _include_in_build(self, option: ET.Element | None, what: str) -> bool:
        if option is None:
            return True
        controls = option.find(".//VariousControls")
        for tag in ("MiscControls", "Define", "Undefine", "IncludePath"):
            value = _text(controls, tag)
            if value:
                self.diag.warn(f"{what} has its own {tag} {value!r}, which is ignored", self.where)
        return _text(option, "CommonProperty/IncludeInBuild", "1") != "0"

    def _resolve_file(self, text: str) -> Path | None:
        path = self._resolve(text)
        if path is None:
            self.diag.error(f"source file '{text}' does not exist", self.where)
        elif not path.is_file():
            self.diag.error(f"source path '{text}' is not a file", self.where)
            return None
        return path

    def _resolve_dir(self, text: str) -> Path | None:
        path = self._resolve(text)
        if path is None or not path.is_dir():
            self.diag.warn(f"include directory '{text}' does not exist; dropped", self.where, option="includes")
            return None
        return path

    def _resolve(self, text: str) -> Path | None:
        if is_absolute_windows(text):
            self.diag.warn(f"absolute path '{text}' cannot be used on this machine; ignored", self.where)
            return None
        path, case_differs = resolve_nocase(self.base, split_windows(text))
        if path is not None and case_differs:
            self.diag.note(f"path '{text}' matches '{path.name}' on disk only when ignoring case", self.where)
        return path.resolve() if path is not None else None


def _split_list(text: str, separator: str) -> list[str]:
    return [item.strip() for item in text.split(separator) if item.strip()]


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))
