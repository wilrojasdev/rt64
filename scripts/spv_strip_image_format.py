#!/usr/bin/env python3
# SPIR-V post-processor: rewrite every OpTypeImage Format operand to Unknown (0),
# deduplicate the OpTypeImage instructions that collapse to the same signature
# as a result, and ensure the module declares the StorageImageRead/Write
# WithoutFormat capabilities required for accessing format-unknown storage
# images.
#
# Why we need this: DXC 1.7 (the binary vendored under src/contrib/dxc) silently
# ignores [[vk::image_format("unknown")]] and emits the typed default
# (Rgba32f, R32ui, R32f, R16ui, ...) instead. On strict-aliasing drivers — Mali
# Valhall G57 on the Samsung Galaxy A24 is the reference — a mismatch between
# the SPIR-V OpTypeImage Format and the bound VkImageView/VkBufferView format
# produces *undefined values across the entire image*, not just at the accessed
# texel. In BK64 that manifests as a uniform white frame for any draw whose
# framebuffer-manipulation passes alias different N64 framebuffer formats
# (16/32 bpp).
#
# This script is meant to be applied only to shaders that legitimately need
# format-agnostic image access — currently the FbReadAny* and FbWrite* compute
# shaders, all of which target whatever native N64 framebuffer is live. Plume
# already enables the WithoutFormat device features when the GPU exposes them.
import struct
import sys

SPIRV_MAGIC = 0x07230203
HEADER_WORDS = 5

# Opcodes we touch by name. Values from the SPIR-V core grammar.
OP_CAPABILITY = 17
OP_NAME = 5
OP_MEMBER_NAME = 6
OP_TYPE_IMAGE = 25
OP_IMAGE_READ = 98
OP_IMAGE_WRITE = 99

# Capabilities (SPIR-V Capability enum).
CAP_STORAGE_IMAGE_READ_WITHOUT_FORMAT = 55
CAP_STORAGE_IMAGE_WRITE_WITHOUT_FORMAT = 56


def parse_instructions(data):
    """Yield (offset, opcode, word_count) for each instruction past the header."""
    offset = HEADER_WORDS * 4
    while offset < len(data):
        word0 = struct.unpack_from("<I", data, offset)[0]
        word_count = (word0 >> 16) & 0xFFFF
        opcode = word0 & 0xFFFF
        if word_count == 0:
            raise SystemExit(f"malformed instruction at byte {offset}")
        yield offset, opcode, word_count
        offset += word_count * 4


