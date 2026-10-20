"""Convert a small synthetic Keil project, build it, edit it and backport."""

import json
import shutil
import subprocess

import pytest

from keil_bridge import cli
from keil_bridge.manifest import Manifest
from test_units import STARTUP

UVPROJX = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<Project>
  <SchemaVersion>2.1</SchemaVersion>
  <Targets>
    <Target>
      <TargetName>Target 1</TargetName>
      <ToolsetNumber>0x4</ToolsetNumber>
      <ToolsetName>ARM-ADS</ToolsetName>
      <TargetOption>
        <TargetCommonOption>
          <Device>STM32F103C8</Device>
          <Cpu>IROM(0x08000000,0x10000) IRAM(0x20000000,0x5000) CPUTYPE("Cortex-M3") CLOCK(12000000) ELITTLE</Cpu>
          <OutputName>demo</OutputName>
        </TargetCommonOption>
        <TargetArmAds>
          <ArmAdsMisc>
            <RvdsVP>0</RvdsVP>
            <Ir1Chk>1</Ir1Chk>
            <Im1Chk>1</Im1Chk>
            <OnChipMemories>
              <OCR_RVCT4><Type>1</Type><StartAddress>0x8000000</StartAddress><Size>0x10000</Size></OCR_RVCT4>
              <OCR_RVCT9><Type>0</Type><StartAddress>0x20000000</StartAddress><Size>0x5000</Size></OCR_RVCT9>
            </OnChipMemories>
          </ArmAdsMisc>
          <Cads>
            <Optim>1</Optim>
            <uC99>0</uC99>
            <VariousControls>
              <MiscControls>--some-armcc-flag</MiscControls>
              <Define>DEMO=1</Define>
              <IncludePath>..\\Inc;..\\Missing</IncludePath>
            </VariousControls>
          </Cads>
          <Aads><VariousControls><MiscControls></MiscControls></VariousControls></Aads>
          <LDads><umfTarg>1</umfTarg><Misc></Misc></LDads>
        </TargetArmAds>
      </TargetOption>
      <Groups>
        <Group>
          <GroupName>App</GroupName>
          <Files>
            <File><FileName>main.c</FileName><FileType>1</FileType><FilePath>..\\Src\\main.c</FilePath></File>
            <File><FileName>startup.s</FileName><FileType>2</FileType><FilePath>.\\startup.s</FilePath></File>
          </Files>
        </Group>
      </Groups>
    </Target>
  </Targets>
