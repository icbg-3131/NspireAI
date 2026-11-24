#!/usr/bin/env python3
"""Check packaged Mach-O architecture, load paths and embedded hashes on Linux."""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import sys


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def u32(data: bytes, offset: int, order: str = "<") -> int:
    return struct.unpack_from(order + "I", data, offset)[0]


def code_signature(data: bytes, offset: int, size: int) -> int:
    blob = data[offset:offset + size]
    magic, length, count = struct.unpack_from(">3I", blob)
    require(magic == 0xFADE0CC0 and length <= len(blob), "invalid embedded signature")
    algorithms = {1: "sha1", 2: "sha256", 3: "sha256", 4: "sha384"}
    checked = 0
    for i in range(count):
        slot, start = struct.unpack_from(">2I", blob, 12 + 8 * i)
        if slot != 0 and not 0x1000 <= slot < 0x1005:
            continue
        directory_size = u32(blob, start + 4, ">")
        directory = blob[start:start + directory_size]
        magic, _, _, flags, hashes, _, _, slots, limit = struct.unpack_from(">9I", directory)
        hash_size, hash_type, _, exponent = struct.unpack_from("4B", directory, 36)
        require(magic == 0xFADE0C02 and flags & 2, "expected ad-hoc CodeDirectory")
        require(0 < limit <= offset and hash_type in algorithms, "invalid code hash configuration")
        page_size = (1 << exponent) if exponent else limit
        require(slots == (limit + page_size - 1) // page_size, "invalid code page count")
        for page in range(slots):
            start_code = page * page_size
            digest = hashlib.new(algorithms[hash_type], data[start_code:min(start_code + page_size, limit)])
            start_hash = hashes + page * hash_size
            require(digest.digest()[:hash_size] == directory[start_hash:start_hash + hash_size],
                    f"CodeDirectory page {page} has a mismatched hash")
        checked += slots
    require(checked > 0, "no embedded code page hashes")
    return checked


def macho(path: Path):
    data = path.read_bytes()
    if data[:4] != bytes.fromhex("cffaedfe"):
        require(data[:4] not in {bytes.fromhex(magic) for magic in ("cafebabe", "cafebabf", "feedfacf")},
                f"binary is not a thin little-endian Mach-O: {path.name}")
        return None
    cpu, _, _, count, command_size = struct.unpack_from("<5I", data, 4)
    require(cpu == 0x0100000C, f"{path.name} is not ARM64")
    end = 32 + command_size
    offset, pages, minimum = 32, 0, 0
    dependencies, rpaths = [], []
    for _ in range(count):
        command, size = struct.unpack_from("<2I", data, offset)
        require(size >= 8 and offset + size <= end <= len(data), "invalid Mach-O load command")
        kind = command & 0x7FFFFFFF
        if kind in (0xC, 0xD, 0x18, 0x1C, 0x1F, 0x20, 0x23):
            start = offset + u32(data, offset + 8)
            value = data[start:offset + size].partition(b"\0")[0].decode()
            if kind == 0x1C:
                rpaths.append(value)
            elif kind == 0xD:
                require(value.startswith(("@rpath/", "@loader_path/", "@executable_path/")),
                        f"non-relocatable install ID {value}")
            else:
                dependencies.append(value)
        elif kind == 0x1D:
            pages = code_signature(data, u32(data, offset + 8), u32(data, offset + 12))
        elif kind == 0x32:
            require(u32(data, offset + 8) == 1, f"{path.name} targets a platform other than macOS")
            minimum = u32(data, offset + 12)
        elif kind == 0x24:
            minimum = u32(data, offset + 8)
        offset += size
    require(pages and minimum, f"missing code signature or macOS target in {path.name}")
    return dependencies, rpaths, pages, minimum


def expand(value: str, binary: Path, executable: Path) -> Path:
    token, _, suffix = value.partition("/")
    roots = {"@loader_path": binary.parent, "@executable_path": executable.parent}
    require(token in roots, f"external dynamic path {value} in {binary.name}")
    return (roots[token] / suffix).resolve()


def audit(bundle: Path) -> None:
    bundle = bundle.resolve()
    executable = bundle / "runtime/bin/python3.12"
    library = bundle / "lib/libusb-1.0.0.dylib"
    records = {}
    for item in sorted(bundle.rglob("*")):
        if item.is_symlink():
            require(item.exists() and item.resolve().is_relative_to(bundle),
                    f"invalid bundled symlink {item.relative_to(bundle)}")
        elif item.is_file():
            # Skip ordinary source and data files without loading them into memory.
            with item.open("rb") as stream:
                magic = stream.read(4)
            if magic in {bytes.fromhex(value) for value in ("cffaedfe", "cafebabe", "cafebabf", "feedfacf")}:
                records[item] = macho(item)
    require(executable in records and library in records, "Python or libusb is missing")

    executable_roots = [expand(p, executable, executable) for p in records[executable][1]]
    for binary, (dependencies, rpaths, pages, minimum) in records.items():
        roots = executable_roots + [expand(p, binary, executable) for p in rpaths]
        require(all(root.is_relative_to(bundle) and root.is_dir() for root in roots),
                f"unusable RPATH in {binary.relative_to(bundle)}")
        for dependency in dependencies:
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if dependency.startswith("@rpath/"):
                relative = dependency.removeprefix("@rpath/")
                candidates = [(root / relative).resolve() for root in roots]
            else:
                candidates = [expand(dependency, binary, executable)]
            require(any(c.is_relative_to(bundle) and c.is_file() for c in candidates),
                    f"unresolved dependency {dependency} in {binary.relative_to(bundle)}")
        print(f"{binary.relative_to(bundle)}: ARM64, {pages} code-page hashes match, "
              f"{len(dependencies)} load paths checked")
    maximum = max(record[3] for record in records.values())
    print(f"Static audit passed for {len(records)} Mach-O files. "
          f"Highest declared macOS minimum: {maximum >> 16}.{maximum >> 8 & 255}.{maximum & 255}.")
    print("Actual macOS startup, Gatekeeper decisions and USB echo require on-device testing.")


if __name__ == "__main__":
    audit(Path(sys.argv[1]))
