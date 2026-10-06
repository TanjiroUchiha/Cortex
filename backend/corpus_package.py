import argparse
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

import yaml

# The expanded corpus is consolidated into corpus.d/<category>/ — manifest.json
# sits at the corpus.d root and category folders resolve to domains at load.
PACKAGE_NAME = "corpus.d"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "dataset" / PACKAGE_NAME


class MetadataLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    pairs = loader.construct_pairs(node, deep=deep)
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("Frontmatter keys must be unique strings")
        result[key] = value
    return result


MetadataLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def parse_markdown(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
    if not match:
        if text.startswith("---\n"):
            raise ValueError("Unterminated YAML frontmatter")
        return {}, text.strip()
    try:
        metadata = yaml.load(match.group(1), Loader=MetadataLoader)
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML frontmatter") from exc
    if not isinstance(metadata, dict):
        raise ValueError("Frontmatter must be a mapping")
    metadata = json.loads(json.dumps(metadata, default=str))
    for key in ("allowed_roles", "allowed_users", "allowed_entities", "related_documents"):
        if key in metadata and (not isinstance(metadata[key], list)
                                or any(not isinstance(v, str) for v in metadata[key])):
            raise ValueError(f"{key} must be a list of strings")
    if "document_id" in metadata and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", str(metadata["document_id"])):
        raise ValueError("Invalid document_id")
    return metadata, text[match.end():].strip()


def relative_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ValueError(f"Unsafe corpus path: {value}")
    return path


def package_sources(root=PACKAGE_ROOT):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    seen = set()
    for entry in manifest["documents"]:
        relative = relative_path(entry["path"])
        file = root.joinpath(*relative.parts)
        metadata, prose = parse_markdown(file.read_text(encoding="utf-8"))
        for key, value in metadata.items():
            if key in entry and entry[key] != value:
                raise ValueError(f"Manifest/frontmatter mismatch: {relative}: {key}")
        metadata = {**entry, **metadata, "package": PACKAGE_NAME}
        if metadata["document_id"] in seen or relative.parts[0] != metadata["category"]:
            raise ValueError(f"Duplicate ID or category mismatch: {relative}")
        seen.add(metadata["document_id"])
        yield metadata["category"], {"id": metadata["document_id"], "title": entry["title"],
                                      "content": prose, "format": "md", "file": str(file),
                                      "metadata": metadata, "relative_path": str(relative)}


def import_archive(archive, destination=PACKAGE_ROOT):
    destination = Path(destination)
    with zipfile.ZipFile(archive) as source:
        members = []
        for info in source.infolist():
            path = relative_path(info.filename)
            if not path.parts or path.parts[0] != PACKAGE_NAME:
                raise ValueError("Unexpected archive root")
            if info.is_dir():
                continue
            if info.file_size > 10_000_000 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Oversized file or symlink in archive")
            target = destination.joinpath(*path.parts[1:])
            content = source.read(info)
            if target.exists() and target.read_bytes() != content:
                raise FileExistsError(f"Refusing to replace {target}")
            members.append((target, content))
        for target, content in members:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                with target.open("xb") as handle:
                    handle.write(content)
    return len(members)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(f"Imported/preserved {import_archive(args.archive)} package files in {PACKAGE_ROOT}")
