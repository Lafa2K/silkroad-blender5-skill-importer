bl_info = {
    "name": "Silkroad Skill/VFX Importer",
    "author": "Codex",
    "version": (0, 2, 0),
    "blender": (5, 0, 0),
    "location": "View3D Sidebar > Silkroad",
    "description": "Imports Silkroad Online BSR/BMS/BSK/BAN assets and EFP skill effects.",
    "category": "Import-Export",
}

import json
import math
import os
import re
import struct
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, OperatorFileListElement, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion, Vector


DEFAULT_GAME_ROOT = r"D:\claude\silkroad"
DEFAULT_CHARACTER_BSR = r"Data\res\char\china\chinaman_adventurer.bsr"
DEFAULT_CONFIG_NAME = "silkroad_importer_config.json"
CP949 = "cp949"
NONE_TOKENS = {"", "none", "xxx", "null", "0"}
CHARACTER_ATTACHMENT_BONES = {"Bip01 R Hand", "Bip01 L Hand", "Bip01 R HandMid", "Bip01 L HandMid", "Bip01 R HandMid2", "Bip01 L HandMid2", "Bip01 Spine", "Bip01 Spine1"}
CHARACTER_COLLECTION = "Silkroad Character"
WEAPON_COLLECTION = "Silkroad Weapon"
EFFECT_COLLECTION = "Silkroad Skill Effects"
ATTACH_ROTATION_PRESET_DEGREES = (
    (("shield",), (0.0, 0.0, 180.0)),
    (("tblade", "twohand", "two_hand"), (0.0, 0.0, -90.0)),
    (("sword", "blade"), (0.0, 0.0, -90.0)),
)
DEFAULT_IMPORTER_CONFIG = {
    "characters": [
        {"id": "china_male_adventurer", "name": "China Male Adventurer", "path": r"Data\res\char\china\chinaman_adventurer.bsr", "race": "china", "gender": "male"},
        {"id": "china_female_adventurer", "name": "China Female Adventurer", "path": r"Data\res\char\china\chinawoman_adventurer.bsr", "race": "china", "gender": "female"},
    ],
    "weapons": {
        "china": {
            "sword": [{"id": "sword_15", "name": "Sword 15", "path": r"Data\res\item\china\weapon\sword_15.bsr"}],
            "blade": [{"id": "blade_15", "name": "Blade 15", "path": r"Data\res\item\china\weapon\blade_15.bsr"}],
            "spear": [{"id": "spear_50", "name": "Spear 50", "path": r"Data\res\item\china\weapon\spear_50.bsr"}],
            "tblade": [{"id": "tblade_14", "name": "Two-Hand Blade 14", "path": r"Data\res\item\china\weapon\tblade_14.bsr"}],
            "bow": [{"id": "bow_15", "name": "Bow 15", "path": r"Data\res\item\china\weapon\bow_15.bsr"}],
            "shield": [{"id": "shield_15", "name": "Shield 15", "path": r"Data\res\item\china\shield\shield_15.bsr"}],
        }
    },
}
_IMPORTER_CONFIG_CACHE = {}
_SKILL_EFFECT_CACHE = {}
_ENUM_ITEMS_CACHE = {}

SR_TO_BLENDER = Matrix(((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)))
BLENDER_TO_SR = SR_TO_BLENDER.inverted()
# Silkroad bones point down their local X axis. Blender bones point down
# local Y, so matrices assigned to Edit/Pose bones need this local remap.
BLENDER_BONE_TO_SR_BONE = Matrix(((0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)))


class SilkroadError(Exception):
    pass


class BSReader:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.pos = 0

    def tell(self):
        return self.pos

    def seek(self, pos):
        if pos < 0 or pos > len(self.data):
            raise SilkroadError(f"Invalid seek {pos} in {self.path}")
        self.pos = pos

    def skip(self, count):
        self.seek(self.pos + count)

    def read(self, count):
        end = self.pos + count
        if end > len(self.data):
            raise SilkroadError(f"Unexpected EOF in {self.path}")
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def unpack(self, fmt):
        size = struct.calcsize(fmt)
        chunk = self.read(size)
        return struct.unpack(fmt, chunk)

    def u8(self):
        return self.unpack("<B")[0]

    def bool(self):
        return self.u8() != 0

    def i16(self):
        return self.unpack("<h")[0]

    def u16(self):
        return self.unpack("<H")[0]

    def i32(self):
        return self.unpack("<i")[0]

    def u32(self):
        return self.unpack("<I")[0]

    def f32(self):
        return self.unpack("<f")[0]

    def fixed_string(self, count):
        return self.read(count).decode("ascii", errors="replace")

    def string(self):
        length = self.i32()
        if length < 0 or length > 65536:
            raise SilkroadError(f"Invalid string length {length} at {self.pos - 4} in {self.path}")
        return self.read(length).decode(CP949, errors="replace")

    def vec2(self):
        return self.f32(), self.f32()

    def vec3(self):
        return self.f32(), self.f32(), self.f32()

    def vec4(self):
        return self.f32(), self.f32(), self.f32(), self.f32()

    def quat_xyzw(self):
        return self.f32(), self.f32(), self.f32(), self.f32()

    def color32(self):
        b, g, r, a = self.unpack("<BBBB")
        return r / 255.0, g / 255.0, b / 255.0, a / 255.0

    def color4(self):
        return self.f32(), self.f32(), self.f32(), self.f32()

    def matrix4(self):
        vals = self.unpack("<16f")
        return Matrix((vals[0:4], vals[4:8], vals[8:12], vals[12:16]))


@dataclass
class BmsVertex:
    position: tuple
    normal: tuple
    uv: tuple


@dataclass
class BmsMesh:
    name: str
    material_name: str
    vertices: list
    faces: list
    bone_names: list = field(default_factory=list)
    weights: list = field(default_factory=list)
    lightmap_path: str = ""


@dataclass
class BskBone:
    name: str
    parent: str
    rot_parent: tuple
    trans_parent: tuple
    rot_origin: tuple
    trans_origin: tuple
    rot_local: tuple
    trans_local: tuple
    children: list
    bone_type: int = 0


@dataclass
class BskSkeleton:
    bones: list


@dataclass
class BanTrack:
    bone_name: str
    frames: list


@dataclass
class BanAnimation:
    name: str
    duration_ms: int
    fps: int
    anim_type: int
    key_times: list
    tracks: dict


@dataclass
class BmtMaterial:
    name: str
    diffuse: tuple
    ambient: tuple
    specular: tuple
    emissive: tuple
    flags: int
    diffuse_map: str
    normal_map: str = ""
    base_dir: Path = None


@dataclass
class BsrResource:
    path: Path
    name: str
    object_type: int
    flags: list
    material_paths: list
    mesh_paths: list
    animation_paths: list
    skeleton_path: str
    attachment_bone: str
    mesh_groups: dict
    animation_groups: dict


@dataclass
class ParsedMeshAsset:
    raw_path: str
    path: Path
    mesh: BmsMesh


@dataclass
class EfpMeshRef:
    path: str
    textures: list


@dataclass
class EfpResource:
    two_sided: int = 0
    src_blend: int = 5
    dst_blend: int = 6
    meshes: list = field(default_factory=list)


@dataclass
class EfpSource:
    command: str = ""
    source_type: int = 0
    byte1: int = 0
    start: float = 0.0
    end: float = 0.0
    value: object = None


@dataclass
class EfpObject:
    name: str
    controllers: list
    global_params: list
    emitter_commands: list
    lifetime_command: EfpSource
    program_commands: list
    view_command: EfpSource
    render_command: EfpSource
    render_commands: list
    resource: EfpResource
    children: list
    int0: int = 0
    int1: int = 0
    int2: int = 0
    int3: int = 0


@dataclass
class EfpEffect:
    path: Path
    version: int
    scale: float
    root: EfpObject


@dataclass
class SkillCueInfo:
    action: str
    sequence: int
    attach_type: str
    move_type: str
    move_params: list
    resource_path: str
    bone_name: str
    start_offset: tuple
    target_offset: tuple
    launch_offset: tuple
    linked_effect_path: str
    delay_ms: int = 0
    raw_line: str = ""


@dataclass
class SkillEffectInfo:
    code: str
    animation: str
    weapon_group: str
    efp_paths: list
    texture_paths: list
    raw_line: str
    cues: list = field(default_factory=list)


def sr_vec_to_blender(value):
    x, y, z = value
    return Vector((x, -z, y))


def sr_quat_to_blender(value):
    x, y, z, w = value
    q = Quaternion((w, x, y, z))
    mat = q.to_matrix()
    converted = SR_TO_BLENDER @ mat @ BLENDER_TO_SR
    return converted.to_quaternion()


def sr_quat_matrix(value):
    x, y, z, w = value
    return Quaternion((w, x, y, z)).to_matrix()


def sr_transform_matrix(quat_xyzw, trans_xyz):
    mat = sr_quat_matrix(quat_xyzw).to_4x4()
    mat.translation = Vector(trans_xyz)
    return mat


def sr_bone_matrix_to_blender(sr_matrix):
    rot = SR_TO_BLENDER @ sr_matrix.to_3x3() @ BLENDER_BONE_TO_SR_BONE
    mat = rot.to_4x4()
    mat.translation = sr_vec_to_blender(sr_matrix.translation)
    return mat


def skeleton_global_rest_matrices(skeleton):
    matrices = {}
    for bone in skeleton.bones:
        local = sr_transform_matrix(bone.rot_parent, bone.trans_parent)
        matrices[bone.name] = matrices[bone.parent] @ local if bone.parent and bone.parent in matrices else local
    return matrices


def safe_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9_. -]+", "_", str(name)).strip()
    return cleaned or "Silkroad"


def config_id(value):
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(value).strip().lower()).strip("_")
    return cleaned or "entry"


def copy_default_config():
    return json.loads(json.dumps(DEFAULT_IMPORTER_CONFIG))


def importer_config_path(game_root):
    root = Path(game_root or DEFAULT_GAME_ROOT)
    tools_dir = root / "tools"
    try:
        tools_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        tools_dir = Path(__file__).resolve().parent
    return tools_dir / DEFAULT_CONFIG_NAME


def file_signature(path):
    try:
        stat = Path(path).stat()
        return stat.st_mtime_ns, stat.st_size
    except Exception:
        return None


def cached_enum_items(key, items):
    _ENUM_ITEMS_CACHE[key] = items
    return items


def normalize_config_records(records):
    result = []
    seen = set()
    for index, record in enumerate(records or []):
        if not isinstance(record, dict):
            continue
        item = dict(record)
        item["id"] = config_id(item.get("id") or item.get("name") or item.get("path") or index)
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        item["name"] = str(item.get("name") or item["id"])
        item["path"] = str(item.get("path") or "")
        result.append(item)
    return result


def load_importer_config(game_root, force=False):
    path = importer_config_path(game_root)
    cache_key = str(path).lower()
    signature = file_signature(path)
    cached = _IMPORTER_CONFIG_CACHE.get(cache_key)
    if not force and cached and cached.get("signature") == signature:
        return cached["config"]

    config = copy_default_config()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception as exc:
            print(f"Silkroad importer: config read failed {path}: {exc}")
    else:
        save_importer_config(game_root, config)
    config["characters"] = normalize_config_records(config.get("characters", []))
    weapons = config.get("weapons", {})
    if not isinstance(weapons, dict):
        weapons = {}
    for race, groups in list(weapons.items()):
        if not isinstance(groups, dict):
            weapons[race] = {}
            continue
        for group_name, records in list(groups.items()):
            groups[group_name] = normalize_config_records(records)
    config["weapons"] = weapons
    _IMPORTER_CONFIG_CACHE[cache_key] = {"signature": file_signature(path), "config": config}
    return config


def save_importer_config(game_root, config):
    path = importer_config_path(game_root)
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
    except Exception as exc:
        print(f"Silkroad importer: config write failed {path}: {exc}")
    _IMPORTER_CONFIG_CACHE[str(path).lower()] = {"signature": file_signature(path), "config": config}
    _ENUM_ITEMS_CACHE.clear()
    return path


def config_path_to_absolute(game_root, raw_path):
    path = Path(str(raw_path or ""))
    if path.is_absolute():
        return path
    return Path(game_root or DEFAULT_GAME_ROOT) / normalized_relpath(str(raw_path))


def is_none_path(value):
    return not value or value.strip().lower() in NONE_TOKENS


def normalized_relpath(value):
    return value.strip().replace("/", os.sep).replace("\\", os.sep)


def resolve_with_stem_aliases(resolver, raw_path, aliases=None, base_dir=None, prefer=None, allow_search=True):
    path = resolver.resolve(raw_path, base_dir=base_dir, prefer=prefer, allow_search=allow_search)
    if path:
        return path
    if is_none_path(raw_path):
        return None

    rel = Path(normalized_relpath(raw_path.strip().strip('"')))
    suffix = rel.suffix
    parent = rel.parent
    for alias in aliases or []:
        if not alias:
            continue
        alias_rel = parent / f"{alias}{suffix}"
        path = resolver.resolve(str(alias_rel), base_dir=base_dir, prefer=prefer, allow_search=allow_search)
        if path:
            print(f"Silkroad importer: missing {raw_path}; using {alias_rel}")
            return path
    return None