def patch(path: str) -> None:
    with open(path, "rb") as f:
        data = bytearray(f.read())

    if len(data) < HEADER_WORDS * 4:
        raise SystemExit(f"{path}: not a valid SPIR-V module (too small)")

    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != SPIRV_MAGIC:
        # SPIR-V can be big-endian — reject; DXC always emits little-endian.
        raise SystemExit(f"{path}: unexpected magic 0x{magic:08x}")

    # Pass 1: rewrite every OpTypeImage Format operand to Unknown (0) and record
    # each one's id, post-patch signature, sampled value, and whether the
    # signature already had an entry. Track which capabilities the module uses
    # so we know whether to add Read/WriteWithoutFormat.
    image_id_to_signature: dict[int, tuple] = {}
    image_id_to_sampled: dict[int, int] = {}
    image_format_patches = 0
    has_image_read = False
    has_image_write = False
    existing_capabilities: set[int] = set()
    for offset, opcode, word_count in parse_instructions(data):
        if opcode == OP_CAPABILITY and word_count >= 2:
            cap = struct.unpack_from("<I", data, offset + 1 * 4)[0]
            existing_capabilities.add(cap)
        elif opcode == OP_TYPE_IMAGE and word_count >= 9:
            # Layout: word0 (header), result_id, sampled_type_id, dim, depth,
            # arrayed, ms, sampled, format, [access_qualifier?]
            result_id = struct.unpack_from("<I", data, offset + 1 * 4)[0]
            sampled_type = struct.unpack_from("<I", data, offset + 2 * 4)[0]
            dim = struct.unpack_from("<I", data, offset + 3 * 4)[0]
            depth = struct.unpack_from("<I", data, offset + 4 * 4)[0]
            arrayed = struct.unpack_from("<I", data, offset + 5 * 4)[0]
            ms = struct.unpack_from("<I", data, offset + 6 * 4)[0]
            sampled = struct.unpack_from("<I", data, offset + 7 * 4)[0]
            format_offset = offset + 8 * 4
            current_format = struct.unpack_from("<I", data, format_offset)[0]
            if current_format != 0:
                struct.pack_into("<I", data, format_offset, 0)
                image_format_patches += 1
            access_qualifier = (
                struct.unpack_from("<I", data, offset + 9 * 4)[0]
                if word_count >= 10 else None
            )
            image_id_to_signature[result_id] = (
                sampled_type, dim, depth, arrayed, ms, sampled, 0, access_qualifier,
            )
            image_id_to_sampled[result_id] = sampled
        elif opcode == OP_IMAGE_READ:
            has_image_read = True
        elif opcode == OP_IMAGE_WRITE:
            has_image_write = True

    # Pass 2: group OpTypeImages by post-patch signature; build duplicate -> canonical map.
    canonical_for_signature: dict[tuple, int] = {}
    duplicate_to_canonical: dict[int, int] = {}
    for image_id, signature in image_id_to_signature.items():
        canonical = canonical_for_signature.get(signature)
        if canonical is None:
            canonical_for_signature[signature] = image_id
        else:
            duplicate_to_canonical[image_id] = canonical

    # Pass 3: figure out which capabilities we need to inject. We add
    # StorageImageReadWithoutFormat if the module performs OpImageRead and
    # contains at least one storage OpTypeImage (sampled == 2). Same for write.
    has_storage_image = any(
        s == 2 for s in image_id_to_sampled.values()
    )
    capabilities_to_add: list[int] = []
    if (
        has_image_read
        and has_storage_image
        and CAP_STORAGE_IMAGE_READ_WITHOUT_FORMAT not in existing_capabilities
    ):
        capabilities_to_add.append(CAP_STORAGE_IMAGE_READ_WITHOUT_FORMAT)
    if (
        has_image_write
        and has_storage_image
        and CAP_STORAGE_IMAGE_WRITE_WITHOUT_FORMAT not in existing_capabilities
    ):
        capabilities_to_add.append(CAP_STORAGE_IMAGE_WRITE_WITHOUT_FORMAT)

    # Nothing to do?
    if (
        image_format_patches == 0
        and not duplicate_to_canonical
        and not capabilities_to_add
    ):
        print(f"spv_strip_image_format: {path}: no changes")
        return

    # Pass 4: rebuild the module in one sweep:
    #   - copy each instruction; for every operand word, rewrite duplicate ids
    #     to their canonical id (but never the OpTypeImage's own result_id —
    #     that would collapse a kept canonical onto its own duplicate count
    #     and confuse the drop logic);
    #   - drop the duplicate OpTypeImage entries entirely;
    #   - drop OpName / OpMemberName instructions that target a duplicate id
    #     (SPIR-V already has the canonical's own debug names);
    #   - splice any new OpCapability instructions in immediately after the
    #     last existing OpCapability — SPIR-V requires the Capability section
    #     before all other instructions.
    last_capability_end = HEADER_WORDS * 4
    for offset, opcode, word_count in parse_instructions(data):
        if opcode == OP_CAPABILITY:
            last_capability_end = offset + word_count * 4

    capability_blob = bytearray()
    for cap in capabilities_to_add:
        # OpCapability is opcode 17 with word_count 2: [header, capability].
        capability_blob += struct.pack("<II", (2 << 16) | OP_CAPABILITY, cap)

    out = bytearray(data[:HEADER_WORDS * 4])
    capabilities_inserted = False

    for offset, opcode, word_count in parse_instructions(data):
        if not capabilities_inserted and offset >= last_capability_end:
            out += capability_blob
            capabilities_inserted = True

        # Drop duplicates and their debug names by their *original* id so we
        # don't have to reason about a remapped header.
        if opcode == OP_TYPE_IMAGE and word_count >= 2:
            result_id = struct.unpack_from("<I", data, offset + 1 * 4)[0]
            if result_id in duplicate_to_canonical:
                continue
        elif opcode in (OP_NAME, OP_MEMBER_NAME) and word_count >= 2:
            target_id = struct.unpack_from("<I", data, offset + 1 * 4)[0]
            if target_id in duplicate_to_canonical:
                continue

        # Copy the instruction, remapping operand ids. The result_id slot of
        # an OpTypeImage we keep is canonical by construction and must not
        # change.
        instr = bytearray(data[offset : offset + word_count * 4])
        if duplicate_to_canonical:
            for i in range(1, word_count):
                if opcode == OP_TYPE_IMAGE and i == 1:
                    continue
                word_offset = i * 4
                word = struct.unpack_from("<I", instr, word_offset)[0]
                canonical = duplicate_to_canonical.get(word)
                if canonical is not None:
                    struct.pack_into("<I", instr, word_offset, canonical)
        out += instr

    if not capabilities_inserted:
        out += capability_blob

    with open(path, "wb") as f:
        f.write(out)

    print(
        f"spv_strip_image_format: {path}: patched {image_format_patches} "
        f"OpTypeImage Format(s), deduped {len(duplicate_to_canonical)} type(s), "
        f"added capabilities: {capabilities_to_add}"
    )


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: spv_strip_image_format.py <file.spv> [<file.spv> ...]")

    for path in argv[1:]:
        patch(path)


if __name__ == "__main__":
    main(sys.argv)
