"""Loader and validator for the M0 scene format (version 0.1)."""

import json
import math
from bisect import bisect_left
from copy import deepcopy
from fractions import Fraction

__all__ = ["apply_edit", "component_catalog", "frame_count", "human_capsules",
           "load_edits", "load_scene", "prop_boxes", "sample_scene", "validate_scene",
           "validate_shot_set"]

BODY_FIELDS = ("height", "shoulder", "build", "leg_ratio", "head_scale", "chest")
CAMERA_FIELDS = ("pos", "look_at", "lens_mm", "sensor_mm", "roll", "near", "far")
DEFAULT_HUMAN_BODY = dict(height=1.75, shoulder=.43, build=.5, leg_ratio=.49, head_scale=1, chest=.5)
MAX_DYNAMIC_FRAMES = MAX_KEYFRAMES = 120

JOINTS = {
    "left_shoulder": ("左肩", -90, 160), "right_shoulder": ("右肩", -90, 160),
    "left_elbow": ("左肘", -30, 145), "right_elbow": ("右肘", -30, 145),
    "left_hip": ("左髋", -75, 90), "right_hip": ("右髋", -75, 90),
    "left_knee": ("左膝", 0, 145), "right_knee": ("右膝", 0, 145),
}
ACTIONS = {
    "stand": ("站立", {}),
    "walk": ("走路", {"distance": {"type": "number", "required": False,
                                   "default": 1, "min": 0, "max": 100}}),
    "wave": ("挥手", {}),
}
PROP_PARAMS = {"pos": {"type": "array", "required": True, "length": 3,
                       "items": {"type": "number"}},
               "yaw": {"type": "number", "required": False, "default": 0}}
PROPS = {
    "table": ("桌子", (((0, .74, 0), (1.2, .08, .72)),) + tuple(
        ((x, .35, z), (.08, .70, .08)) for x in (-.52, .52) for z in (-.28, .28))),
    "chair": ("椅子", (((0, .44, 0), (.46, .08, .46)), ((0, .70, -.19), (.46, .44, .08))) + tuple(
        ((x, .20, z), (.06, .40, .06)) for x in (-.18, .18) for z in (-.18, .18))),
    "pedestal": ("台座", (((0, .055, 0), (.65, .11, .65)),
                          ((0, .38, 0), (.42, .54, .42)), ((0, .70, 0), (.56, .10, .56)))),
}


def component_catalog():
    """Return detached component declarations shared by UI and external callers."""
    return {
        "actions": [{"id": key, "name": name, "params": deepcopy(params)}
                    for key, (name, params) in ACTIONS.items()],
        "props": [{"id": key, "name": name, "params": deepcopy(PROP_PARAMS)}
                  for key, (name, _) in PROPS.items()],
        "joints": [{"id": key, "name": name, "min": low, "max": high}
                   for key, (name, low, high) in JOINTS.items()],
    }


def prop_boxes(entity):
    """Return prefab boxes as (local center xyz, size xyz), relative to the prop base."""
    return list(PROPS[entity["component"]][1])


def _limb_point(point, old_pivot, pivot, pitch=0, spread=0):
    x, y, z = (a-b for a, b in zip(point, old_pivot))
    if spread:
        c, s = math.cos(spread), math.sin(spread)
        x, y = c*x-s*y, s*x+c*y
    if pitch:
        c, s = math.cos(pitch), math.sin(pitch)
        y, z = c*y+s*z, c*z-s*y
    return pivot[0]+x, pivot[1]+y, pivot[2]+z