class AssetResolver:
    def __init__(self, game_root):
        self.root = Path(game_root)
        self.search_cache = {}
        self.ddj_cache = Path(tempfile.gettempdir()) / "silkroad_ddj_cache"
        self.ddj_cache.mkdir(parents=True, exist_ok=True)

    def resolve(self, raw_path, base_dir=None, prefer=None, allow_search=True):
        if is_none_path(raw_path):
            return None

        raw_path = raw_path.strip().strip('"')
        direct = Path(raw_path)
        if direct.is_absolute() and direct.exists():
            return direct

        rel = Path(normalized_relpath(raw_path))
        candidates = []
        if base_dir:
            candidates.append(Path(base_dir) / rel)
            if len(rel.parts) == 1:
                candidates.append(Path(base_dir) / rel.name)

        prefixes = []
        if prefer:
            prefixes.append(prefer)
        prefixes.extend(["", "Data", "Particles", "Media"])
        for prefix in prefixes:
            candidates.append(self.root / prefix / rel if prefix else self.root / rel)

        for candidate in candidates:
            if candidate.exists():
                return candidate

        if allow_search:
            key = raw_path.lower()
            if key in self.search_cache:
                return self.search_cache[key]
            name = rel.name
            for top in ("Data", "Particles", "Media"):
                root = self.root / top
                if root.exists():
                    found = next(root.rglob(name), None)
                    if found:
                        self.search_cache[key] = found
                        return found

        return None

    def loadable_texture(self, raw_path, base_dir=None):
        path = self.resolve(raw_path, base_dir=base_dir, allow_search=True)
        if not path:
            return None
        if path.suffix.lower() != ".ddj":
            return path
        return self.extract_ddj(path)

    def extract_ddj(self, path):
        reader = BSReader(path)
        signature = reader.fixed_string(12)
        if signature != "JMXVDDJ 1000":
            return path
        size = reader.i32()
        reader.i32()
        remaining = len(reader.data) - reader.tell()
        payload = reader.read(min(size, remaining))
        if not payload.startswith(b"DDS "):
            raise SilkroadError(f"{path} does not contain a DDS payload")
        digest = f"{path.stem}_{path.stat().st_mtime_ns & 0xffffffff:x}_{size:x}.dds"
        out_path = self.ddj_cache / safe_name(digest)
        if not out_path.exists() or out_path.stat().st_size != len(payload):
            out_path.write_bytes(payload)
        return out_path


def parse_bms(path):
    reader = BSReader(path)
    signature = reader.fixed_string(12)
    if not signature.startswith("JMXVBMS"):
        raise SilkroadError(f"{path} is not a JMXVBMS mesh")

    offsets = [reader.u32() for _ in range(12)]
    reader.u32()
    vertex_flag = reader.u32()
    reader.u32()
    name = reader.string()
    material_name = reader.string()
    reader.u32()

    vertices = []
    lightmap_path = ""
    vertex_offset, skin_offset, face_offset = offsets[0], offsets[1], offsets[2]

    reader.seek(vertex_offset)
    vertex_count = reader.u32()
    for _ in range(vertex_count):
        position = reader.vec3()
        normal = reader.vec3()
        uv = reader.vec2()
        if vertex_flag & 0x400:
            reader.vec2()
        if vertex_flag & 0x800:
            reader.skip(36)
        reader.f32()
        reader.u32()
        reader.u32()
        vertices.append(BmsVertex(position, normal, uv))

    if vertex_flag & 0x400:
        lightmap_path = reader.string()
    if vertex_flag & 0x1000:
        extra_count = reader.u32()
        reader.skip(extra_count * 24)

    bone_names = []
    weights = [[] for _ in range(vertex_count)]
    if skin_offset:
        reader.seek(skin_offset)
        bone_count = reader.u32()
        if bone_count > 0:
            bone_names = [reader.string() for _ in range(bone_count)]
            for vertex_index in range(vertex_count):
                b0 = reader.u8()
                w0 = reader.u16()
                b1 = reader.u8()
                w1 = reader.u16()
                if b0 != 255 and b0 < len(bone_names) and w0 > 0:
                    weights[vertex_index].append((bone_names[b0], w0 / 65535.0))
                if b1 != 255 and b1 < len(bone_names) and w1 > 0:
                    weights[vertex_index].append((bone_names[b1], w1 / 65535.0))

    faces = []
    if face_offset:
        reader.seek(face_offset)
        face_count = reader.u32()
        for _ in range(face_count):
            faces.append((reader.u16(), reader.u16(), reader.u16()))

    return BmsMesh(name, material_name, vertices, faces, bone_names, weights, lightmap_path)


def parse_bsk(path):
    reader = BSReader(path)
    signature = reader.fixed_string(12)
    if not signature.startswith("JMXVBSK"):
        raise SilkroadError(f"{path} is not a JMXVBSK skeleton")

    bones = []
    bone_count = reader.u32()
    for _ in range(bone_count):
        bone_type = reader.u8()
        name = reader.string()
        parent = reader.string()
        rot_parent = reader.quat_xyzw()
        trans_parent = reader.vec3()
        rot_origin = reader.quat_xyzw()
        trans_origin = reader.vec3()
        rot_local = reader.quat_xyzw()
        trans_local = reader.vec3()
        child_count = reader.u32()
        children = [reader.string() for _ in range(child_count)]
        bones.append(BskBone(name, parent, rot_parent, trans_parent, rot_origin, trans_origin, rot_local, trans_local, children, bone_type))
    return BskSkeleton(bones)


def parse_ban(path):
    reader = BSReader(path)
    signature = reader.fixed_string(12)
    if not signature.startswith("JMXVBAN"):
        raise SilkroadError(f"{path} is not a JMXVBAN animation")

    reader.i32()
    reader.i32()
    name = reader.string()
    duration_ms = reader.i32()
    fps = reader.i32()
    anim_type = reader.i32()
    key_count = reader.u32()
    key_times = [reader.u32() for _ in range(key_count)]
    track_count = reader.u32()
    tracks = {}
    for _ in range(track_count):
        bone_name = reader.string()
        frame_count = reader.u32()
        frames = []
        for _ in range(frame_count):
            frames.append((reader.quat_xyzw(), reader.vec3()))
        tracks[bone_name] = BanTrack(bone_name, frames)
    return BanAnimation(name, duration_ms, fps, anim_type, key_times, tracks)


def parse_bmt(path):
    reader = BSReader(path)
    signature = reader.fixed_string(12)
    if signature != "JMXVBMT 0102":
        raise SilkroadError(f"{path} is not a supported JMXVBMT material file")

    materials = {}
    count = reader.i32()
    for _ in range(count):
        name = reader.string()
        diffuse = reader.color4()
        ambient = reader.color4()
        specular = reader.color4()
        emissive = reader.color4()
        reader.f32()
        flags = reader.u32()
        diffuse_map = reader.string()
        reader.f32()
        reader.u8()
        reader.u8()
        reader.bool()
        normal_map = ""
        if flags & (1 << 14):
            normal_map = reader.string()
            reader.u32()
        materials[name] = BmtMaterial(name, diffuse, ambient, specular, emissive, flags, diffuse_map, normal_map, Path(path).parent)
    return materials


def skip_bbox(reader):
    reader.vec3()
    reader.vec3()


def parse_bsr(path):
    reader = BSReader(path)
    signature = reader.fixed_string(12)
    if signature != "JMXVRES 0109":
        raise SilkroadError(f"{path} is not a supported JMXVRES resource")

    reader.skip(32)
    flags = [reader.i32() for _ in range(5)]
    object_type = reader.i16()
    reader.i16()
    name = reader.string()
    reader.i32()
    reader.i32()
    reader.skip(40)

    reader.string()
    skip_bbox(reader)
    skip_bbox(reader)
    if reader.u32() != 0:
        reader.matrix4()

    material_paths = []
    for _ in range(reader.i32()):
        reader.i32()
        material_paths.append(reader.string())

    mesh_paths = []
    for _ in range(reader.i32()):
        mesh_paths.append(reader.string())
        if flags[0] & 1:
            reader.i32()

    reader.u32()
    reader.u32()
    animation_paths = [reader.string() for _ in range(reader.i32())]

    skeleton_path = ""
    attachment_bone = ""
    if reader.u32() != 0:
        skeleton_path = reader.string()
        attachment_bone = reader.string()

    mesh_groups = {}
    for _ in range(reader.i32()):
        group_name = reader.string()
        mesh_groups[group_name] = [reader.i32() for _ in range(reader.i32())]

    animation_groups = {}
    for _ in range(reader.i32()):
        group_name = reader.string()
        group = {}
        for _ in range(reader.i32()):
            anim_type = reader.i32()
            anim_index = reader.i32()
            event_count = reader.i32()
            reader.skip(event_count * 16)
            walk_count = reader.i32()
            reader.f32()
            reader.skip(walk_count * 8)
            group[anim_type] = anim_index
        animation_groups[group_name] = group

    return BsrResource(Path(path), name, object_type, flags, material_paths, mesh_paths, animation_paths, skeleton_path, attachment_bone, mesh_groups, animation_groups)


def read_blend(reader, value_kind):
    begin = reader.f32()
    end = reader.f32()
    count = reader.i32()
    points = []
    for _ in range(count):
        time_value = reader.f32()
        if value_kind == "float":
            value = reader.f32()
        elif value_kind == "byte":
            value = reader.u8()
        elif value_kind == "color":
            value = reader.color32()
        elif value_kind == "vector":
            value = reader.vec3()
        else:
            value = None
        points.append((time_value, value))
    return {"begin": begin, "end": end, "points": points}


def read_static_emit(reader):
    return {
        "min": reader.u32(),
        "max": reader.u32(),
        "burst_rate": reader.u32(),
        "min_particles": reader.u32(),
        "spawn_rate": reader.f32(),
    }


PARAM_BY_COMMAND = {
    "StaticEmit": "EFStaticEmit",
    "Attraction": "float",
    "SetPosition": "Vector",
    "SetSpherePos": "Vector",
    "SetVelocity": "Vector",
    "Force": "Vector",
    "SetRotationMat": "Matrix",
    "SetRVelocityMat": "Matrix",
    "SetRotation": "RotVector",
    "SetRVelocity": "RotVector",
    "SetRotationAxis": "AxisVector4",
    "SetRVelocityAxis": "AxisVector4",
    "SetShapeRot": "AxisVector4",
    "SetShapeRotVel": "AxisVector4",
    "SetConePos": "AngleVector1",
    "SetConeVel": "AngleVector1",
    "ConeForce": "AngleVector1",
    "SetGraphScale": "FrameScale",
    "SetGraphRandomScale": "BlendScaleGraphPointer",
    "SetGraphDiffuse": "FrameDiffuse",
    "SetBANRot": "FrameBANRotation",
    "SetBANPos": "FrameBANPosition",
    "TextureSlide": "FrameTextureSlide",
}


def read_parameter(reader, name):
    if name in {"float", "BlendScaleGraphPointer"}:
        return reader.f32()
    if name in {"Vector", "Vector3"}:
        return reader.vec3()
    if name == "Matrix":
        return reader.matrix4()
    if name in {"SEFStaticEmit", "EFStaticEmit"}:
        return read_static_emit(reader)
    if name == "AxisVector4":
        return {"left": reader.vec4(), "matrix": reader.matrix4()}
    if name == "RotVector":
        return {"left": reader.vec3(), "matrix": reader.matrix4()}
    if name == "AngleVector1":
        return {"left": reader.vec3(), "right": reader.vec3()}
    if name == "FrameScale":
        return [reader.vec3() for _ in range(reader.i32())]
    if name == "BlendScaleGraph":
        return read_blend(reader, "vector")
    if name == "FrameDiffuse":
        return [reader.color32() for _ in range(reader.i32())]
    if name == "BlendDiffuseGraph":
        return read_blend(reader, "color")
    if name == "FrameBANRotation":
        left = reader.f32()
        return {"left": left, "frames": [reader.matrix4() for _ in range(reader.i32())]}
    if name == "FrameBANPosition":
        left = reader.f32()
        return {"left": left, "frames": [reader.vec3() for _ in range(reader.i32())]}
    if name == "BSAnimation":
        return [reader.string() for _ in range(reader.i32())]
    if name == "FrameTextureSlide":
        left = reader.vec3()
        return {"left": left, "frames": [reader.vec4() for _ in range(reader.i32())]}
    raise SilkroadError(f"Unknown EFP parameter {name}")


def read_source(reader):
    if not reader.bool():
        return EfpSource()
    command = reader.string()
    source_type = reader.u8()
    byte1 = reader.u8()
    start = reader.f32()
    end = reader.f32()
    float2 = reader.f32()
    param_name = PARAM_BY_COMMAND.get(command)
    value = read_parameter(reader, param_name) if param_name else None
    return EfpSource(command, source_type, byte1, start, end, value)