</Project>
"""

MAIN_C = """#include "Board.h"
/* 主程序 */
void SystemInit(void) {}
void USART1_IRQHandler(void) {}
int main(void)
{
    return BOARD_VALUE;
}
"""


@pytest.fixture
def keil(tmp_path):
    root = tmp_path / "keil"
    (root / "Project").mkdir(parents=True)
    (root / "Src").mkdir()
    (root / "Inc").mkdir()
    (root / "Project" / "demo.uvprojx").write_text(UVPROJX, encoding="utf-8")
    (root / "Project" / "startup.s").write_bytes(STARTUP.replace("\n", "\r\n").encode("ascii"))
    (root / "Project" / "demo.uvoptx").write_text("ide state")
    (root / "Src" / "main.c").write_bytes(MAIN_C.replace("\n", "\r\n").encode("gbk"))
    # Included as "Board.h" but named in lower case: works on Windows only.
    (root / "Inc" / "board.h").write_bytes(b"#define BOARD_VALUE 0\r\n")
    return root


def run(*args):
    return cli.main([str(a) for a in args])


def test_convert_build_backport(keil, tmp_path, capsys):
    out = tmp_path / "gcc"
    assert run("convert", keil / "Project" / "demo.uvprojx", out, "--no-git") == 0
    err = capsys.readouterr().err
    assert "include directory '..\\Missing' does not exist" in err
    assert "--some-armcc-flag" in err
    assert "keil-bridge/include-shims/Board.h" in err

    main_c = out / "Src" / "main.c"
    assert main_c.read_bytes() == MAIN_C.encode("utf-8")
    assert (out / "keil-bridge" / "startup.c").exists()
    assert not (out / "Project" / "demo.uvoptx").exists()
    manifest = Manifest.load(out)
    assert manifest.files["Src/main.c"].encoding == "gbk"
    assert "Project/demo.uvoptx" in manifest.skipped
    project_mk = (out / "keil-bridge" / "project.mk").read_text()
    assert "CSTD ?= gnu90" in project_mk and "OPT ?= -O0" in project_mk and "-DDEMO=1" in project_mk
    # convert builds in a staging dir; the compile database must name the final one.
    commands = json.loads((out / "compile_commands.json").read_text())
    assert {c["directory"] for c in commands} == {str(out.resolve())}

    if shutil.which("arm-none-eabi-gcc") and shutil.which("make"):
        build = subprocess.run(["make"], cwd=out, capture_output=True, text=True)
        assert build.returncode == 0, build.stdout + build.stderr
        assert (out / "build" / "demo.hex").exists()

    # Edit in the converted tree and write back.
    main_c.write_text(MAIN_C.replace("/* 主程序 */", "/* 主程序：已修改 */"), encoding="utf-8")
    assert run("status", out) == 0
    assert "M  Src/main.c" in capsys.readouterr().out
    assert run("backport", out) == 0
    assert (keil / "Src" / "main.c").read_bytes() == MAIN_C.replace("/* 主程序 */", "/* 主程序：已修改 */").replace(
        "\n", "\r\n"
    ).encode("gbk")
    capsys.readouterr()
    assert run("status", out) == 0
    assert capsys.readouterr().out.strip() == "no changes"

    # Both sides changed: refused unless --force.
    (keil / "Src" / "main.c").write_bytes(b"int keil;\r\n")
    main_c.write_text("int gcc;\n", encoding="utf-8")
    assert run("backport", out) == 1
    assert (keil / "Src" / "main.c").read_bytes() == b"int keil;\r\n"
    assert run("backport", out, "--force") == 0
    assert (keil / "Src" / "main.c").read_bytes() == b"int gcc;\r\n"


def test_regen_overrides(keil, tmp_path, capsys):
    out = tmp_path / "gcc"
    assert run("convert", keil / "Project" / "demo.uvprojx", out, "--no-git", "--set", "extra_cflags=-Wall") == 0
    err = capsys.readouterr().err
    assert "--some-armcc-flag" not in err  # superseded by the extra_cflags override

    assert run("regen", out, "--set", "c_std=gnu11", "--set", "stack_size=0x800") == 0
    assert "CSTD ?= gnu11" in (out / "keil-bridge" / "project.mk").read_text()
    assert "__stack_size = 0x800;" in (out / "keil-bridge" / "stm32f103c8.ld").read_text()
    toml = (out / "keil-bridge.toml").read_text()
    assert 'c_std = "gnu11"' in toml and 'extra_cflags = ["-Wall"]' in toml
    assert "| `c_std` | `gnu11` | gnu90 (overridden) |" in (out / "CONVERSION.md").read_text()

    assert run("regen", out, "--unset", "c_std") == 0
    assert "CSTD ?= gnu90" in (out / "keil-bridge" / "project.mk").read_text()
    assert run("regen", out, "--set", "nonsense=1") == 1


def test_bad_startup_produces_no_output(keil, tmp_path, capsys):
    startup = keil / "Project" / "startup.s"
    startup.write_bytes(startup.read_bytes().replace(b"BLX     R0", b"MOVS    R1, #0\r\n                 BLX     R0"))
    out = tmp_path / "gcc"
    assert run("convert", keil / "Project" / "demo.uvprojx", out, "--no-git") == 1
    assert "Reset_Handler does more than call SystemInit and __main" in capsys.readouterr().err
    assert not out.exists()
    assert list(tmp_path.iterdir()) == [keil]