def human_capsules(body, joints=None):
    h = body['height']
    half = body['shoulder']/2
    leg = body['leg_ratio']*h
    build = .75+.5*body['build']
    head = .060*h*body['head_scale']
    neck = h-2*head
    shoulder_y = leg + .66*(neck-leg)
    hip = min(.52*half, .09*h)
    ankle = .041*h*build
    arm = .037*h*build
    foot = .043*h*build
    torso = min(half, .118*h*build*(.85+.3*body['chest']), .75*(shoulder_y-leg))
    parts = [
        ((0, leg+torso/2, 0), (0, shoulder_y, 0), torso),
        ((0, shoulder_y, 0), (0, neck+.23*head, 0), .038*h),
        ((0, h-head-.035*h, 0), (0, h-head, 0), head),
    ]
    for sign, side in ((-1, 'left'), (1, 'right')):
        a = sign*half
        shoulder = (a, shoulder_y, 0)
        elbow = (1.19*a, leg+.28*(shoulder_y-leg), 0)
        wrist = (1.24*a, leg-.09*h, .024*h)
        hip_point = (sign*hip, leg, 0)
        knee = (sign*hip, .53*leg, 0)
        ankle_point = (sign*hip, foot, 0)
        heel, toe = (sign*hip, foot, -.02*h), (sign*hip, foot, .054*h)
        if joints:
            raise_arm = sign*math.radians(joints.get(f'{side}_shoulder', 0))
            bend_arm = sign*math.radians(joints.get(f'{side}_elbow', 0))
            if raise_arm or bend_arm:
                posed_elbow = _limb_point(elbow, shoulder, shoulder, spread=raise_arm) if raise_arm else elbow
                wrist = _limb_point(wrist, elbow, posed_elbow, spread=raise_arm+bend_arm)
                elbow = posed_elbow
            raise_leg = math.radians(joints.get(f'{side}_hip', 0))
            bend_leg = -math.radians(joints.get(f'{side}_knee', 0))
            if raise_leg or bend_leg:
                posed_knee = _limb_point(knee, hip_point, hip_point, pitch=raise_leg) if raise_leg else knee
                posed_ankle = _limb_point(ankle_point, knee, posed_knee, pitch=raise_leg+bend_leg)
                heel = _limb_point(heel, ankle_point, posed_ankle)
                toe = _limb_point(toe, ankle_point, posed_ankle)
                knee, ankle_point = posed_knee, posed_ankle
        parts += [(shoulder, elbow, arm), (elbow, wrist, arm*.84),
                  (hip_point, knee, .056*h*build), (knee, ankle_point, ankle), (heel, toe, foot)]
    return parts