def read_source_list(reader):
    return [read_source(reader) for _ in range(reader.i32())]


def read_resource(reader):
    resource = EfpResource()
    resource.two_sided = reader.u32()
    resource.src_blend = reader.i32()
    resource.dst_blend = reader.i32()
    reader.i32()
    reader.i32()
    reader.i32()
    reader.i32()
    reader.i32()
    reader.i32()
    for _ in range(reader.i32()):
        mesh_path = reader.string()
        textures = [reader.string() for _ in range(reader.i32())]
        resource.meshes.append(EfpMeshRef(mesh_path, textures))
    return resource


def read_controller(reader, name):
    if name in {"NormalTimeLife", "NormalTimeLoopLife"}:
        return {"name": name}
    if name == "StaticEmit":
        return {"name": name, "value": read_static_emit(reader)}
    if name == "Program":
        return {"name": name, "sources": read_source_list(reader)}
    if name == "LinkMode":
        return {"name": name, "values": [reader.u32(), reader.u32(), reader.u32(), reader.u32()]}
    if name == "BAN":
        return {"name": name, "animations": [reader.string() for _ in range(reader.i32())]}
    if name == "ViewMode":
        return {"name": name, "mode": reader.string()}
    if name == "Shape":
        return {"name": name, "shape": reader.string(), "resource": read_resource(reader)}
    if name == "ScaleGraph":
        return {
            "name": name,
            "x": read_blend(reader, "float"),
            "y": read_blend(reader, "float"),
            "z": read_blend(reader, "float"),
            "float0": reader.f32(),
            "float1": reader.f32(),
        }
    if name == "DiffuseGraph":
        return {"name": name, "byte": read_blend(reader, "byte"), "color": read_blend(reader, "color")}
    raise SilkroadError(f"Unknown EFP controller {name}")


def read_global_data(reader):
    reader.i32()
    params = []
    for _ in range(reader.i32()):
        name = reader.string()
        params.append({"name": name, "value": read_parameter(reader, name)})
    return params


def read_efp_object(reader):
    reader.i32()
    name = reader.string()
    controllers = []
    for _ in range(reader.i32()):
        controller_name = reader.string()
        controllers.append(read_controller(reader, controller_name))

    global_params = read_global_data(reader)
    read_source_list(reader)
    emitter_commands = read_source_list(reader)
    read_source_list(reader)
    lifetime_command = read_source(reader)
    program_commands = read_source_list(reader)

    reader.u8()
    reader.u8()
    int0 = reader.i32()
    int1 = reader.i32()
    int2 = reader.i32()
    reader.u8()
    int3 = reader.i32()
    reader.u8()
    view_command = read_source(reader)
    resource = read_resource(reader)
    render_command = read_source(reader)
    read_source_list(reader)
    render_commands = read_source_list(reader)

    children = [read_efp_object(reader) for _ in range(reader.i32())]
    return EfpObject(name, controllers, global_params, emitter_commands, lifetime_command, program_commands, view_command, render_command, render_commands, resource, children, int0, int1, int2, int3)


def parse_efp(path):
    reader = BSReader(path)
    signature = reader.fixed_string(8)
    if signature != "JMXVEFF ":
        raise SilkroadError(f"{path} is not a JMXVEFF effect")
    version = int(reader.fixed_string(4))
    scale = 1.0
    if version in (12, 13):
        scale = reader.f32()
    if version == 13:
        reader.i32()
        reader.i32()
        reader.i32()
    return EfpEffect(Path(path), version, scale, read_efp_object(reader))


def make_material(name, resolver, material_info=None, texture_path=None, base_color=(0.8, 0.8, 0.8, 1.0), alpha=True):
    mat = bpy.data.materials.new(safe_name(name))
    mat.use_nodes = True
    mat.blend_method = "BLEND" if alpha else "OPAQUE"
    if hasattr(mat, "use_screen_refraction"):
        mat.use_screen_refraction = False
    if hasattr(mat, "show_transparent_back"):
        mat.show_transparent_back = True

    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    if not bsdf:
        return mat

    color = base_color
    if material_info:
        color = material_info.diffuse

    if bsdf.inputs.get("Base Color"):
        bsdf.inputs["Base Color"].default_value = color
    if bsdf.inputs.get("Alpha"):
        bsdf.inputs["Alpha"].default_value = (color[3] if len(color) > 3 else 1.0) if alpha else 1.0

    raw_texture = texture_path or (material_info.diffuse_map if material_info else "")
    base_dir = material_info.base_dir if material_info else None
    tex = None
    if raw_texture:
        try:
            tex = resolver.loadable_texture(raw_texture, base_dir=base_dir)
        except Exception as exc:
            print(f"Silkroad importer: could not extract texture {raw_texture}: {exc}")
    if tex and tex.exists():
        try:
            image = bpy.data.images.load(str(tex), check_existing=True)
            tex_node = nodes.new(type="ShaderNodeTexImage")
            tex_node.image = image
            tex_node.interpolation = "Linear"
            mat.node_tree.links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
            if alpha and "Alpha" in tex_node.outputs and bsdf.inputs.get("Alpha"):
                mat.node_tree.links.new(tex_node.outputs["Alpha"], bsdf.inputs["Alpha"])
        except Exception:
            print(f"Silkroad importer: could not load texture {tex}")

    return mat


def infer_material_path_for_mesh(mesh_path, material_name):
    parts = list(Path(mesh_path).parts)
    try:
        mesh_index = parts.index("mesh")
    except ValueError:
        return None
    parts[mesh_index] = "mtrl"
    return Path(*parts).with_name(f"{material_name}.bmt")


def create_mesh_object(mesh_data, name, resolver, material_lookup, armature=None, collection=None, flip_uv=True, flip_winding=True):
    positions = [sr_vec_to_blender(v.position) for v in mesh_data.vertices]
    vertex_count = len(positions)
    faces = []
    skipped_faces = 0
    for a, b, c in mesh_data.faces:
        if a >= vertex_count or b >= vertex_count or c >= vertex_count:
            skipped_faces += 1
            continue
        faces.append((a, c, b) if flip_winding else (a, b, c))
    mesh = bpy.data.meshes.new(safe_name(name) + "_Mesh")
    mesh.from_pydata(positions, [], faces)
    mesh.update()

    if mesh_data.vertices:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for poly in mesh.polygons:
            poly.use_smooth = True
            for loop_index in poly.loop_indices:
                vertex_index = mesh.loops[loop_index].vertex_index
                u, v = mesh_data.vertices[vertex_index].uv
                uv_layer.data[loop_index].uv = (u, 1.0 - v if flip_uv else v)

    obj = bpy.data.objects.new(safe_name(name), mesh)
    (collection or bpy.context.collection).objects.link(obj)

    material_info = material_lookup.get(mesh_data.material_name)
    mat = make_material(mesh_data.material_name or name, resolver, material_info=material_info, alpha=False)
    obj.data.materials.append(mat)

    if mesh_data.bone_names:
        groups = {bone: obj.vertex_groups.new(name=bone) for bone in mesh_data.bone_names}
        for vertex_index, entries in enumerate(mesh_data.weights):
            for bone_name, weight in entries:
                if weight > 0.0 and bone_name in groups:
                    groups[bone_name].add([vertex_index], weight, "ADD")
        if armature:
            modifier = obj.modifiers.new("Silkroad Armature", "ARMATURE")
            modifier.object = armature
            obj.parent = armature

    obj["silkroad_material"] = mesh_data.material_name
    if skipped_faces:
        obj["silkroad_skipped_faces"] = skipped_faces
    return obj


def create_armature(skeleton, name, collection=None, only_bones=None):
    arm_data = bpy.data.armatures.new(safe_name(name) + "_Armature")
    arm_obj = bpy.data.objects.new(safe_name(name) + "_Armature", arm_data)
    (collection or bpy.context.collection).objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")

    edit_bones = arm_data.edit_bones
    all_bones = {bone.name: bone for bone in skeleton.bones}
    include_bones = None
    if only_bones:
        include_bones = set(only_bones)
        for bone_name in list(include_bones):
            parent = all_bones.get(bone_name).parent if bone_name in all_bones else ""
            while parent and parent in all_bones:
                include_bones.add(parent)
                parent = all_bones[parent].parent

    selected_bones = [bone for bone in skeleton.bones if include_bones is None or bone.name in include_bones]
    selected_names = {bone.name for bone in selected_bones}
    rest_matrices = skeleton_global_rest_matrices(skeleton)
    origin_positions = {bone.name: sr_vec_to_blender(rest_matrices[bone.name].translation) for bone in selected_bones}

    for bone in selected_bones:
        eb = edit_bones.new(bone.name)
        head = origin_positions.get(bone.name, Vector((0, 0, 0)))
        rest = sr_bone_matrix_to_blender(rest_matrices[bone.name])
        direction = (rest.to_3x3() @ Vector((0, 1, 0))).normalized()
        roll_axis = (rest.to_3x3() @ Vector((0, 0, 1))).normalized()
        child_lengths = [
            (origin_positions[child] - head).length
            for child in bone.children
            if child in selected_names and child in origin_positions and (origin_positions[child] - head).length > 0.001
        ]
        length = child_lengths[0] if child_lengths else 0.18
        tail = head + direction * max(0.08, length)
        if (tail - head).length < 0.001:
            tail = head + Vector((0, 0.18, 0))
        eb.head = head
        eb.tail = tail
        try:
            eb.align_roll(roll_axis)
        except Exception:
            pass

    for bone in selected_bones:
        if bone.parent and bone.parent in edit_bones and bone.name in edit_bones:
            edit_bones[bone.name].parent = edit_bones[bone.parent]
            edit_bones[bone.name].use_connect = False

    bpy.ops.object.mode_set(mode="OBJECT")
    arm_obj.show_in_front = True
    return arm_obj


def import_bsr_to_scene(context, bsr_path, game_root, collection_name=None, flip_uv=True, flip_winding=True, target_armature=None, parent_collection=None):
    resolver = AssetResolver(game_root)
    bsr_path = Path(bsr_path)
    bsr = parse_bsr(bsr_path)
    stem_aliases = []
    if bsr.skeleton_path:
        stem_aliases.append(Path(normalized_relpath(bsr.skeleton_path)).stem)
    stem_aliases.append(bsr_path.stem)
    stem_aliases.append(bsr.name)

    collection = bpy.data.collections.new(collection_name or safe_name(bsr.name))
    (parent_collection or context.scene.collection).children.link(collection)

    material_lookup = {}
    for raw_material_path in bsr.material_paths:
        material_path = resolve_with_stem_aliases(resolver, raw_material_path, stem_aliases, prefer="Data")
        if material_path:
            try:
                material_lookup.update(parse_bmt(material_path))
            except Exception as exc:
                print(f"Silkroad importer: material parse failed {material_path}: {exc}")

    parsed_meshes = []
    used_bone_names = set()
    for raw_mesh_path in bsr.mesh_paths:
        mesh_path = resolve_with_stem_aliases(resolver, raw_mesh_path, stem_aliases, prefer="Data")
        if not mesh_path:
            print(f"Silkroad importer: mesh not found {raw_mesh_path}")
            continue
        try:
            mesh_data = parse_bms(mesh_path)
            if mesh_data.material_name and mesh_data.material_name not in material_lookup:
                material_path = infer_material_path_for_mesh(mesh_path, mesh_data.material_name)
                if material_path and material_path.exists():
                    try:
                        material_lookup.update(parse_bmt(material_path))
                    except Exception as exc:
                        print(f"Silkroad importer: inferred material parse failed {material_path}: {exc}")
            parsed_meshes.append(ParsedMeshAsset(raw_mesh_path, mesh_path, mesh_data))
            for entries in mesh_data.weights:
                for bone_name, weight in entries:
                    if weight > 0.0:
                        used_bone_names.add(bone_name)
        except Exception as exc:
            print(f"Silkroad importer: mesh parse failed {mesh_path}: {exc}")

    armature = None
    skeleton_path = resolver.resolve(bsr.skeleton_path, prefer="Data")
    if skeleton_path:
        skeleton = parse_bsk(skeleton_path)
        if bsr.object_type == 0:
            skeleton_names = {bone.name for bone in skeleton.bones}
            used_bone_names.update(name for name in CHARACTER_ATTACHMENT_BONES if name in skeleton_names)
        armature = create_armature(skeleton, bsr.name, collection, used_bone_names or None)
        armature["silkroad_root"] = str(Path(game_root))
        armature["silkroad_bsr_path"] = str(bsr_path)
        armature["silkroad_skeleton_path"] = str(skeleton_path)
        armature["silkroad_attachment_bone"] = bsr.attachment_bone
        armature["silkroad_animation_paths"] = json.dumps(bsr.animation_paths)
        armature["silkroad_animation_groups"] = json.dumps({k: {str(t): v for t, v in g.items()} for k, g in bsr.animation_groups.items()})
    elif target_armature and target_armature.type == "ARMATURE" and used_bone_names:
        matching_bones = used_bone_names & {bone.name for bone in target_armature.pose.bones}
        if matching_bones:
            armature = target_armature
            armature["silkroad_last_attached_resource"] = str(bsr_path)

    imported = []
    for parsed in parsed_meshes:
        try:
            obj = create_mesh_object(parsed.mesh, parsed.mesh.name, resolver, material_lookup, armature, collection, flip_uv, flip_winding)
            imported.append(obj)
        except Exception as exc:
            print(f"Silkroad importer: mesh import failed {parsed.path}: {exc}")

    if armature:
        context.view_layer.objects.active = armature
        armature.select_set(True)
    return armature, imported, bsr


