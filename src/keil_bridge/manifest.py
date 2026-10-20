"""keil-bridge/manifest.json: where the tree came from and how to write files back."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .textcodec import TextFormat

MANIFEST_PATH = "keil-bridge/manifest.json"
OVERRIDES_PATH = "keil-bridge.toml"
FORMAT_VERSION = 1


@dataclass
class FileRecord:
    encoding: str
    eol: str
    orig_sha256: str  # bytes in the Keil project when last synchronized
    conv_sha256: str  # bytes in the converted tree when last synchronized

    @property
    def format(self) -> TextFormat:
        return TextFormat(self.encoding, self.eol)


@dataclass
class Manifest:
    keil_root: str  # absolute path of the Keil project root
    uvprojx: str  # relative to keil_root
    target: str
    default_eol: str  # used for files that had no line break
    files: dict[str, FileRecord] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)  # files under keil_root not copied

    @property
    def keil_root_path(self) -> Path:
        return Path(self.keil_root)

    def save(self, tree: Path):
        data = {"version": FORMAT_VERSION, **asdict(self)}
        data["files"] = {path: asdict(record) for path, record in sorted(self.files.items())}
        path = tree / MANIFEST_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, tree: Path) -> "Manifest":
        data = json.loads((tree / MANIFEST_PATH).read_text(encoding="utf-8"))
        if data.pop("version", None) != FORMAT_VERSION:
            raise ValueError(f"{MANIFEST_PATH}: unsupported manifest version")
        files = {path: FileRecord(**record) for path, record in data.pop("files").items()}
        return cls(**data, files=files)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