def _object(value, path, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an object")
    for key in required:
        if key not in value:
            raise ValueError(f"{path}.{key}: required field is missing")
    for key in value:
        if key not in required and key not in optional:
            raise ValueError(f"{path}.{key}: unknown field")


def _literal(value, path, *choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{path}: expected {' or '.join(map(repr, choices))}")


def _number(value, path, *, positive=False, integer=False):
    if type(value) not in (int, float) or (integer and type(value) is not int):
        expected = "an integer" if integer else "a number"
        raise ValueError(f"{path}: expected {expected}")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or abs(value) > 10000:
        raise ValueError(f"{path}: expected a finite number within [-10000, 10000]")
    if positive and value <= 0:
        raise ValueError(f"{path}: must be greater than 0")
    return value


def _vector(value, path, length, **number_options):
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{path}: expected an array of length {length}")
    for index, item in enumerate(value):
        _number(item, f"{path}[{index}]", **number_options)
    return value


def _id(value, path, seen):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: expected a non-blank string")
    if value in seen:
        raise ValueError(f"{path}: duplicate ID {value!r} (first used at {seen[value]})")
    seen[value] = path


def _entity_name(value, path):
    if not isinstance(value, str) or len(value) > 80 or not all(c.isprintable() for c in value):
        raise ValueError(f"{path}: 名称必须是最多 80 个可打印字符")
    return value.strip()


def _validate_action_text(text, path):
    if not isinstance(text, str) or len(text) > 2000:
        raise ValueError(f"{path}: expected a string of at most 2000 characters")


def _validate_keyframe(keyframe, path, count):
    _object(keyframe, path, ("frame", "pos", "yaw", "joints"))
    _validate_frame_index(keyframe["frame"], path + ".frame", count)
    if _vector(keyframe["pos"], path + ".pos", 3)[1] < 0:
        raise ValueError(f"{path}.pos[1]: bottom center cannot be below ground")
    _number(keyframe["yaw"], path + ".yaw")
    _validate_joints(keyframe["joints"], path + ".joints")


def _validate_joints(joints, path):
    _object(joints, path, (), JOINTS)
    for key, angle in joints.items():
        _, low, high = JOINTS[key]
        if not low <= _number(angle, f"{path}.{key}") <= high:
            raise ValueError(f"{path}.{key}: must be within [{low}, {high}]")


def _validate_frame_index(frame, path, count):
    if type(frame) is not int or not 0 <= frame < count:
        raise ValueError(f"{path}: expected an integer frame in [0, {count - 1}]")


def _validate_track(track, path, count):
    if not isinstance(track, list) or len(track) > MAX_KEYFRAMES:
        raise ValueError(f"{path}: expected an array of at most {MAX_KEYFRAMES} keyframes")
    last = -1
    for index, keyframe in enumerate(track):
        item_path = f"{path}[{index}]"
        _validate_keyframe(keyframe, item_path, count)
        if keyframe["frame"] <= last:
            raise ValueError(f"{item_path}.frame: keyframes must be strictly increasing")
        last = keyframe["frame"]


def _validate_entity(entity, path, seen, count=None):
    _object(entity, path, ("id", "kind", "transform"),
            ("body", "pose", "size", "name", "component", "keyframes", "joints"))
    _id(entity["id"], f"{path}.id", seen)
    kind = entity["kind"]
    _literal(kind, f"{path}.kind", "human", "box", "prop")
    if kind == "human":
        _object(entity, path, ("id", "kind", "transform", "body"), ("pose", "name", "keyframes", "joints"))
    else:
        _object(entity, path, ("id", "kind", "transform", "size" if kind == "box" else "component"), ("name",))
    if "name" in entity:
        _entity_name(entity["name"], f"{path}.name")

    transform = entity["transform"]
    _object(transform, f"{path}.transform", ("pos",), ("yaw",))
    pos = _vector(transform["pos"], f"{path}.transform.pos", 3)
    if pos[1] < 0:
        raise ValueError(f"{path}.transform.pos[1]: bottom center cannot be below ground")
    if "yaw" in transform:
        _number(transform["yaw"], f"{path}.transform.yaw")

    if kind == "box":
        _vector(entity["size"], f"{path}.size", 3, positive=True)
        return
    if kind == "prop":
        _literal(entity["component"], f"{path}.component", *PROPS)
        return

    body = entity["body"]
    _object(body, f"{path}.body", BODY_FIELDS)
    ranges = ((.5, 2.6), (.15, .85), (0, 1), (.35, .65), (.6, 1.5), (0, 1))
    for field, (low, high) in zip(BODY_FIELDS, ranges):
        if not low <= _number(body[field], f"{path}.body.{field}") <= high:
            raise ValueError(f"{path}.body.{field}: must be within [{low}, {high}]")
    if body["shoulder"] >= body["height"]:
        raise ValueError(f"{path}.body.shoulder: must be less than height")
    if "pose" in entity:
        _literal(entity["pose"], f"{path}.pose", "stand")
    if "keyframes" in entity:
        _validate_track(entity["keyframes"], f"{path}.keyframes", count)
    if "joints" in entity:
        if "keyframes" in entity:
            raise ValueError(f"{path}.joints: cannot coexist with keyframes")
        _validate_joints(entity["joints"], f"{path}.joints")


def _validate_camera(camera):
    _object(camera, "camera", ("pos", "look_at", "lens_mm", "sensor_mm", "near", "far"), ("roll",))
    pos = _vector(camera["pos"], "camera.pos", 3)
    look_at = _vector(camera["look_at"], "camera.look_at", 3)
    dx, dy, dz = (float(target) - float(origin) for origin, target in zip(pos, look_at))
    if not all(math.isfinite(component) for component in (dx, dy, dz)):
        raise ValueError("camera.look_at: viewing direction is not finite")
    distance = math.hypot(dx, dy, dz)
    if distance < 1e-6:
        raise ValueError("camera.look_at: must differ from camera.pos by at least 1e-6 m")
    if math.hypot(dx, dz) < 1e-8 * distance:
        raise ValueError("camera.look_at: viewing direction cannot be parallel to Y up")
    for field in ("lens_mm", "sensor_mm", "near", "far"):
        if _number(camera[field], f"camera.{field}") < 1e-4:
            raise ValueError(f"camera.{field}: must be at least 0.0001")
    if camera["far"] <= camera["near"]:
        raise ValueError("camera.far: must be greater than camera.near")
    if "roll" in camera:
        _number(camera["roll"], "camera.roll")


def frame_count(doc):
    """Number of integer frame indices in the shot, starting at frame zero."""
    shot = doc["shot"]
    # 按十进制数值精确相乘，避免浮点舍入把整数帧数推过取整边界。
    frames = Fraction(str(shot["duration_s"])) * Fraction(str(shot["fps"]))
    return max(1, math.ceil(frames))


def _sample_track(track, frame):
    right = bisect_left(track, frame, key=lambda keyframe: keyframe["frame"])
    if right == 0 or right == len(track) or track[right]["frame"] == frame:
        point = track[min(right, len(track) - 1)]
        return point["pos"], point["yaw"], point["joints"]
    a, b = track[right - 1], track[right]
    t = (frame - a["frame"]) / (b["frame"] - a["frame"])
    pos = [p + t * (q - p) for p, q in zip(a["pos"], b["pos"])]
    yaw = a["yaw"] + t * ((b["yaw"] - a["yaw"] + 180) % 360 - 180)
    if abs(yaw) > 10000:  # Keep the sampled transform valid even at the input angle limits.
        yaw %= 360
    joints = {}
    for key in JOINTS:
        if key in a["joints"] or key in b["joints"]:
            p, q = a["joints"].get(key, 0), b["joints"].get(key, 0)
            joints[key] = p + t * (q - p)
    return pos, yaw, joints


def sample_scene(doc, frame):
    """Take a detached snapshot of a validated scene at the requested frame."""
    _validate_frame_index(frame, "frame", frame_count(doc))
    source_entities = doc["scene"]["entities"]
    # Skip copying the tracks: the renderer needs only the sampled poses.
    snapshot = deepcopy({**doc, "scene": {**doc["scene"], "entities": []}})
    for entity in source_entities:
        sampled = deepcopy({key: value for key, value in entity.items() if key != "keyframes"})
        if entity.get("keyframes"):
            pos, yaw, joints = _sample_track(entity["keyframes"], frame)
            sampled["transform"] = {"pos": list(pos), "yaw": yaw}
            sampled["joints"] = dict(joints)
        snapshot["scene"]["entities"].append(sampled)
    return snapshot


def _validate_human_bounds(entity, width, depth, pos, yaw, joints=None, frame=None):
    x0, _, z0 = pos
    yaw = math.radians(yaw)
    c, s = math.cos(yaw), math.sin(yaw)
    for a, b, radius in human_capsules(entity["body"], joints):
        if any(abs(x0 + c*x + s*z) + radius > width/2 + 1e-9 or
               abs(z0 - s*x + c*z) + radius > depth/2 + 1e-9
               for x, _, z in (a, b)):
            label = entity.get("name", "").strip() or entity["id"]
            prefix = f"第 {frame} 帧 (frame {frame}): " if frame is not None else ""
            raise ValueError(f"{prefix}人物「{label}」超出沙盒边界，请调整站位、朝向或沙盒宽深。")


def validate_scene(doc, *, check=None):
    """Return doc unchanged or raise ValueError with the offending field path.

    Omitted yaw and roll mean 0; omitted human pose means 'stand'.
    Optional check lets the caller stop long validation cooperatively.
    """
    if check is not None:
        check()
    _object(doc, "document", ("version", "units", "up", "resolution", "scene", "camera", "shot"))
    _literal(doc["version"], "version", "0.1")
    _literal(doc["units"], "units", "meter")
    _literal(doc["up"], "up", "Y")
    width, height = _vector(doc["resolution"], "resolution", 2, positive=True, integer=True)
    if width > 2048 or height > 2048:
        raise ValueError("resolution: each dimension must be at most 2048 pixels")

    seen = {}
    scene = doc["scene"]
    _object(scene, "scene", ("id", "ground", "entities"))
    _id(scene["id"], "scene.id", seen)
    ground = scene["ground"]
    _object(ground, "scene.ground", ("type", "size"))
    _literal(ground["type"], "scene.ground.type", "plane")
    width, depth = _vector(ground["size"], "scene.ground.size", 2)
    if not (1 <= width <= 100 and 1 <= depth <= 100):
        raise ValueError("沙盒宽、深必须各在 1–100 米之间")
    entities = scene["entities"]
    if not isinstance(entities, list) or len(entities) > 256:
        raise ValueError("scene.entities: expected an array of at most 256 entities")
    shot = doc["shot"]
    _object(shot, "shot", ("id", "duration_s", "fps"), ("action_text",))
    if "action_text" in shot:
        _validate_action_text(shot["action_text"], "shot.action_text")
    _number(shot["duration_s"], "shot.duration_s", positive=True)
    _number(shot["fps"], "shot.fps", positive=True)
    count = frame_count(doc)
    animated = []
    for index, entity in enumerate(entities):
        if check is not None:
            check()
        _validate_entity(entity, f"scene.entities[{index}]", seen, count)
        if entity["kind"] != "human":
            continue
        track = entity.get("keyframes")
        if track:
            animated.append(entity)
        else:
            transform = entity["transform"]
            _validate_human_bounds(entity, width, depth, transform["pos"],
                                   transform.get("yaw", 0), entity.get("joints"))

    _validate_camera(doc["camera"])
    _id(doc["shot"]["id"], "shot.id", seen)
    if animated:
        _check_dynamic_budget(count)
        for frame in range(count):
            for entity in animated:
                if check is not None:
                    check()
                pos, yaw, joints = _sample_track(entity["keyframes"], frame)
                _validate_human_bounds(entity, width, depth, pos, yaw, joints, frame)
    return doc


def validate_shot_set(shot_set):
    """Validate an ordered set of complete scenes without changing their JSON values."""
    _object(shot_set, "shot_set", ("version", "shots"))
    _literal(shot_set["version"], "shot_set.version", "1")
    shots = shot_set["shots"]
    if not isinstance(shots, list) or not 1 <= len(shots) <= 12:
        raise ValueError("shot_set.shots: expected an array of 1 to 12 shots")
    seen = {}
    for index, doc in enumerate(shots):
        path = f"shot_set.shots[{index}]"
        try:
            validate_scene(doc)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        _id(doc["shot"]["id"], f"{path}.shot.id", seen)
    return shot_set


def _check_dynamic_budget(count):
    if count > MAX_DYNAMIC_FRAMES:
        raise ValueError(f"shot: dynamic scenes are limited to {MAX_DYNAMIC_FRAMES} frames (got {count})")


def _builtin_track(component, params, entity, count, fps, path):
    _check_dynamic_budget(count)
    base = entity["transform"]
    pos = _vector(base["pos"], f'{path}: entity {entity["id"]!r} transform.pos', 3)
    yaw = _number(base.get("yaw", 0), f'{path}: entity {entity["id"]!r} transform.yaw')
    angle = math.radians(yaw)

    def keyframe(frame, joints=None, offset=0):
        return {"frame": frame, "pos": [pos[0] + math.sin(angle)*offset, pos[1],
                                        pos[2] + math.cos(angle)*offset],
                "yaw": yaw, "joints": joints or {}}

    if component == "stand":
        return [keyframe(0)]
    if component == "wave":
        poses = [{}, {"right_shoulder": 88, "right_elbow": 34},
                 {"right_shoulder": 88, "right_elbow": 77},
                 {"right_shoulder": 88, "right_elbow": 34}, {}]
        if count == 1:
            return [keyframe(0, poses[2])]
        if count == 2:
            return [keyframe(0), keyframe(1, poses[2])]
        frames = {0: poses[0], count - 1: poses[-1]}
        for i in (1, 3, 2):
            frames[max(1, min(count - 2, round(i * (count - 1) / 4)))] = poses[i]
        return [keyframe(frame, joints) for frame, joints in sorted(frames.items())]

    distance = _number(params.get("distance", 1), path + ".distance")
    _, declaration = ACTIONS["walk"]
    if not declaration["distance"]["min"] <= distance <= declaration["distance"]["max"]:
        raise ValueError(f"{path}.distance: must be within [0, 100]")
    last = count - 1
    if not last or not distance:
        return [keyframe(0)]
    # 每周期至少八个帧间隔，让两次摆腿都采到前摆、后摆和屈膝。
    cycles = min(max(1, round(last / fps)), last // 8)
    if not cycles:
        return [keyframe(0), keyframe(last, offset=distance)]
    track = []
    for frame in range(count):
        # 只摆动一侧，另一侧保持直腿接地；换脚时恢复站姿。
        half, progress = divmod(frame * cycles * 2, last)
        joints = {}
        if progress:
            side = "left" if half % 2 == 0 else "right"
            t = progress / last
            joints = {f"{side}_hip": -18 * math.sin(2 * math.pi * t),
                      f"{side}_knee": 24 * math.sin(math.pi * t)**2}
        track.append(keyframe(frame, joints, distance * frame / last))
    return track


def _human_target(operation, path, entities):
    entity_id = operation["entity"]
    if not isinstance(entity_id, str) or entity_id not in entities:
        raise ValueError(f"{path}.entity: 未找到实体 {entity_id!r}")
    entity = entities[entity_id]
    if entity["kind"] != "human":
        raise ValueError(f"{path}.entity: 只有人物可编辑动作")
    return entity


def _replace_track(entity, track):
    entity.pop("joints", None)
    if track:
        entity["keyframes"] = track
    else:
        entity.pop("keyframes", None)


def apply_edit(doc, operations):
    """按顺序应用语义编辑，整批校验通过后才返回新文档。"""
    validate_scene(doc)
    if not isinstance(operations, list) or not 1 <= len(operations) <= 128:
        raise ValueError("operations: 需要 1 到 128 个操作")
    updated = deepcopy(doc)
    entities = {entity["id"]: entity for entity in updated["scene"]["entities"]}
    count = frame_count(updated)
    for index, operation in enumerate(operations):
        path = f"operations[{index}]"
        if not isinstance(operation, dict):
            raise ValueError(f"{path}: 操作必须是对象")
        name = operation.get("op")
        if name == "set_action_text":
            _object(operation, path, ("op", "text"))
            _validate_action_text(operation["text"], f"{path}.text")
            updated["shot"]["action_text"] = operation["text"]
            continue
        if name == "set_ground_size":
            _object(operation, path, ("op", "size"))
            updated["scene"]["ground"]["size"] = deepcopy(operation["size"])
            continue
        if name == "add_entity":
            _object(operation, path, ("op", "entity"))
            entity = deepcopy(operation["entity"])
            seen = {key: "scene.entities" for key in entities}
            _validate_entity(entity, f"{path}.entity", seen, count)
            entities[entity["id"]] = entity
            updated["scene"]["entities"].append(entity)
            continue
        if name == "apply_component":
            _object(operation, path, ("op", "component", "entity", "params"))
            component, params = operation["component"], operation["params"]
            _literal(component, path + ".component", *ACTIONS, *PROPS)
            if component in ACTIONS:
                _object(params, path + ".params", (), ACTIONS[component][1])
                entity = _human_target(operation, path, entities)
                _replace_track(entity, _builtin_track(component, params, entity, count,
                                                       updated["shot"]["fps"], path + ".params"))
            else:
                _object(params, path + ".params", ("pos",), ("yaw",))
                entity_id = operation["entity"]
                _id(entity_id, path + ".entity", dict.fromkeys(entities, "scene.entities"))
                entity = {"id": entity_id, "kind": "prop", "component": component,
                          "transform": deepcopy(params), "name": PROPS[component][0]}
                _validate_entity(entity, path + ".entity", {}, count)
                entities[entity_id] = entity
                updated["scene"]["entities"].append(entity)
            continue
        if name in ("set_keyframe", "remove_keyframe", "set_action"):
            field = {"set_keyframe": "keyframe", "remove_keyframe": "frame", "set_action": "keyframes"}[name]
            _object(operation, path, ("op", "entity", field))
            entity = _human_target(operation, path, entities)
            if name == "set_action":
                track = operation[field]
                _validate_track(track, f"{path}.{field}", count)
                _replace_track(entity, deepcopy(track))
                continue
            track = list(entity.get("keyframes", []))
            if name == "set_keyframe":
                keyframe = operation[field]
                _validate_keyframe(keyframe, f"{path}.{field}", count)
                frame = keyframe["frame"]
            else:
                frame = operation[field]
                _validate_frame_index(frame, f"{path}.{field}", count)
            index_in_track = bisect_left(track, frame, key=lambda point: point["frame"])
            exists = index_in_track < len(track) and track[index_in_track]["frame"] == frame
            if name == "remove_keyframe":
                if not exists:
                    raise ValueError(f"{path}.frame: 未找到关键帧 {frame}")
                track.pop(index_in_track)
            else:
                track[index_in_track:index_in_track + int(exists)] = [deepcopy(keyframe)]
                if len(track) > MAX_KEYFRAMES:
                    raise ValueError(f"{path}.keyframe: expected at most {MAX_KEYFRAMES} keyframes")
            _replace_track(entity, track)
            continue
        if name == "remove_entity":
            _object(operation, path, ("op", "entity"))
            entity_id = operation["entity"]
            if not isinstance(entity_id, str) or entity_id not in entities:
                raise ValueError(f"{path}.entity: 未找到实体 {entity_id!r}")
            updated["scene"]["entities"].remove(entities.pop(entity_id))
            continue
        if name == "set_entity_name":
            _object(operation, path, ("op", "entity", "name"))
            entity_id = operation["entity"]
            if not isinstance(entity_id, str) or entity_id not in entities:
                raise ValueError(f"{path}.entity: 未找到实体 {entity_id!r}")
            label = _entity_name(operation["name"], f"{path}.name")
            if label:
                entities[entity_id]["name"] = label
            else:
                entities[entity_id].pop("name", None)
            continue
        if name == "set_camera":
            required, fields = ("op",), CAMERA_FIELDS
            target = updated["camera"]
        elif name == "set_transform" or name == "set_body":
            key = "entity" if name == "set_transform" else "actor"
            required = ("op", key)
            fields = ("pos", "yaw") if name == "set_transform" else BODY_FIELDS
            _object(operation, path, required, fields)
            entity_id = operation[key]
            if not isinstance(entity_id, str) or entity_id not in entities:
                raise ValueError(f"{path}.{key}: 未找到实体 {entity_id!r}")
            entity = entities[entity_id]
            if name == "set_body" and entity["kind"] != "human":
                raise ValueError(f"{path}.{key}: 只有人物可修改体型")
            target = entity["transform" if name == "set_transform" else "body"]
        else:
            raise ValueError(f"{path}.op: 不支持的操作 {name!r}")
        if name == "set_camera":
            _object(operation, path, required, fields)
        changes = {key: operation[key] for key in fields if key in operation}
        if not changes:
            raise ValueError(f"{path}: 至少指定一个待修改字段")
        target.update(deepcopy(changes))
    return validate_scene(updated)


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def _reject_constant(token):
    raise ValueError(f"invalid JSON numeric constant {token}")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def load_scene(path):
    return validate_scene(_read_json(path))


def load_edits(path):
    return _read_json(path)