def frame_from_ms(ms, fps):
    return 1.0 + (float(ms) / 1000.0) * float(fps)


def skeleton_for_armature(armature):
    skeleton_path = armature.get("silkroad_skeleton_path", "")
    if skeleton_path and Path(skeleton_path).exists():
        return parse_bsk(skeleton_path)

    root = armature.get("silkroad_root", "")
    bsr_path = armature.get("silkroad_bsr_path", "")
    if root and bsr_path and Path(bsr_path).exists():
        bsr = parse_bsr(bsr_path)
        path = AssetResolver(root).resolve(bsr.skeleton_path, prefer="Data")
        if path:
            armature["silkroad_skeleton_path"] = str(path)
            return parse_bsk(path)
    return None


def validate_ban_matches_armature(armature, ban):
    pose_names = {bone.name for bone in armature.pose.bones}
    track_names = set(ban.tracks.keys())
    matched = pose_names & track_names
    if track_names and len(matched) < max(1, min(6, len(track_names) // 4)):
        sample = ", ".join(sorted(list(track_names))[:4])
        raise SilkroadError(f"BAN does not match selected armature ({len(matched)}/{len(track_names)} bones matched; tracks include {sample})")


def ban_global_matrices_for_frame(skeleton, ban, frame_index):
    matrices = {}
    for bone in skeleton.bones:
        track = ban.tracks.get(bone.name)
        if track and frame_index < len(track.frames):
            quat_xyzw, trans = track.frames[frame_index]
        else:
            quat_xyzw, trans = bone.rot_parent, bone.trans_parent
        local = sr_transform_matrix(quat_xyzw, trans)
        matrices[bone.name] = matrices[bone.parent] @ local if bone.parent and bone.parent in matrices else local
    return matrices


def set_action_timeline_metadata(action, frame_start, frame_end, fps=None, ban_path=None, ban_name=None):
    try:
        action.use_fake_user = True
    except Exception:
        pass
    action["silkroad_frame_start"] = int(frame_start)
    action["silkroad_frame_end"] = int(frame_end)
    if fps:
        action["silkroad_fps"] = int(fps)
    if ban_path:
        action["silkroad_ban_path"] = str(ban_path)
    if ban_name:
        action["silkroad_ban_name"] = str(ban_name)
    for attr, value in (("use_frame_range", True), ("frame_start", float(frame_start)), ("frame_end", float(frame_end))):
        if hasattr(action, attr):
            try:
                setattr(action, attr, value)
            except Exception:
                pass


def remember_armature_action(armature, action):
    if not armature or not action:
        return
    action["silkroad_armature"] = armature.name
    raw_names = armature.get("silkroad_imported_actions", "[]")
    try:
        names = json.loads(raw_names) if isinstance(raw_names, str) else list(raw_names)
    except Exception:
        names = []
    names = [name for name in names if isinstance(name, str) and name in bpy.data.actions]
    if action.name not in names:
        names.append(action.name)
    armature["silkroad_imported_actions"] = json.dumps(names)
    armature["silkroad_action_count"] = len(names)


def action_timeline_metadata(action):
    if not action:
        return None
    start = action.get("silkroad_frame_start")
    end = action.get("silkroad_frame_end")
    fps = action.get("silkroad_fps")
    if start is None or end is None:
        try:
            frame_range = action.frame_range
            start, end = int(math.floor(frame_range[0])), int(math.ceil(frame_range[1]))
        except Exception:
            return None
    return int(start), max(int(end), int(start) + 1), int(fps) if fps else None


def sync_scene_timeline_to_action(scene, action):
    metadata = action_timeline_metadata(action)
    if not metadata:
        return False
    start, end, fps = metadata
    if fps:
        scene.render.fps = fps
    scene.frame_start = start
    scene.frame_end = end
    if scene.frame_current < start or scene.frame_current > end:
        scene.frame_set(start)
    return True


@persistent
def silkroad_action_timeline_handler(scene, depsgraph=None):
    props = getattr(scene, "silkroad_importer", None)
    if props and not props.auto_action_timeline:
        return
    obj = bpy.context.object
    if not obj or obj.type != "ARMATURE" or not obj.animation_data:
        return
    action = obj.animation_data.action
    if not action:
        return
    sync_key = f"{obj.name_full}:{action.name_full}:{action.get('silkroad_frame_end', '')}"
    if scene.get("_silkroad_last_action_sync") == sync_key:
        return
    if sync_scene_timeline_to_action(scene, action):
        scene["_silkroad_last_action_sync"] = sync_key


def apply_ban_to_armature(context, armature, ban_path, clear_pose=True):
    if not armature or armature.type != "ARMATURE":
        raise SilkroadError("Select or import an armature before applying a BAN animation")

    ban = parse_ban(ban_path)
    validate_ban_matches_armature(armature, ban)
    skeleton = skeleton_for_armature(armature)
    scene = context.scene
    fps = max(1, ban.fps or scene.render.fps)
    scene.render.fps = fps
    scene.frame_start = 1
    scene.frame_end = max(2, math.ceil(frame_from_ms(ban.duration_ms, fps)))

    action = bpy.data.actions.new(safe_name(ban.name))
    set_action_timeline_metadata(action, scene.frame_start, scene.frame_end, fps=fps, ban_path=ban_path, ban_name=ban.name)
    armature.animation_data_create()
    armature.animation_data.action = action

    context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="POSE")

    if clear_pose:
        for pose_bone in armature.pose.bones:
            pose_bone.location = (0, 0, 0)
            pose_bone.rotation_mode = "QUATERNION"
            pose_bone.rotation_quaternion = (1, 0, 0, 0)

    if skeleton:
        skeleton_bones = [bone for bone in skeleton.bones if bone.name in armature.pose.bones]
        count = len(ban.key_times)
        for index in range(count):
            frame = frame_from_ms(ban.key_times[index], fps)
            scene.frame_set(max(scene.frame_start, round(frame)))
            sr_matrices = ban_global_matrices_for_frame(skeleton, ban, index)
            pose_matrices = {bone.name: sr_bone_matrix_to_blender(sr_matrices[bone.name]) for bone in skeleton_bones}
            for bone in skeleton_bones:
                pose_bone = armature.pose.bones.get(bone.name)
                if not pose_bone:
                    continue
                pose_bone.rotation_mode = "QUATERNION"
                rest = armature.data.bones[bone.name].matrix_local
                if bone.parent and bone.parent in pose_matrices and bone.parent in armature.data.bones:
                    parent_pose = pose_matrices[bone.parent]
                    parent_rest = armature.data.bones[bone.parent].matrix_local
                    pose_bone.matrix_basis = (parent_pose @ parent_rest.inverted() @ rest).inverted() @ pose_matrices[bone.name]
                else:
                    pose_bone.matrix_basis = rest.inverted() @ pose_matrices[bone.name]
            context.view_layer.update()
            for bone in skeleton_bones:
                pose_bone = armature.pose.bones.get(bone.name)
                if not pose_bone:
                    continue
                pose_bone.keyframe_insert("location", frame=frame)
                pose_bone.keyframe_insert("rotation_quaternion", frame=frame)
    else:
        for bone_name, track in ban.tracks.items():
            pose_bone = armature.pose.bones.get(bone_name)
            if not pose_bone:
                continue
            pose_bone.rotation_mode = "QUATERNION"
            count = min(len(track.frames), len(ban.key_times))
            for index in range(count):
                quat_xyzw, trans = track.frames[index]
                frame = frame_from_ms(ban.key_times[index], fps)
                pose_bone.location = sr_vec_to_blender(trans)
                pose_bone.rotation_quaternion = sr_quat_to_blender(quat_xyzw)
                pose_bone.keyframe_insert("location", frame=frame)
                pose_bone.keyframe_insert("rotation_quaternion", frame=frame)

    bpy.ops.object.mode_set(mode="OBJECT")
    armature["silkroad_last_ban"] = str(ban_path)
    remember_armature_action(armature, action)
    return ban


def create_billboard_mesh(name, size=1.0):
    half = size * 0.5
    mesh = bpy.data.meshes.new(safe_name(name) + "_Mesh")
    mesh.from_pydata(
        [(-half, 0, -half), (half, 0, -half), (half, 0, half), (-half, 0, half)],
        [],
        [(0, 1, 2, 3)],
    )
    mesh.update()
    uv_layer = mesh.uv_layers.new(name="UVMap")
    coords = [(0, 0), (1, 0), (1, 1), (0, 1)]
    for idx, loop in enumerate(mesh.loops):
        uv_layer.data[loop.index].uv = coords[idx % 4]
    return mesh


def iter_efp_resources(efp_object):
    if efp_object.resource and efp_object.resource.meshes:
        yield efp_object.resource
    for controller in efp_object.controllers:
        resource = controller.get("resource") if isinstance(controller, dict) else None
        if resource and resource.meshes:
            yield resource


def source_frame_range(efp_object, fps):
    start_ms = efp_object.int0 if efp_object.int0 > 0 else 0
    end_ms = efp_object.int1 if efp_object.int1 > start_ms else max(1000, start_ms + 1000)
    return frame_from_ms(start_ms, fps), frame_from_ms(end_ms, fps)


def animate_visibility(obj, start_frame, end_frame):
    obj.hide_viewport = True
    obj.hide_render = True
    obj.keyframe_insert("hide_viewport", frame=max(1, start_frame - 1))
    obj.keyframe_insert("hide_render", frame=max(1, start_frame - 1))
    obj.hide_viewport = False
    obj.hide_render = False
    obj.keyframe_insert("hide_viewport", frame=start_frame)
    obj.keyframe_insert("hide_render", frame=start_frame)
    obj.keyframe_insert("hide_viewport", frame=end_frame)
    obj.keyframe_insert("hide_render", frame=end_frame)
    obj.hide_viewport = True
    obj.hide_render = True
    obj.keyframe_insert("hide_viewport", frame=end_frame + 1)
    obj.keyframe_insert("hide_render", frame=end_frame + 1)


def apply_efp_commands(empty, efp_object, fps, scale):
    commands = []
    commands.extend(efp_object.emitter_commands)
    commands.extend(efp_object.program_commands)
    commands.extend(efp_object.render_commands)
    for controller in efp_object.controllers:
        commands.extend(controller.get("sources", []))

    for source in commands:
        if source.command == "SetPosition" and source.value:
            empty.location = sr_vec_to_blender(source.value) * scale
        elif source.command == "SetBANPos" and isinstance(source.value, dict):
            frames = source.value.get("frames", [])
            if frames:
                start = frame_from_ms(source.start * 1000.0, fps)
                end = frame_from_ms(source.end * 1000.0, fps) if source.end > source.start else start + len(frames)
                step = (end - start) / max(1, len(frames) - 1)
                for index, vec in enumerate(frames):
                    empty.location = sr_vec_to_blender(vec) * scale
                    empty.keyframe_insert("location", frame=start + step * index)
        elif source.command == "SetBANRot" and isinstance(source.value, dict):
            frames = source.value.get("frames", [])
            if frames:
                empty.rotation_mode = "QUATERNION"
                start = frame_from_ms(source.start * 1000.0, fps)
                step = 1.0
                for index, mat in enumerate(frames):
                    converted = SR_TO_BLENDER.to_4x4() @ mat @ BLENDER_TO_SR.to_4x4()
                    empty.rotation_quaternion = converted.to_quaternion()
                    empty.keyframe_insert("rotation_quaternion", frame=start + step * index)
        elif source.command == "SetGraphScale" and isinstance(source.value, list) and source.value:
            start = frame_from_ms(source.start * 1000.0, fps)
            end = frame_from_ms(source.end * 1000.0, fps) if source.end > source.start else start + len(source.value)
            step = (end - start) / max(1, len(source.value) - 1)
            for index, vec in enumerate(source.value):
                empty.scale = sr_vec_to_blender(vec)
                empty.keyframe_insert("scale", frame=start + step * index)


def import_efp_object(context, resolver, efp_object, collection, parent=None, fps=30, effect_scale=1.0, depth=0):
    empty = bpy.data.objects.new(safe_name(efp_object.name or f"EFP_{depth}"), None)
    empty.empty_display_type = "SPHERE"
    empty.empty_display_size = 0.25 * effect_scale
    collection.objects.link(empty)
    if parent:
        empty.parent = parent
    apply_efp_commands(empty, efp_object, fps, effect_scale)

    start_frame, end_frame = source_frame_range(efp_object, fps)
    animate_visibility(empty, start_frame, end_frame)

    material_cache = {}
    for resource in iter_efp_resources(efp_object):
        for mesh_ref in resource.meshes:
            mesh_path = resolver.resolve(mesh_ref.path, prefer="Particles")
            textures = [tex for tex in mesh_ref.textures if not is_none_path(tex)]
            texture = textures[0] if textures else ""
            mat_key = texture or mesh_ref.path or efp_object.name
            mat = material_cache.get(mat_key)
            if not mat:
                mat = make_material(f"{empty.name}_{Path(mat_key).stem}", resolver, texture_path=texture, base_color=(1, 1, 1, 0.65), alpha=True)
                material_cache[mat_key] = mat

            if mesh_path and mesh_path.suffix.lower() == ".bms":
                try:
                    mesh_data = parse_bms(mesh_path)
                    obj = create_mesh_object(mesh_data, f"{empty.name}_{mesh_data.name}", resolver, {}, None, collection, True, True)
                    obj.data.materials.clear()
                    obj.data.materials.append(mat)
                except Exception:
                    obj = bpy.data.objects.new(f"{empty.name}_plate", create_billboard_mesh(f"{empty.name}_plate", effect_scale))
                    collection.objects.link(obj)
                    obj.data.materials.append(mat)
            else:
                obj = bpy.data.objects.new(f"{empty.name}_plate", create_billboard_mesh(f"{empty.name}_plate", effect_scale))
                collection.objects.link(obj)
                obj.data.materials.append(mat)

            obj.parent = empty
            animate_visibility(obj, start_frame, end_frame)

    for child in efp_object.children:
        import_efp_object(context, resolver, child, collection, empty, fps, effect_scale, depth + 1)
    return empty


def import_efp_to_scene(context, efp_path, game_root, parent=None, effect_scale=1.0, parent_collection=None, collection_name=None):
    resolver = AssetResolver(game_root)
    effect = parse_efp(efp_path)
    collection = bpy.data.collections.new(collection_name or ("EFP_" + safe_name(effect.path.stem)))
    (parent_collection or context.scene.collection).children.link(collection)
    fps = context.scene.render.fps or 30
    root = import_efp_object(context, resolver, effect.root, collection, parent, fps, effect.scale * effect_scale)
    root["silkroad_efp_path"] = str(efp_path)
    return root, effect


def parse_csv_floats(value, size=3, default=None):
    if default is None:
        default = tuple(0.0 for _ in range(size))
    if is_none_path(value):
        return default
    parts = [part.strip() for part in str(value).split(",")]
    if len(parts) < size:
        return default
    try:
        return tuple(float(parts[index]) for index in range(size))
    except ValueError:
        return default


def parse_skill_move(value):
    if is_none_path(value):
        return "MOV_NONE", []
    parts = [part.strip() for part in str(value).split(",")]
    move_type = parts[0].upper() if parts and parts[0] else "MOV_NONE"
    params = []
    for part in parts[1:]:
        try:
            params.append(float(part))
        except ValueError:
            params.append(0.0)
    return move_type, params


def parse_skill_delay(value):
    try:
        return int(float(value))
    except Exception:
        return 0


def combine_skill_resource_path(folder, name):
    if is_none_path(name) or str(name).upper() == "WEAPON":
        return ""
    if is_none_path(folder) or str(folder).strip() == "*":
        return str(name).strip()
    return normalized_relpath(str(Path(str(folder).strip()) / str(name).strip()))


def normalized_skill_cue_code(code):
    return re.sub(r"_\d+$", "", (code or "").upper())


def parse_skill_cue_tokens(tokens, raw_line):
    actions = {"READY", "WAIT", "SHOT", "ACT", "ACT_S", "ACT_L", "DEACT"}
    action_index = next((index for index, token in enumerate(tokens) if token.upper() in actions), -1)
    if len(tokens) < 24 or action_index < 0:
        return None
    attach_index = action_index + 11
    if len(tokens) <= attach_index + 11:
        return None
    attach_type = tokens[attach_index].upper()
    move_type, move_params = parse_skill_move(tokens[attach_index + 1])
    if not attach_type.startswith("AT_") or not move_type.startswith("MOV_"):
        return None

    return SkillCueInfo(
        action=tokens[action_index].upper(),
        sequence=parse_skill_delay(tokens[action_index + 1]),
        attach_type=attach_type,
        move_type=move_type,
        move_params=move_params,
        launch_offset=parse_csv_floats(tokens[attach_index + 2]),
        resource_path=combine_skill_resource_path(tokens[attach_index + 4], tokens[attach_index + 5]),
        bone_name="" if is_none_path(tokens[attach_index + 6]) or str(tokens[attach_index + 6]).strip() == "*" else tokens[attach_index + 6].strip(),
        start_offset=parse_csv_floats(tokens[attach_index + 7]),
        target_offset=parse_csv_floats(tokens[attach_index + 9]),
        linked_effect_path="" if is_none_path(tokens[attach_index + 10]) else tokens[attach_index + 10].strip(),
        delay_ms=parse_skill_delay(tokens[attach_index + 11]) if len(tokens) > attach_index + 11 else 0,
        raw_line=raw_line,
    )


def parse_skill_effects(game_root):
    path = Path(game_root) / "Media" / "server_dep" / "silkroad" / "textdata" / "skilleffect.txt"
    if not path.exists():
        return []
    cache_key = str(path).lower()
    signature = file_signature(path)
    cached = _SKILL_EFFECT_CACHE.get(cache_key)
    if cached and cached.get("signature") == signature:
        return cached["infos"]

    with path.open("rb") as probe:
        prefix = probe.read(4)
    if prefix.startswith(b"\xff\xfe") or prefix.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    elif prefix.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        encoding = CP949
    infos = []
    cues_by_code = {}
    weapons = {"BOW", "SWORD", "SPEAR", "BLADE", "GLAVIE", "STAFF", "DAGGER", "CROSSBOW", "AXE", "HARP", "CLERIC", "SHIELD"}
    with path.open("r", encoding=encoding, errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("//") or line.startswith("#"):
                continue
            tokens = [token.strip() for token in line.split("\t") if token.strip()] if "\t" in line else re.split(r"\s+", line)
            code = next((token for token in tokens if token.startswith("SKILL_")), "")
            if not code:
                continue
            cue = parse_skill_cue_tokens(tokens, line)
            if cue:
                cues_by_code.setdefault(normalized_skill_cue_code(code), []).append(cue)
            animation = next((token for token in tokens if token.startswith("ANI_")), "")
            weapon = next((token for token in tokens if token.upper() in weapons), "")
            efps = [token for token in tokens if token.lower().endswith(".efp")]
            textures = [token for token in tokens if token.lower().endswith(".ddj")]
            infos.append(SkillEffectInfo(code, animation, weapon.lower(), efps, textures, line))
    for info in infos:
        info.cues = list(cues_by_code.get(normalized_skill_cue_code(info.code), []))
    _SKILL_EFFECT_CACHE[cache_key] = {"signature": signature, "infos": infos}
    return infos


def find_skill_effect(game_root, query):
    query = (query or "").strip().upper()
    if not query:
        return None
    infos = parse_skill_effects(game_root)
    exact = [info for info in infos if info.code.upper() == query]
    if exact:
        return exact[0]
    partial = [info for info in infos if query in info.code.upper()]
    return partial[0] if partial else None


def find_matching_skill_effects(game_root, query, limit=0):
    query = (query or "").strip().upper()
    infos = parse_skill_effects(game_root)
    if query:
        infos = [info for info in infos if query in info.code.upper()]
    seen = set()
    matches = []
    for info in infos:
        code = info.code.upper()
        if code in seen:
            continue
        seen.add(code)
        matches.append(info)
        if limit and len(matches) >= limit:
            break
    return matches


def skill_matches_weapon(skill, weapon_type):
    weapon = (weapon_type or "").strip().lower()
    if not weapon:
        return True
    aliases = {"tblade": {"tblade", "glavie", "spear"}, "blade": {"blade"}, "sword": {"sword"}, "shield": {"shield"}, "bow": {"bow"}, "spear": {"spear", "glavie"}}
    accepted = aliases.get(weapon, {weapon})
    group = (skill.weapon_group or "").lower()
    code = skill.code.lower()
    return group in accepted or any(f"_{alias}_" in code or code.endswith(f"_{alias}") for alias in accepted)


def enum_character_preset_items(self, context):
    props = context.scene.silkroad_importer if context and context.scene else None
    game_root = props.game_root if props else DEFAULT_GAME_ROOT
    config = load_importer_config(game_root)
    key = ("characters", str(importer_config_path(game_root)).lower(), file_signature(importer_config_path(game_root)))
    cached = _ENUM_ITEMS_CACHE.get(key)
    if cached:
        return cached
    items = [(record["id"], record["name"], record.get("path", "")) for record in config.get("characters", [])]
    return cached_enum_items(key, items or [("__none__", "No characters", "Add a character preset")])


def enum_weapon_type_items(self, context):
    props = context.scene.silkroad_importer if context and context.scene else None
    game_root = props.game_root if props else DEFAULT_GAME_ROOT
    config = load_importer_config(game_root)
    key = ("weapon_types", str(importer_config_path(game_root)).lower(), file_signature(importer_config_path(game_root)))
    cached = _ENUM_ITEMS_CACHE.get(key)
    if cached:
        return cached
    race = "china"
    groups = config.get("weapons", {}).get(race, {})
    items = [(config_id(name), str(name).title(), "") for name in groups.keys()]
    return cached_enum_items(key, items or [("__none__", "No weapons", "Add a weapon preset")])


def enum_weapon_preset_items(self, context):
    props = context.scene.silkroad_importer if context and context.scene else None
    if not props:
        return [("__none__", "No weapons", "")]
    key = ("weapons", props.game_root, props.weapon_type, file_signature(importer_config_path(props.game_root)))
    cached = _ENUM_ITEMS_CACHE.get(key)
    if cached:
        return cached
    config = load_importer_config(props.game_root)
    records = config.get("weapons", {}).get("china", {}).get(props.weapon_type, [])
    items = [(record["id"], record["name"], record.get("path", "")) for record in records]
    return cached_enum_items(key, items or [("__none__", "No weapons", "Add a weapon preset")])


def enum_skill_items(self, context):
    props = context.scene.silkroad_importer if context and context.scene else None
    if not props:
        return [("__none__", "No skills", "")]
    query = (props.skill_search or "").strip().upper()
    skill_path = Path(props.game_root) / "Media" / "server_dep" / "silkroad" / "textdata" / "skilleffect.txt"
    key = ("skills", props.game_root, props.weapon_type, query, file_signature(skill_path))
    cached = _ENUM_ITEMS_CACHE.get(key)
    if cached:
        return cached
    skills = []
    for skill in parse_skill_effects(props.game_root):
        if not skill_matches_weapon(skill, props.weapon_type):
            continue
        if query and query not in skill.code.upper():
            continue
        skills.append(skill)
        if len(skills) >= 200:
            break
    items = [(skill.code, skill.code.replace("SKILL_", ""), f"{skill.animation} {skill.weapon_group}".strip()) for skill in skills]
    return cached_enum_items(key, items or [("__none__", "No matching skills", "")])


def ensure_named_collection(context, name):
    collection = bpy.data.collections.get(name)
    if not collection:
        collection = bpy.data.collections.new(name)
        context.scene.collection.children.link(collection)
    elif collection.name not in {child.name for child in context.scene.collection.children}:
        try:
            context.scene.collection.children.link(collection)
        except Exception:
            pass
    return collection


def clear_collection_contents(collection):
    if not collection:
        return
    for child in list(collection.children):
        clear_collection_contents(child)
        try:
            collection.children.unlink(child)
        except Exception:
            pass
        try:
            bpy.data.collections.remove(child)
        except Exception:
            pass
    for obj in list(collection.objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass


def first_armature_in_collection(name):
    collection = bpy.data.collections.get(name)
    if not collection:
        return None
    for obj in collection.all_objects:
        if obj.type == "ARMATURE":
            return obj
    return None


def hide_collection_objects(collection, hide):
    if not collection:
        return 0
    count = 0
    for obj in collection.all_objects:
        obj.hide_viewport = hide
        obj.hide_render = hide
        count += 1
    return count


def skill_animation_type(animation_token):
    token = (animation_token or "").upper()
    if token.startswith("ANI_"):
        token = token[4:]
    if token.startswith("SKILL_"):
        try:
            number = int(token.split("_", 1)[1])
        except ValueError:
            return None
        if 1 <= number <= 10:
            return 0x1A + (number - 1)
        if 11 <= number <= 20:
            return 0x44 + (number - 11)
        if 21 <= number <= 40:
            return 0x65 + (number - 21)
        if 41 <= number <= 100:
            return 0x7B + (number - 41)
    fixed = {
        "STAND1": 0x00,
        "WALK": 0x01,
        "ATTACK1": 0x02,
        "DAMAGE1": 0x03,
        "DIE1": 0x04,
        "ATTACK2": 0x05,
        "ATTREADY": 0x06,
        "RUN": 0x07,
        "ATTACK3": 0x10,
        "ATTACK4": 0x11,
        "MAGIC_SELF": 0x13,
        "MAGIC_TARGET": 0x14,
    }
    return fixed.get(token)


def active_armature(context):
    obj = context.object
    if obj and obj.type == "ARMATURE":
        return obj
    if obj and obj.parent and obj.parent.type == "ARMATURE":
        return obj.parent
    return None


def bsr_path_text(armature):
    return str(armature.get("silkroad_bsr_path", "") or "").replace("\\", "/").lower() if armature else ""


def looks_like_item_armature(armature):
    if not armature or armature.type != "ARMATURE" or not armature.get("silkroad_bsr_path"):
        return False
    if armature.get("silkroad_attachment_bone"):
        return True
    text = bsr_path_text(armature)
    return "/res/item/" in text or "/prim/mesh/item/" in text


def looks_like_character_armature(armature):
    if not armature or armature.type != "ARMATURE" or not armature.get("silkroad_bsr_path"):
        return False
    if looks_like_item_armature(armature):
        return False
    text = bsr_path_text(armature)
    return not text or "/res/char/" in text or "/res/mob/" in text or "/res/npc/" in text


def armature_from_object(obj):
    if obj and obj.type == "ARMATURE":
        return obj
    if obj and obj.parent and obj.parent.type == "ARMATURE":
        return obj.parent
    return None


def selected_armatures(context):
    result = []
    seen = set()
    for obj in context.selected_objects:
        armature = armature_from_object(obj)
        if armature and armature.name not in seen:
            result.append(armature)
            seen.add(armature.name)
    return result


def find_selected_attach_pair(context):
    props = context.scene.silkroad_importer
    armatures = selected_armatures(context)
    active = active_armature(context)
    if active and active not in armatures:
        armatures.append(active)

    weapon = next((arm for arm in armatures if arm.get("silkroad_attachment_bone")), None)
    if not weapon:
        raise SilkroadError("Select the imported weapon/item armature together with the character armature")

    attach_bone = weapon.get("silkroad_attachment_bone", "")
    character = None
    if props.target_armature and props.target_armature != weapon and props.target_armature.type == "ARMATURE":
        if attach_bone in props.target_armature.pose.bones:
            character = props.target_armature
    if not character:
        character = next((arm for arm in armatures if arm != weapon and attach_bone in arm.pose.bones), None)
    if not character:
        raise SilkroadError(f"Select a character armature that has attachment bone {attach_bone}")
    return character, weapon, attach_bone


def attach_identity_text(item_armature):
    parts = [
        item_armature.name,
        item_armature.get("silkroad_bsr_path", ""),
        item_armature.get("silkroad_skeleton_path", ""),
        item_armature.get("silkroad_attachment_bone", ""),
    ]
    return " ".join(str(part).replace("\\", "/").lower() for part in parts)


def infer_attach_rotation(item_armature):
    text = attach_identity_text(item_armature)
    for keys, degrees in ATTACH_ROTATION_PRESET_DEGREES:
        if any(key in text for key in keys):
            return tuple(math.radians(value) for value in degrees), "+".join(keys)
    return (0.0, 0.0, 0.0), "default"


def attach_armature_to_bone(character, item_armature, bone_name, rotation_offset=(0.0, 0.0, 0.0), preset_name="manual"):
    if bone_name not in character.pose.bones:
        raise SilkroadError(f"{character.name} does not have bone {bone_name}")
    item_armature.parent = character
    item_armature.parent_type = "BONE"
    item_armature.parent_bone = bone_name
    item_armature.matrix_parent_inverse = Matrix.Identity(4)
    item_armature.matrix_basis = Matrix.Identity(4)
    item_armature.location = (0, 0, 0)
    item_armature.rotation_mode = "XYZ"
    item_armature.rotation_euler = rotation_offset
    item_armature.scale = (1, 1, 1)
    item_armature["silkroad_attached_to"] = character.name
    item_armature["silkroad_attached_bone"] = bone_name
    item_armature["silkroad_attach_rotation_deg"] = json.dumps([round(math.degrees(value), 4) for value in rotation_offset])
    item_armature["silkroad_attach_rotation_preset"] = preset_name
    return item_armature


def skill_cue_parent_candidates(cue, armature, effect_parent, props):
    candidates = []
    for obj in (armature, props.weapon_armature, effect_parent):
        if obj and obj.name not in {candidate.name for candidate in candidates}:
            candidates.append(obj)
    if cue.bone_name:
        for obj in candidates:
            if obj and obj.type == "ARMATURE" and cue.bone_name in obj.pose.bones:
                return obj
    return effect_parent or armature


def skill_cue_start_location(cue, scale):
    return sr_vec_to_blender(cue.start_offset) * scale


def skill_cue_is_projectile(cue):
    return cue.attach_type.startswith("AT_MOV") or cue.move_type not in {"", "MOV_NONE"}


def skill_cue_travel_location(cue, scale, bone_parented=False):
    start = Vector(cue.start_offset)
    target = Vector(cue.target_offset)
    if not skill_cue_is_projectile(cue):
        return sr_vec_to_blender(start) * scale

    distance = abs(target.z)
    if distance < 0.001 and cue.move_params:
        distance = max(abs(value) for value in cue.move_params)
        distance = max(4.0, min(14.0, distance / 75.0))
    if distance < 0.001:
        distance = 8.0

    forward_z = target.z if abs(target.z) > 0.001 else -distance
    if bone_parented:
        end_sr = (start.x + target.x, start.y, start.z + forward_z)
    else:
        end_sr = (start.x + target.x, start.y + target.y, start.z + forward_z)
    return sr_vec_to_blender(end_sr) * scale


def skill_cue_world_start(parent, cue, scale):
    offset = skill_cue_start_location(cue, scale)
    if parent and parent.type == "ARMATURE" and cue.bone_name and cue.bone_name in parent.pose.bones:
        bone = parent.pose.bones[cue.bone_name]
        return (parent.matrix_world @ bone.matrix).translation + (parent.matrix_world.to_3x3() @ offset)
    if parent:
        return parent.matrix_world.translation + (parent.matrix_world.to_3x3() @ offset)
    return offset


def skill_cue_world_target(target, cue, scale):
    return target.matrix_world.translation + (target.matrix_world.to_3x3() @ (sr_vec_to_blender(cue.target_offset) * scale))


def point_object_toward(obj, target_location):
    direction = target_location - obj.location
    if direction.length < 0.001:
        return
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = direction.to_track_quat("Y", "Z")


def skill_cue_frame_range(context, cue):
    fps = context.scene.render.fps or 30
    start = float(context.scene.frame_start) + max(0, cue.sequence - 1) * 3.0 + (cue.delay_ms / 1000.0) * fps
    if skill_cue_is_projectile(cue):
        duration_ms = cue.move_params[-1] if cue.move_params else 350.0
        duration = max(4.0, (max(120.0, duration_ms) / 1000.0) * fps)
    else:
        duration = max(12.0, float(context.scene.frame_end - context.scene.frame_start))
    return start, start + duration


def create_skill_cue_anchor(context, cue, parent, collection, scale, target=None):
    anchor = bpy.data.objects.new(safe_name(f"{cue.action}_{cue.sequence}_{cue.move_type}"), None)
    anchor.empty_display_type = "ARROWS" if skill_cue_is_projectile(cue) else "SPHERE"
    anchor.empty_display_size = 0.35 * scale
    collection.objects.link(anchor)

    bone_parented = bool(parent and parent.type == "ARMATURE" and cue.bone_name and cue.bone_name in parent.pose.bones)
    use_target = bool(target and skill_cue_is_projectile(cue))
    if bone_parented and not use_target:
        anchor.parent = parent
        anchor.parent_type = "BONE"
        anchor.parent_bone = cue.bone_name
        anchor.matrix_parent_inverse = Matrix.Identity(4)
        anchor.matrix_basis = Matrix.Identity(4)
    elif parent and not use_target:
        anchor.parent = parent
        anchor.matrix_parent_inverse = Matrix.Identity(4)

    start_frame, end_frame = skill_cue_frame_range(context, cue)
    if use_target:
        anchor.location = skill_cue_world_start(parent, cue, scale)
        point_object_toward(anchor, skill_cue_world_target(target, cue, scale))
        anchor.keyframe_insert("location", frame=start_frame)
        anchor.keyframe_insert("rotation_quaternion", frame=start_frame)
        anchor.location = skill_cue_world_target(target, cue, scale)
        point_object_toward(anchor, anchor.location)
        anchor.keyframe_insert("location", frame=end_frame)
        anchor.keyframe_insert("rotation_quaternion", frame=end_frame)
        anchor["silkroad_target"] = target.name
    else:
        anchor.location = skill_cue_start_location(cue, scale)
    if skill_cue_is_projectile(cue) and not use_target:
        anchor.keyframe_insert("location", frame=start_frame)
        anchor.location = skill_cue_travel_location(cue, scale, bone_parented)
        anchor.keyframe_insert("location", frame=end_frame)
    animate_visibility(anchor, start_frame, end_frame)

    anchor["silkroad_skill_cue_action"] = cue.action
    anchor["silkroad_attach_type"] = cue.attach_type
    anchor["silkroad_move_type"] = cue.move_type
    anchor["silkroad_attach_bone"] = cue.bone_name
    anchor["silkroad_start_offset"] = json.dumps([round(value, 4) for value in cue.start_offset])
    anchor["silkroad_target_offset"] = json.dumps([round(value, 4) for value in cue.target_offset])
    return anchor


def parent_objects_to_anchor(objects, anchor):
    for obj in objects:
        if not obj:
            continue
        obj.parent = anchor
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.location = (0, 0, 0)
        obj["silkroad_skill_anchor"] = anchor.name


def cue_resource_paths(cue):
    paths = []
    if cue.resource_path:
        paths.append(cue.resource_path)
    if cue.linked_effect_path and cue.linked_effect_path.lower().endswith(".efp"):
        paths.append(cue.linked_effect_path)
    return paths


def import_skill_cue_resource(context, resolver, raw_path, game_root, anchor, collection, props, skill, cue):
    suffix = Path(normalized_relpath(raw_path)).suffix.lower()
    prefer = "Particles" if suffix == ".efp" else "Data"
    path = resolver.resolve(raw_path, prefer=prefer)
    if not path:
        print(f"Silkroad importer: skill cue resource not found {raw_path}")
        return 0

    collection_name = "SkillCue_" + safe_name(f"{skill.code}_{cue.action}_{cue.sequence}_{Path(raw_path).stem}")
    if suffix == ".efp":
        root, _ = import_efp_to_scene(context, path, game_root, parent=anchor, effect_scale=props.effect_scale, parent_collection=collection, collection_name=collection_name)
        root["silkroad_skill_cue_resource"] = raw_path
        return 1
    if suffix == ".bsr":
        child_armature, objects, _ = import_bsr_to_scene(context, path, game_root, collection_name=collection_name, flip_uv=props.flip_uv_v, flip_winding=props.flip_winding, parent_collection=collection)
        roots = [child_armature] if child_armature else [obj for obj in objects if obj and not obj.parent]
        if not roots:
            roots = objects
        parent_objects_to_anchor(roots, anchor)
        for root in roots:
            root["silkroad_skill_cue_resource"] = raw_path
        return 1 if roots else 0
    return 0


def import_skill_cues(context, props, skill, armature=None, effect_parent=None, effect_collection=None):
    if not props.use_skill_offsets or not skill.cues:
        return 0
    resolver = AssetResolver(props.game_root)
    collection = effect_collection or ensure_named_collection(context, EFFECT_COLLECTION)
    imported = 0
    for cue in skill.cues:
        resources = cue_resource_paths(cue)
        if not resources:
            continue
        parent = skill_cue_parent_candidates(cue, armature, effect_parent, props)
        anchor = create_skill_cue_anchor(context, cue, parent, collection, props.effect_scale, props.skill_target)
        anchor["silkroad_skill_code"] = skill.code
        for raw_path in resources:
            try:
                imported += import_skill_cue_resource(context, resolver, raw_path, props.game_root, anchor, collection, props, skill, cue)
            except Exception as exc:
                print(f"Silkroad importer: skill cue import failed {skill.code} {raw_path}: {exc}")
    return imported


def resolve_animation_for_skill(armature, game_root, skill_info):
    if not armature:
        return None
    raw_paths = json.loads(armature.get("silkroad_animation_paths", "[]"))
    raw_groups = json.loads(armature.get("silkroad_animation_groups", "{}"))
    anim_type = skill_animation_type(skill_info.animation)
    if anim_type is None:
        return None

    group_names = []
    if skill_info.weapon_group:
        group_names.append(skill_info.weapon_group)
    group_names.extend(["default", "bow", "sword", "spear"])
    group_names.extend(raw_groups.keys())

    anim_index = None
    for group_name in group_names:
        group = raw_groups.get(group_name)
        if not group:
            continue
        value = group.get(str(anim_type))
        if value is not None and int(value) >= 0:
            anim_index = int(value)
            break

    if anim_index is None or anim_index >= len(raw_paths):
        return None
    return AssetResolver(game_root).resolve(raw_paths[anim_index], prefer="Data")


def ensure_skill_armature(context, props):
    armature = active_armature(context) or props.target_armature
    if armature or not props.auto_import_character:
        return armature
    path = Path(props.default_character_bsr)
    if not path.exists():
        path = Path(props.game_root) / DEFAULT_CHARACTER_BSR
    armature, _, _ = import_bsr_to_scene(context, path, props.game_root, flip_uv=props.flip_uv_v, flip_winding=props.flip_winding)
    props.target_armature = armature
    return armature


def import_skill_info(context, props, skill, armature=None, import_effects=True, effect_parent=None, effect_collection=None):
    root = props.game_root
    resolver = AssetResolver(root)
    animation_count = 0
    effect_count = 0
    messages = []

    if armature:
        ban_path = resolve_animation_for_skill(armature, root, skill)
        if ban_path:
            try:
                ban = apply_ban_to_armature(context, armature, ban_path)
                animation_count = 1
                messages.append(f"animation {ban.name}")
            except Exception as exc:
                print(f"Silkroad importer: could not apply skill animation {skill.code}: {exc}")
                messages.append("animation failed")
        else:
            messages.append("no matching BAN animation")

    if import_effects:
        parent_for_effects = effect_parent if effect_parent is not None else armature
        for raw_efp in skill.efp_paths:
            efp_path = resolver.resolve(raw_efp, prefer="Particles")
            if not efp_path:
                print(f"Silkroad importer: EFP not found {raw_efp}")
                continue
            try:
                import_efp_to_scene(context, efp_path, root, parent=parent_for_effects, effect_scale=props.effect_scale, parent_collection=effect_collection, collection_name="Skill_" + safe_name(skill.code))
                effect_count += 1
            except Exception as exc:
                print(f"Silkroad importer: EFP import failed {efp_path}: {exc}")
        messages.append(f"{effect_count} effect(s)")
        cue_count = import_skill_cues(context, props, skill, armature, parent_for_effects, effect_collection)
        if props.use_skill_offsets:
            messages.append(f"{cue_count} cue resource(s)")

    return animation_count, effect_count, messages


class SilkroadImporterProperties(PropertyGroup):
    game_root: StringProperty(
        name="Game Root",
        subtype="DIR_PATH",
        default=DEFAULT_GAME_ROOT,
        description="Folder containing Data, Media and Particles",
    )
    default_character_bsr: StringProperty(
        name="Base Character",
        subtype="FILE_PATH",
        default=str(Path(DEFAULT_GAME_ROOT) / DEFAULT_CHARACTER_BSR),
    )
    character_preset: bpy.props.EnumProperty(
        name="Character",
        items=enum_character_preset_items,
        description="Character preset from the importer JSON config",
    )
    weapon_type: bpy.props.EnumProperty(
        name="Weapon Type",
        items=enum_weapon_type_items,
        description="Weapon group used for presets and skill filtering",
    )
    weapon_preset: bpy.props.EnumProperty(
        name="Weapon",
        items=enum_weapon_preset_items,
        description="Weapon preset from the importer JSON config",
    )
    skill_search: StringProperty(
        name="Skill Search",
        default="",
        description="Filter the skill list by code text",
    )
    skill_choice: bpy.props.EnumProperty(
        name="Skill List",
        items=enum_skill_items,
        description="Skill effect entry filtered by current weapon type",
    )
    skill_effect_parent: bpy.props.EnumProperty(
        name="Effect Parent",
        items=(("CHARACTER", "Character", "Parent effects to the active character"), ("WEAPON", "Weapon", "Parent effects to the equipped weapon"), ("WORLD", "World", "Leave effects in world space")),
        default="CHARACTER",
    )
    clear_skill_effects: BoolProperty(
        name="Clear Old Effects",
        default=True,
        description="Clear the skill effect container before playing another skill",
    )
    use_skill_offsets: BoolProperty(
        name="Use Skill Offsets",
        default=False,
        description="Use skilleffect.txt cue bones, offsets, and projectile movement when importing skill effects",
    )
    skill_target: PointerProperty(name="TARGET", type=bpy.types.Object)
    skill_code: StringProperty(
        name="Skill",
        default="SKILL_CH_BOW_CHAIN_A",
        description="Skill code or partial code from skilleffect.txt",
    )
    auto_import_character: BoolProperty(
        name="Auto Character",
        default=True,
        description="Import the base character if no armature is selected",
    )
    auto_action_timeline: BoolProperty(
        name="Auto Action Timeline",
        default=True,
        description="Update the scene frame range when switching Silkroad actions",
    )
    flip_uv_v: BoolProperty(name="Flip Texture V", default=True)
    flip_winding: BoolProperty(name="Flip Faces", default=True)
    effect_scale: FloatProperty(name="Effect Scale", default=1.0, min=0.01, max=100.0)
    auto_attach_rotation: BoolProperty(
        name="Auto Attach Rotation",
        default=True,
        description="Use a small preset rotation for known weapon types when attaching",
    )
    attach_rotation: FloatVectorProperty(
        name="Attach Rotation",
        subtype="EULER",
        unit="ROTATION",
        size=3,
        default=(0.0, 0.0, 0.0),
        description="Manual local rotation used when Auto Attach Rotation is off",
    )
    skill_import_limit: IntProperty(
        name="Skill Limit",
        default=25,
        min=1,
        max=1000,
        description="Maximum number of matching skills to import in one batch",
    )
    skill_import_effects: BoolProperty(
        name="Import Effects",
        default=True,
        description="Import EFP visual effects when batch-importing matching skills",
    )
    target_armature: PointerProperty(name="Target Armature", type=bpy.types.Object)
    weapon_armature: PointerProperty(name="Weapon Armature", type=bpy.types.Object)


class SILKROAD_OT_import_bsr(Operator, ImportHelper):
    bl_idname = "silkroad.import_bsr"
    bl_label = "Import Silkroad Character/Resource"
    bl_description = "Import a Silkroad .bsr resource with meshes, materials, skeleton and animation table"
    filename_ext = ".bsr"
    filter_glob: StringProperty(default="*.bsr", options={"HIDDEN"})

    def execute(self, context):
        props = context.scene.silkroad_importer
        try:
            target = active_armature(context) or props.target_armature
            armature, objects, _ = import_bsr_to_scene(context, self.filepath, props.game_root, flip_uv=props.flip_uv_v, flip_winding=props.flip_winding, target_armature=target)
            self.report({"INFO"}, f"Imported {len(objects)} mesh objects" + (" with armature" if armature else ""))
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SILKROAD_OT_import_default_character(Operator):
    bl_idname = "silkroad.import_default_character"
    bl_label = "Import Base Character"
    bl_description = "Import the configured base character"

    def execute(self, context):
        props = context.scene.silkroad_importer
        path = Path(props.default_character_bsr)
        if not path.exists():
            path = Path(props.game_root) / DEFAULT_CHARACTER_BSR
        try:
            import_bsr_to_scene(context, path, props.game_root, flip_uv=props.flip_uv_v, flip_winding=props.flip_winding)
            self.report({"INFO"}, "Base character imported")
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SILKROAD_OT_import_preset_character(Operator):
    bl_idname = "silkroad.import_preset_character"
    bl_label = "Load Character Preset"
    bl_description = "Clear the character container and import the selected character preset"

    def execute(self, context):
        props = context.scene.silkroad_importer
        config = load_importer_config(props.game_root)
        record = next((item for item in config.get("characters", []) if item["id"] == props.character_preset), None)
        if not record:
            self.report({"ERROR"}, "Character preset not found")
            return {"CANCELLED"}
        path = config_path_to_absolute(props.game_root, record.get("path", ""))
        if not path.exists():
            self.report({"ERROR"}, f"Character file not found: {path}")
            return {"CANCELLED"}
        try:
            char_collection = ensure_named_collection(context, CHARACTER_COLLECTION)
            clear_collection_contents(char_collection)
            clear_collection_contents(ensure_named_collection(context, WEAPON_COLLECTION))
            clear_collection_contents(ensure_named_collection(context, EFFECT_COLLECTION))
            armature, _, _ = import_bsr_to_scene(context, path, props.game_root, collection_name="Character_" + safe_name(record["id"]), flip_uv=props.flip_uv_v, flip_winding=props.flip_winding, parent_collection=char_collection)
            if not armature:
                raise SilkroadError("Character preset did not create an armature")
            armature["silkroad_role"] = "character"
            armature["silkroad_preset_id"] = record["id"]
            props.target_armature = armature
            props.weapon_armature = None
            self.report({"INFO"}, f"Loaded {record['name']}")
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SILKROAD_OT_register_selected_character(Operator):
    bl_idname = "silkroad.register_selected_character"
    bl_label = "Save Current Character"
    bl_description = "Add the current imported character armature to the JSON config"

    def execute(self, context):
        props = context.scene.silkroad_importer
        selected = active_armature(context)
        if selected and not looks_like_character_armature(selected):
            self.report({"ERROR"}, "Selected armature looks like a weapon/item. Use Save Weapon instead.")
            return {"CANCELLED"}
        armature = selected or props.target_armature
        if not armature or not armature.get("silkroad_bsr_path"):
            self.report({"ERROR"}, "Select an imported character armature first")
            return {"CANCELLED"}
        if not looks_like_character_armature(armature):
            self.report({"ERROR"}, "This looks like a weapon/item. Use Save Weapon instead.")
            return {"CANCELLED"}
        config = load_importer_config(props.game_root)
        path = Path(armature.get("silkroad_bsr_path"))
        try:
            raw_path = str(path.relative_to(Path(props.game_root)))
        except Exception:
            raw_path = str(path)
        record_id = config_id(armature.name)
        records = config.setdefault("characters", [])
        records[:] = [record for record in records if record.get("id") != record_id]
        records.append({"id": record_id, "name": armature.name, "path": raw_path})
        save_importer_config(props.game_root, config)
        self.report({"INFO"}, f"Registered character {armature.name}")
        return {"FINISHED"}


class SILKROAD_OT_equip_preset_weapon(Operator):
    bl_idname = "silkroad.equip_preset_weapon"
    bl_label = "Equip Weapon Preset"
    bl_description = "Clear the weapon container, import the selected weapon, and attach it to the active character"

    def execute(self, context):
        props = context.scene.silkroad_importer
        config = load_importer_config(props.game_root)
        records = config.get("weapons", {}).get("china", {}).get(props.weapon_type, [])
        record = next((item for item in records if item["id"] == props.weapon_preset), None)
        if not record:
            self.report({"ERROR"}, "Weapon preset not found")
            return {"CANCELLED"}
        character = props.target_armature or first_armature_in_collection(CHARACTER_COLLECTION) or active_armature(context)
        if not character or character.type != "ARMATURE":
            self.report({"ERROR"}, "Load or select a character first")
            return {"CANCELLED"}
        path = config_path_to_absolute(props.game_root, record.get("path", ""))
        if not path.exists():
            self.report({"ERROR"}, f"Weapon file not found: {path}")
            return {"CANCELLED"}
        try:
            weapon_collection = ensure_named_collection(context, WEAPON_COLLECTION)
            clear_collection_contents(weapon_collection)
            armature, _, bsr = import_bsr_to_scene(context, path, props.game_root, collection_name="Weapon_" + safe_name(record["id"]), flip_uv=props.flip_uv_v, flip_winding=props.flip_winding, parent_collection=weapon_collection)
            if not armature:
                raise SilkroadError("Weapon preset did not create an armature")
            rotation_offset, preset_name = infer_attach_rotation(armature) if props.auto_attach_rotation else (tuple(props.attach_rotation), "manual")
            attach_armature_to_bone(character, armature, bsr.attachment_bone, rotation_offset, preset_name)
            armature["silkroad_role"] = "weapon"
            armature["silkroad_preset_id"] = record["id"]
            props.target_armature = character
            props.weapon_armature = armature
            props.attach_rotation = rotation_offset
            self.report({"INFO"}, f"Equipped {record['name']}")
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SILKROAD_OT_register_selected_weapon(Operator):
    bl_idname = "silkroad.register_selected_weapon"
    bl_label = "Save Current Weapon"
    bl_description = "Add the current imported weapon armature to the selected weapon type in the JSON config"

    def execute(self, context):
        props = context.scene.silkroad_importer
        selected = active_armature(context)
        if selected and not looks_like_item_armature(selected):
            self.report({"ERROR"}, "Selected armature is not a weapon/item")
            return {"CANCELLED"}
        armature = selected or props.weapon_armature
        if not armature or not armature.get("silkroad_bsr_path"):
            self.report({"ERROR"}, "Select an imported weapon armature first")
            return {"CANCELLED"}
        if armature == props.target_armature or not looks_like_item_armature(armature):
            self.report({"ERROR"}, "This does not look like an attached weapon/item armature")
            return {"CANCELLED"}
        if props.weapon_type == "__none__":
            self.report({"ERROR"}, "Choose a weapon type first")
            return {"CANCELLED"}
        config = load_importer_config(props.game_root)
        path = Path(armature.get("silkroad_bsr_path"))
        try:
            raw_path = str(path.relative_to(Path(props.game_root)))
        except Exception:
            raw_path = str(path)
        record_id = config_id(armature.name)
        records = config.setdefault("weapons", {}).setdefault("china", {}).setdefault(props.weapon_type, [])
        records[:] = [record for record in records if record.get("id") != record_id]
        records.append({"id": record_id, "name": armature.name, "path": raw_path})
        save_importer_config(props.game_root, config)
        self.report({"INFO"}, f"Registered weapon {armature.name}")
        return {"FINISHED"}


class SILKROAD_OT_apply_ban(Operator, ImportHelper):
    bl_idname = "silkroad.apply_ban"
    bl_label = "Apply BAN Animation"
    bl_description = "Apply a Silkroad .ban animation to the selected armature"
    filename_ext = ".ban"
    filter_glob: StringProperty(default="*.ban", options={"HIDDEN"})

    def execute(self, context):
        armature = active_armature(context) or context.scene.silkroad_importer.target_armature
        try:
            ban = apply_ban_to_armature(context, armature, self.filepath)
            self.report({"INFO"}, f"Applied animation {ban.name}")
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SILKROAD_OT_apply_ban_batch(Operator, ImportHelper):
    bl_idname = "silkroad.apply_ban_batch"
    bl_label = "Import BAN Actions"
    bl_description = "Import multiple .ban files as Blender actions on the selected armature"
    filename_ext = ".ban"
    filter_glob: StringProperty(default="*.ban", options={"HIDDEN"})
    files: CollectionProperty(type=OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        armature = active_armature(context) or context.scene.silkroad_importer.target_armature
        paths = [Path(self.directory) / item.name for item in self.files] if self.files else [Path(self.filepath)]
        imported = []
        failed = 0
        for path in paths:
            try:
                ban = apply_ban_to_armature(context, armature, path)
                imported.append(ban.name)
            except Exception as exc:
                failed += 1
                print(f"Silkroad importer: BAN import failed {path}: {exc}")
        if not imported:
            self.report({"ERROR"}, "No BAN animations imported")
            return {"CANCELLED"}
        suffix = f", {failed} failed" if failed else ""
        self.report({"INFO"}, f"Imported {len(imported)} BAN action(s){suffix}")
        return {"FINISHED"}


class SILKROAD_OT_attach_selected_item(Operator):
    bl_idname = "silkroad.attach_selected_item"
    bl_label = "Attach Selected Item"
    bl_description = "Attach the selected imported item or weapon to the selected character using the BSR attachment bone"

    def execute(self, context):
        try:
            props = context.scene.silkroad_importer
            character, item_armature, bone_name = find_selected_attach_pair(context)
            if props.auto_attach_rotation:
                rotation_offset, preset_name = infer_attach_rotation(item_armature)
                props.attach_rotation = rotation_offset
            else:
                rotation_offset, preset_name = tuple(props.attach_rotation), "manual"
            attach_armature_to_bone(character, item_armature, bone_name, rotation_offset, preset_name)
            degrees = ", ".join(f"{math.degrees(value):.0f}" for value in rotation_offset)
            self.report({"INFO"}, f"Attached {item_armature.name} to {character.name}:{bone_name} ({preset_name}: {degrees})")
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SILKROAD_OT_import_efp(Operator, ImportHelper):
    bl_idname = "silkroad.import_efp"
    bl_label = "Import EFP Effect"
    bl_description = "Import a Silkroad .efp visual effect as animated Blender proxy objects"
    filename_ext = ".efp"
    filter_glob: StringProperty(default="*.efp", options={"HIDDEN"})

    def execute(self, context):
        props = context.scene.silkroad_importer
        parent = active_armature(context) or props.target_armature
        try:
            import_efp_to_scene(context, self.filepath, props.game_root, parent=parent, effect_scale=props.effect_scale)
            self.report({"INFO"}, "Effect imported")
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SILKROAD_OT_import_skill(Operator):
    bl_idname = "silkroad.import_skill"
    bl_label = "Import Skill"
    bl_description = "Find a skill in skilleffect.txt, apply its character animation and import its EFP effects"

    def execute(self, context):
        props = context.scene.silkroad_importer
        root = props.game_root
        skill = find_skill_effect(root, props.skill_code)
        if not skill:
            self.report({"ERROR"}, f"Skill not found: {props.skill_code}")
            return {"CANCELLED"}

        try:
            armature = ensure_skill_armature(context, props)
            _, _, messages = import_skill_info(context, props, skill, armature, import_effects=True)
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"{skill.code}: " + ", ".join(messages))
        return {"FINISHED"}


class SILKROAD_OT_play_selected_skill(Operator):
    bl_idname = "silkroad.play_selected_skill"
    bl_label = "Play Selected Skill"
    bl_description = "Apply the selected skill animation and import its effects into the skill effect container"

    def execute(self, context):
        props = context.scene.silkroad_importer
        code = props.skill_choice if props.skill_choice != "__none__" else props.skill_code
        skill = find_skill_effect(props.game_root, code)
        if not skill:
            self.report({"ERROR"}, f"Skill not found: {code}")
            return {"CANCELLED"}
        character = props.target_armature or first_armature_in_collection(CHARACTER_COLLECTION) or active_armature(context)
        weapon = props.weapon_armature or first_armature_in_collection(WEAPON_COLLECTION)
        if props.clear_skill_effects:
            clear_collection_contents(ensure_named_collection(context, EFFECT_COLLECTION))
        effect_collection = ensure_named_collection(context, EFFECT_COLLECTION)
        effect_parent = character
        if props.skill_effect_parent == "WEAPON":
            effect_parent = weapon
        elif props.skill_effect_parent == "WORLD":
            effect_parent = None
        try:
            animations, effects, messages = import_skill_info(context, props, skill, character, import_effects=True, effect_parent=effect_parent, effect_collection=effect_collection)
            props.skill_code = skill.code
            if character:
                props.target_armature = character
            if weapon:
                props.weapon_armature = weapon
            if context.scene.frame_current < context.scene.frame_start or context.scene.frame_current > context.scene.frame_end:
                context.scene.frame_set(context.scene.frame_start)
            self.report({"INFO"}, f"{skill.code}: {animations} animation(s), {effects} effect(s)")
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SILKROAD_OT_toggle_skill_effects(Operator):
    bl_idname = "silkroad.toggle_skill_effects"
    bl_label = "Toggle Skill Effects"
    bl_description = "Hide or show the current skill effect container"

    def execute(self, context):
        collection = ensure_named_collection(context, EFFECT_COLLECTION)
        hide = not bool(collection.get("silkroad_hidden", False))
        count = hide_collection_objects(collection, hide)
        collection["silkroad_hidden"] = hide
        self.report({"INFO"}, ("Hidden" if hide else "Shown") + f" {count} effect object(s)")
        return {"FINISHED"}


class SILKROAD_OT_import_matching_skills(Operator):
    bl_idname = "silkroad.import_matching_skills"
    bl_label = "Import Matching Skills"
    bl_description = "Import every skilleffect.txt entry matching the Skill text, limited by Skill Limit"

    def execute(self, context):
        props = context.scene.silkroad_importer
        skills = find_matching_skill_effects(props.game_root, props.skill_code, props.skill_import_limit)
        if not skills:
            self.report({"ERROR"}, f"No matching skills found: {props.skill_code}")
            return {"CANCELLED"}

        try:
            armature = ensure_skill_armature(context, props)
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, f"Could not import base character: {exc}")
            return {"CANCELLED"}

        imported = 0
        animation_count = 0
        effect_count = 0
        failed = 0
        for skill in skills:
            try:
                animations, effects, _ = import_skill_info(context, props, skill, armature, import_effects=props.skill_import_effects)
                animation_count += animations
                effect_count += effects
                imported += 1
            except Exception as exc:
                failed += 1
                print(f"Silkroad importer: skill import failed {skill.code}: {exc}")

        suffix = f", {failed} failed" if failed else ""
        self.report({"INFO"}, f"Imported {imported} skill(s), {animation_count} animation(s), {effect_count} effect(s){suffix}")
        return {"FINISHED"} if imported else {"CANCELLED"}


class SILKROAD_PT_importer(Panel):
    bl_label = "Silkroad Importer"
    bl_idname = "SILKROAD_PT_importer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Silkroad"

    def draw(self, context):
        props = context.scene.silkroad_importer
        layout = self.layout
        layout.prop(props, "game_root")
        layout.prop(props, "target_armature")
        layout.prop(props, "weapon_armature")
        row = layout.row(align=True)
        row.prop(props, "flip_uv_v")
        row.prop(props, "flip_winding")
        layout.prop(props, "auto_action_timeline")
        layout.prop(props, "effect_scale")

        layout.separator()
        char_box = layout.box()
        char_box.label(text="Character")
        char_box.prop(props, "character_preset")
        row = char_box.row(align=True)
        row.operator(SILKROAD_OT_import_preset_character.bl_idname)
        row.operator(SILKROAD_OT_register_selected_character.bl_idname, text="Save Char")

        weapon_box = layout.box()
        weapon_box.label(text="Weapon")
        row = weapon_box.row(align=True)
        row.prop(props, "weapon_type")
        row.prop(props, "weapon_preset")
        row = weapon_box.row(align=True)
        row.operator(SILKROAD_OT_equip_preset_weapon.bl_idname)
        row.operator(SILKROAD_OT_register_selected_weapon.bl_idname, text="Save Weapon")
        weapon_box.prop(props, "auto_attach_rotation")
        weapon_box.prop(props, "attach_rotation")

        skill_box = layout.box()
        skill_box.label(text="Skill Player")
        skill_box.prop(props, "skill_search")
        skill_box.prop(props, "skill_choice")
        skill_box.prop(props, "skill_effect_parent")
        row = skill_box.row(align=True)
        row.prop(props, "clear_skill_effects")
        row.operator(SILKROAD_OT_toggle_skill_effects.bl_idname, text="Eye")
        skill_box.prop(props, "use_skill_offsets")
        skill_box.prop(props, "skill_target")
        skill_box.operator(SILKROAD_OT_play_selected_skill.bl_idname, icon="PLAY")

        layout.separator()
        layout.label(text="Manual Tools")
        layout.prop(props, "default_character_bsr")
        layout.operator(SILKROAD_OT_import_default_character.bl_idname)
        layout.operator(SILKROAD_OT_import_bsr.bl_idname)
        layout.operator(SILKROAD_OT_attach_selected_item.bl_idname)
        layout.operator(SILKROAD_OT_apply_ban.bl_idname)
        layout.operator(SILKROAD_OT_apply_ban_batch.bl_idname)
        layout.operator(SILKROAD_OT_import_efp.bl_idname)
        layout.separator()
        layout.prop(props, "skill_code")
        layout.prop(props, "auto_import_character")
        layout.prop(props, "use_skill_offsets")
        layout.prop(props, "skill_target")
        layout.operator(SILKROAD_OT_import_skill.bl_idname, icon="PLAY")


def menu_func_import(self, context):
    self.layout.operator(SILKROAD_OT_import_bsr.bl_idname, text="Silkroad Resource/Character (.bsr)")
    self.layout.operator(SILKROAD_OT_apply_ban.bl_idname, text="Silkroad Animation (.ban)")
    self.layout.operator(SILKROAD_OT_apply_ban_batch.bl_idname, text="Silkroad Animation Actions (.ban)")
    self.layout.operator(SILKROAD_OT_import_efp.bl_idname, text="Silkroad Effect (.efp)")


CLASSES = (
    SilkroadImporterProperties,
    SILKROAD_OT_import_bsr,
    SILKROAD_OT_import_default_character,
    SILKROAD_OT_import_preset_character,
    SILKROAD_OT_register_selected_character,
    SILKROAD_OT_equip_preset_weapon,
    SILKROAD_OT_register_selected_weapon,
    SILKROAD_OT_apply_ban,
    SILKROAD_OT_apply_ban_batch,
    SILKROAD_OT_attach_selected_item,
    SILKROAD_OT_import_efp,
    SILKROAD_OT_import_skill,
    SILKROAD_OT_play_selected_skill,
    SILKROAD_OT_toggle_skill_effects,
    SILKROAD_OT_import_matching_skills,
    SILKROAD_PT_importer,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.silkroad_importer = PointerProperty(type=SilkroadImporterProperties)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    if silkroad_action_timeline_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(silkroad_action_timeline_handler)


def unregister():
    if silkroad_action_timeline_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(silkroad_action_timeline_handler)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.silkroad_importer
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
